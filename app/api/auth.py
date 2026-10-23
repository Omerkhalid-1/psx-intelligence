"""Bearer-token protection for administrative and job-triggering endpoints.

A single shared admin token is deliberately minimal (see ADR-008). It is compared in
constant time and never logged. If no token is configured the protected endpoints are
disabled outright rather than left open.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import current_app, g, request

from app.api.responses import ApiError


def require_admin(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        expected = current_app.config.get("ADMIN_API_TOKEN")
        if not expected:
            raise ApiError(503, "admin_disabled", "Administrative endpoints are disabled: ADMIN_API_TOKEN is not set")
        header = request.headers.get("Authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not supplied:
            raise ApiError(401, "unauthorized", "Missing bearer token")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise ApiError(403, "forbidden", "Invalid credentials")
        g.actor = "admin"
        return view(*args, **kwargs)

    return wrapper
