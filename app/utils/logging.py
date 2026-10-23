"""Structured JSON logging with correlation IDs and secret redaction.

Every record carries the fields bound in the current `log_context` (correlation_id,
run_id, job_id, symbol, ...), so API requests and Celery tasks emit a consistent
schema without passing loggers around.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("log_context", default={})

# Fields that may be bound into the context or passed via `extra=`.
CONTEXT_FIELDS = (
    "correlation_id",
    "run_id",
    "job_id",
    "analysis_module",
    "symbol",
    "source_id",
    "model_version",
    "event_type",
    "duration_ms",
    "status",
    "error_category",
)

_SECRET_PATTERNS = [
    re.compile(r"(bearer\s+)(\S+)()", re.IGNORECASE),
    re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)([^@\s]+)(@)", re.IGNORECASE),
    re.compile(r"(redis://[^:/\s]*:)([^@\s]+)(@)", re.IGNORECASE),
    re.compile(r"((?:authorization|bearer|token|api[_-]?key|password|secret)[\"']?\s*[:=]\s*[\"']?)([^\s\"',]+)",
               re.IGNORECASE),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}***{m.group(3) if m.lastindex == 3 else ''}", text)
    return text


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def get_context() -> dict[str, Any]:
    return dict(_context.get())


def bind_context(**fields: Any) -> contextvars.Token:
    merged = {**_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    return _context.set(merged)


def reset_context(token: contextvars.Token) -> None:
    _context.reset(token)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = bind_context(**fields)
    try:
        yield
    finally:
        reset_context(token)


class ContextFilter(logging.Filter):
    def __init__(self, environment: str, service: str) -> None:
        super().__init__()
        self.environment = environment
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.environment = self.environment
        record.service = self.service
        for key, value in _context.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "severity": record.levelname,
            "environment": getattr(record, "environment", None),
            "service": getattr(record, "service", None),
            "component": record.name,
            "message": redact(record.getMessage()),
        }
        for key in CONTEXT_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        details = getattr(record, "details", None)
        if details is not None:
            payload["details"] = json.loads(redact(json.dumps(details, default=str)))
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = " ".join(f"{k}={getattr(record, k)}" for k in CONTEXT_FIELDS if getattr(record, k, None) is not None)
        base = f"{datetime.fromtimestamp(record.created).isoformat(timespec='seconds')} {record.levelname:<7} " \
               f"{record.name}: {redact(record.getMessage())}"
        out = f"{base} [{ctx}]" if ctx else base
        if record.exc_info:
            out += "\n" + redact(self.formatException(record.exc_info))
        return out


def configure_logging(*, level: str, fmt: str, environment: str, service: str, log_file: str | None = None) -> None:
    """Configure the root logger. Idempotent: replaces handlers installed by a previous call."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_psx_handler", False):
            root.removeHandler(handler)
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=5))
    context_filter = ContextFilter(environment, service)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(context_filter)
        handler._psx_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level)
    # Werkzeug's per-request access log duplicates our request log.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
