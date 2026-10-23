"""Dependency health checks. Each check is bounded by a timeout and reports its own
failure instead of raising, so one broken dependency never hides the others."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime

import redis
from flask import current_app
from pydantic import BaseModel
from sqlalchemy import text

from app.extensions import db
from app.utils.time import utcnow

log = logging.getLogger(__name__)

class DependencyStatus(BaseModel):
    name: str
    status: str  # "ok" | "unavailable" | "degraded" | "unknown"
    latency_ms: float | None = None
    detail: str | None = None


SCHEDULER_HEARTBEAT_KEY = "psx:scheduler:heartbeat"
# Beat fires the heartbeat task every 60s; allow generous slack before calling it stale.
SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS = 180


def _timed(name: str, fn: Callable[[], tuple[str, str | None]]) -> DependencyStatus:
    start = time.perf_counter()
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001 - a health probe must report, not raise
        log.warning("health check failed", extra={"event_type": "health_check_failed",
                                                  "details": {"dependency": name, "error": type(exc).__name__}})
        status, detail = "unavailable", type(exc).__name__
    return DependencyStatus(
        name=name, status=status, latency_ms=round((time.perf_counter() - start) * 1000, 2), detail=detail
    )


def _redis_client(url: str) -> redis.Redis:
    timeout = current_app.config["DEPENDENCY_CHECK_TIMEOUT_SECONDS"]
    return redis.Redis.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout)


def check_database() -> DependencyStatus:
    def probe() -> tuple[str, str | None]:
        db.session.execute(text("SELECT 1"))
        return "ok", None

    try:
        return _timed("postgresql", probe)
    finally:
        db.session.rollback()


def check_redis() -> DependencyStatus:
    def probe() -> tuple[str, str | None]:
        _redis_client(current_app.config["REDIS_URL"]).ping()
        return "ok", None

    return _timed("redis", probe)


def check_workers() -> DependencyStatus:
    def probe() -> tuple[str, str | None]:
        from app.tasks.celery_app import celery

        replies = celery.control.ping(timeout=current_app.config["DEPENDENCY_CHECK_TIMEOUT_SECONDS"])
        if not replies:
            return "unavailable", "no Celery workers responded"
        return "ok", f"{len(replies)} worker(s) responded"

    return _timed("celery_workers", probe)


def check_scheduler() -> DependencyStatus:
    def probe() -> tuple[str, str | None]:
        raw = _redis_client(current_app.config["REDIS_URL"]).get(SCHEDULER_HEARTBEAT_KEY)
        if raw is None:
            return "unknown", "no scheduler heartbeat recorded"
        last = datetime.fromisoformat(raw.decode())
        age = (utcnow() - last).total_seconds()
        if age > SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS:
            return "degraded", f"last heartbeat {int(age)}s ago"
        return "ok", f"last heartbeat {int(age)}s ago"

    return _timed("scheduler", probe)


def check_psxdata_service() -> DependencyStatus:
    """The self-hosted psxdata-api process. Its /health does not contact PSX."""

    def probe() -> tuple[str, str | None]:
        from app.services.ingestion.providers.psxdata_api import PsxDataApiClient

        client = PsxDataApiClient(base_url=current_app.config["PSXDATA_API_URL"],
                                  timeout_seconds=current_app.config["DEPENDENCY_CHECK_TIMEOUT_SECONDS"],
                                  max_attempts=1)
        return ("ok", None) if client.health() else ("degraded", "unexpected /health payload")

    return _timed("psxdata_api", probe)


def check_all() -> list[DependencyStatus]:
    checks = [check_database(), check_redis(), check_workers(), check_scheduler()]
    if current_app.config["PSXDATA_ENABLED"]:
        checks.append(check_psxdata_service())
    return checks


def overall_status(checks: list[DependencyStatus]) -> str:
    by_name = {c.name: c.status for c in checks}
    if by_name.get("postgresql") != "ok":
        return "unavailable"  # nothing meaningful can be served without the canonical store
    if all(c.status == "ok" for c in checks):
        return "ok"
    return "degraded"
