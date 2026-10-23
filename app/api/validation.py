"""Helpers that turn Pydantic validation failures into 400 responses."""

from __future__ import annotations

from typing import TypeVar

from flask import request
from pydantic import BaseModel, ValidationError

from app.api.responses import ApiError

M = TypeVar("M", bound=BaseModel)


def _errors(exc: ValidationError) -> list[dict]:
    return [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]


def parse_query(model: type[M]) -> M:
    try:
        return model.model_validate(request.args.to_dict())
    except ValidationError as exc:
        raise ApiError(400, "invalid_query", "Invalid query parameters", _errors(exc)) from None


def parse_body(model: type[M]) -> M:
    payload = request.get_json(silent=True)
    if payload is None:
        raise ApiError(400, "invalid_body", "Request body must be a JSON object")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ApiError(400, "invalid_body", "Invalid request body", _errors(exc)) from None
