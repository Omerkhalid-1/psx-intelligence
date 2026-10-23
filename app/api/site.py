"""Public, read-only endpoints behind the website: analysis engine, parameter guide, engine
self-checks, PSX news search and symbol suggestions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from flask import current_app, request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from app.api import api_v1
from app.api.responses import ApiError, ok
from app.api.validation import parse_query
from app.extensions import db
from app.utils.cache import TTLCache
from app.utils.ratelimit import RateLimiter

log = logging.getLogger(__name__)

_engine_cache: TTLCache | None = None
_checks_cache = TTLCache(300, maxsize=2)
_limiter: RateLimiter | None = None


def _rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(current_app.config["REDIS_URL"], prefix="psx:rl:")
    return _limiter


def _client_key() -> str:
    return request.remote_addr or "unknown"  # ProxyFix (TRUSTED_PROXY_COUNT) sets the real client address


def _limit(name: str, limit: int, window: int = 60, *, key: str | None = None) -> None:
    decision = _rate_limiter().hit(f"{name}:{key or _client_key()}", limit, window)
    if not decision.allowed:
        raise ApiError(429, "rate_limited", f"Too many requests; retry in {decision.retry_after}s",
                       {"retry_after_seconds": decision.retry_after})


def _tracked_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if symbol not in current_app.config["PSXDATA_SYMBOLS"]:
        raise ApiError(404, "not_tracked", f"{symbol} is not on the tracked watchlist (PSXDATA_SYMBOLS)",
                       {"tracked": list(current_app.config["PSXDATA_SYMBOLS"])})
    return symbol


class EngineParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    series: bool = False
    limit: int = Field(default=500, ge=60, le=2500)


def engine_payload(symbol: str, *, with_series: bool = False, limit: int = 500,
                   frames: dict[str, Any] | Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Engine output for a tracked symbol (cached per worker for ENGINE_CACHE_SECONDS). `frames` lets a
    caller that needs many symbols load the price universe once."""
    from app.services.research import engine
    from app.services.research.prices import BENCHMARK, load_universe

    global _engine_cache
    if _engine_cache is None:
        _engine_cache = TTLCache(current_app.config["ENGINE_CACHE_SECONDS"], maxsize=256)
    ttl_key = (symbol, with_series, limit)
    cached = _engine_cache.get(ttl_key)
    if cached is not None:
        return cached
    if frames is not None and callable(frames):
        frames = frames()
    if frames is None or symbol not in frames:
        frames = load_universe((symbol,))
    pf, bench = frames.get(symbol), frames.get(BENCHMARK)
    if pf is None or pf.frame.empty or bench is None:
        raise ApiError(404, "no_data", f"No stored bars for {symbol} yet")
    x = engine.compute_frame(pf, bench) if len(pf.frame) >= engine.MIN_HISTORY else None
    out = engine.evaluate(pf, bench, frame=x)
    out["data_issues"] = data_issues(symbol)
    if with_series and out.get("status") == "ok":
        out["series"] = engine.series(x, limit=limit)
    _engine_cache.set(ttl_key, out)
    return out


def data_issues(symbol: str) -> list[dict[str, Any]]:
    """Open price discontinuities nobody has explained yet (most likely an unrecorded bonus issue or
    split): indicators spanning them are distorted, so pages must say so."""
    from sqlalchemy import select

    from app.models.research import DataQualityIssue

    rows = db.session.scalars(select(DataQualityIssue).where(
        DataQualityIssue.symbol == symbol, DataQualityIssue.resolved_at.is_(None),
        DataQualityIssue.check_name == "unexplained_discontinuity")
        .order_by(DataQualityIssue.session_date.desc())).all()
    return [{"date": r.session_date.isoformat() if r.session_date else None,
             "gap": (r.details or {}).get("gap"), "check": r.check_name} for r in rows]


@api_v1.get("/engine/parameters")
def engine_parameters():
    from app.services.research import engine

    return ok({"engine_version": engine.ENGINE_VERSION, "groups": engine.GROUPS,
               "parameters": engine.parameter_guide()})


@api_v1.get("/engine/diagnostics")
def engine_diagnostics():
    from app.services.research import engine_checks
    from app.services.research.prices import load_universe

    report = _checks_cache.get("report")
    if report is None:
        symbols = tuple(current_app.config["PSXDATA_SYMBOLS"])
        report = engine_checks.run_checks(load_universe(symbols), symbols)
        _checks_cache.set("report", report)
    return ok(report)


@api_v1.get("/engine/<symbol>")
def engine_symbol(symbol: str):
    params = parse_query(EngineParams)
    symbol = _tracked_symbol(symbol)
    try:
        return ok(engine_payload(symbol, with_series=params.series, limit=params.limit))
    except SQLAlchemyError:
        db.session.rollback()
        log.exception("engine failed to load data", extra={"event_type": "engine_error"})
        raise ApiError(503, "database_unavailable", "Market data could not be loaded") from None


class NewsSearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=12)
    limit: int = Field(default=30, ge=1, le=40)


@api_v1.get("/news/search")
def news_search():
    """Headlines about one listed PSX company only. Live results are cached for 15 minutes."""
    from app.services.news import search

    params = parse_query(NewsSearchParams)
    config = current_app.config
    _limit("news-search", config["NEWS_SEARCH_PER_IP_PER_MINUTE"])
    try:
        symbol = search.normalize_symbol(params.symbol)
    except search.InvalidSymbol as exc:
        raise ApiError(400, "invalid_symbol", str(exc)) from None
    def outbound_gate() -> bool:
        """Charged only on a cache miss, i.e. when a live Google News request would be made."""
        return _rate_limiter().hit("news-outbound:all", config["NEWS_SEARCH_OUTBOUND_PER_MINUTE"], 60).allowed

    try:
        result = search.search(config, symbol, limit=params.limit, outbound_gate=outbound_gate)
    except search.UnknownSymbol as exc:
        raise ApiError(404, "unknown_symbol", str(exc)) from None
    except search.SearchUnavailable as exc:
        raise ApiError(503, "news_unavailable", "News search is temporarily unavailable",
                       {"reason": str(exc)}) from None
    return ok(result, warnings=[result["warning"]] if result.get("warning") else [])


class SuggestParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(min_length=1, max_length=40)
    limit: int = Field(default=8, ge=1, le=20)


@api_v1.get("/symbols/search")
def symbol_search():
    from app.services.news import search

    params = parse_query(SuggestParams)
    _limit("symbol-suggest", current_app.config["SYMBOL_SUGGEST_PER_IP_PER_MINUTE"])
    return ok(search.suggest(current_app.config, params.q, params.limit))


def clear_caches() -> None:
    if _engine_cache is not None:
        _engine_cache.clear()
    _checks_cache.clear()
