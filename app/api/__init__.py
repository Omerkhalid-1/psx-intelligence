"""Versioned REST API (`/api/v1`)."""

from __future__ import annotations

import logging

from flask import Blueprint, Flask, request
from werkzeug.exceptions import HTTPException

from app.api.responses import ApiError, error_response

log = logging.getLogger(__name__)

api_v1 = Blueprint("api_v1", __name__, url_prefix="/api/v1")

# Route modules attach handlers to `api_v1` on import.
from app.api import health, ingestion, jobs, market, research, site  # noqa: E402,F401


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):
        if exc.status >= 500:
            log.error("api error", extra={"event_type": "api_error", "status": exc.status,
                                          "error_category": exc.code})
        return error_response(exc.status, exc.code, exc.message, exc.details)

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        if request.path.startswith("/api/"):
            return error_response(exc.code or 500, (exc.name or "error").lower().replace(" ", "_"),
                                  exc.description or exc.name)
        return exc

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        # Full details go to the log (redacted); the client gets a generic message only.
        log.exception("unhandled exception", extra={"event_type": "api_error", "error_category": "unhandled"})
        if request.path.startswith("/api/"):
            return error_response(500, "internal_error", "An internal error occurred")
        return "Internal server error", 500
