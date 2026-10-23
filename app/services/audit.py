"""Durable audit trail writer."""

from __future__ import annotations

from typing import Any

from app.extensions import db
from app.models.operations import AuditEvent
from app.utils.logging import get_context


def record_audit(*, actor: str, action: str, entity_type: str, entity_id: str | None = None,
                 details: dict[str, Any] | None = None) -> AuditEvent:
    """Add an audit event to the current session. The caller owns the transaction so the
    audit row commits (or rolls back) together with the change it describes."""
    event = AuditEvent(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        correlation_id=get_context().get("correlation_id"),
        details=details or {},
    )
    db.session.add(event)
    return event
