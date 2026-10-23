"""Operational records: background job runs and the durable audit trail."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import recorded_at_col, updated_at_col, uuid_pk


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Enqueue itself failed (e.g. broker unreachable): the job never reached a worker.
    DISPATCH_FAILED = "dispatch_failed"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.DISPATCH_FAILED}


class JobRun(db.Model):
    """One requested unit of background work. Created *before* enqueueing so a job is
    visible (and its failure recordable) even if the broker or worker never picks it up."""

    __tablename__ = "job_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=JobStatus.QUEUED,
    )
    # Prevents duplicate submissions of logically identical work while one is active/complete.
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_category: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)  # sanitised; never raw secrets
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = recorded_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_job_runs_status_created", "status", "created_at"),
        Index("ix_job_runs_type_created", "job_type", "created_at"),
    )

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return None


class AuditEvent(db.Model):
    """Append-only record of administrative and state-changing actions. Unlike logs,
    audit events are never rotated away."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = recorded_at_col()
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(100))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_occurred_at", "occurred_at"),
    )
