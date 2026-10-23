from __future__ import annotations

import uuid

from flask import g
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api import api_v1
from app.api.auth import require_admin
from app.api.responses import ApiError, ok
from app.api.schemas import JobListParams, JobOut
from app.api.validation import parse_body, parse_query
from app.extensions import db
from app.models.operations import JobRun
from app.services.orchestration import jobs


class JobSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_type: str = Field(max_length=64)
    parameters: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)


@api_v1.get("/jobs")
def list_jobs():
    params = parse_query(JobListParams)
    query = select(JobRun)
    if params.status:
        query = query.where(JobRun.status == params.status)
    if params.job_type:
        query = query.where(JobRun.job_type == params.job_type)
    total = db.session.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.session.scalars(
        query.order_by(JobRun.created_at.desc())
        .offset((params.page - 1) * params.page_size)
        .limit(params.page_size)
    ).all()
    return ok(
        [JobOut.model_validate(r).model_dump(mode="json") for r in rows],
        meta={"page": params.page, "page_size": params.page_size, "total": total},
    )


@api_v1.get("/jobs/<job_id>")
def get_job(job_id: str):
    try:
        jid = uuid.UUID(job_id)
    except ValueError:
        raise ApiError(400, "invalid_id", "job_id must be a UUID") from None
    job = db.session.get(JobRun, jid)
    if job is None:
        raise ApiError(404, "not_found", "Job not found")
    return ok(JobOut.model_validate(job).model_dump(mode="json"))


@api_v1.post("/jobs")
@require_admin
def submit_job():
    """Submit a background job. Returns 202 with the job record: acceptance is not completion."""
    body = parse_body(JobSubmit)
    try:
        result = jobs.submit_job(body.job_type, parameters=body.parameters, requested_by=g.actor,
                                 idempotency_key=body.idempotency_key)
    except jobs.UnknownJobType:
        raise ApiError(400, "unknown_job_type", f"Unknown job_type; allowed: {sorted(jobs.JOB_TASKS)}") from None
    data = JobOut.model_validate(result.job).model_dump(mode="json")
    warnings = ["An identical job (same idempotency_key) already exists; returning it"] if result.deduplicated else []
    return ok(data, meta={"deduplicated": result.deduplicated}, warnings=warnings,
              status=200 if result.deduplicated else 202)
