"""Uniform API envelope: {data, meta, warnings, errors}.

Errors raised as `ApiError` are converted by the handlers registered in
`app.api.register_error_handlers`; unexpected exceptions become a generic 500 that
never echoes internal details.
"""

from __future__ import annotations

from typing import Any

from flask import g, jsonify

from app.utils.time import utcnow


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _base_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "correlation_id": getattr(g, "correlation_id", None),
        "generated_at": utcnow().isoformat(),
        **(meta or {}),
    }


def ok(data: Any, *, meta: dict[str, Any] | None = None, warnings: list[str] | None = None, status: int = 200):
    body = {"data": data, "meta": _base_meta(meta), "warnings": warnings or [], "errors": []}
    return jsonify(body), status


def error_response(status: int, code: str, message: str, details: Any = None):
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    body = {"data": None, "meta": _base_meta(None), "warnings": [], "errors": [err]}
    return jsonify(body), status
