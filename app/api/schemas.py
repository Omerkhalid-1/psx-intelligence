"""Pydantic models for API request validation and response serialisation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.operations import JobStatus


class PaginationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(default=1, ge=1, le=100_000)
    page_size: int = Field(default=50, ge=1, le=200)


class JobListParams(PaginationParams):
    status: JobStatus | None = None
    job_type: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_.]+$")


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: str
    status: JobStatus
    correlation_id: str
    requested_by: str
    parameters: dict[str, Any]
    result_summary: dict[str, Any] | None
    error_category: str | None
    error_message: str | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
