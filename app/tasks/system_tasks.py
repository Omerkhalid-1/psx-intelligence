"""System tasks: scheduler heartbeat and an end-to-end worker self-check."""

from __future__ import annotations

from typing import Any

import redis
from flask import current_app
from sqlalchemy import func, select, text

from app.extensions import db
from app.models.operations import JobRun
from app.services.health import SCHEDULER_HEARTBEAT_KEY
from app.tasks.base import tracked_job
from app.tasks.celery_app import celery
from app.utils.time import utcnow


@celery.task(name="system.scheduler_heartbeat", ignore_result=True)
def scheduler_heartbeat() -> None:
    """Fired by Celery beat; proves that beat *and* at least one worker are alive."""
    client = redis.Redis.from_url(current_app.config["REDIS_URL"], socket_timeout=2)
    client.set(SCHEDULER_HEARTBEAT_KEY, utcnow().isoformat(), ex=3600)


@tracked_job("system.self_check", max_retries=0)
def self_check(job: JobRun) -> dict[str, Any]:
    """Verifies that a worker can reach the database. Used to validate deployments."""
    db.session.execute(text("SELECT 1"))
    job_count = db.session.scalar(select(func.count()).select_from(JobRun))
    return {"database": "ok", "job_runs_visible": job_count, "checked_at": utcnow().isoformat()}
