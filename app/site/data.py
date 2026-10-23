"""Page data for the website, assembled from the analysis engine and stored headlines."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from app.extensions import db
from app.models.forecasting import PipelineRun
from app.models.operations import JobRun, JobStatus
from app.utils.cache import TTLCache

log = logging.getLogger(__name__)

MATRIX_KEYS = ("price_vs_sma200", "ma_cross", "adx", "rsi14", "macd", "bollinger", "rel_volume", "excess_return",
               "drawdown", "range_52w")
CARD_KEYS = ("price_vs_sma200", "rsi14", "vol_regime")
SPARK_SESSIONS = 120


def company_names(config: Mapping[str, Any]) -> dict[str, str]:
    from app.services.news.search import load_listing

    try:
        return load_listing(config)
    except Exception:  # noqa: BLE001 - names are decoration; never fail a page for them
        log.warning("company names unavailable", extra={"event_type": "listing_unavailable"})
        return {}


def _slim(payload: dict[str, Any], keys: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    by_key = {r["key"]: r for r in payload.get("readings", [])}
    return {k: {f: by_key[k][f] for f in ("name", "display", "state", "tone", "headline", "group")}
            for k in keys if k in by_key}


_meta_cache = TTLCache(300, maxsize=2)


def sectors(config: Mapping[str, Any]) -> dict[str, str]:
    """{symbol: sector} from config/watchlist.yaml (display metadata only)."""
    try:
        doc = yaml.safe_load(Path(config["WATCHLIST_CONFIG"]).read_text()) or {}
    except (OSError, yaml.YAMLError):
        log.warning("watchlist metadata unreadable", extra={"event_type": "config_error"})
        return {}
    return {str(k).upper(): str((v or {}).get("sector") or "") for k, v in (doc.get("symbols") or {}).items()}


def watch_meta(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Tracked symbols with company name and sector, ordered by sector then symbol (index first)."""
    hit = _meta_cache.get("meta")
    if hit is not None:
        return hit
    names, secs = company_names(config), sectors(config)
    rows = [{"symbol": s, "name": names.get(s, s), "sector": secs.get(s) or "Other", "is_index": s == "KSE100"}
            for s in config["PSXDATA_SYMBOLS"]]
    rows.sort(key=lambda r: (not r["is_index"], r["sector"], r["symbol"]))
    _meta_cache.set("meta", rows)
    return rows


def watchlist(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    from app.api.site import engine_payload
    from app.services.research.prices import load_universe

    names = company_names(config)
    secs = sectors(config)
    universe: dict[str, Any] = {}

    def frames() -> dict[str, Any]:
        # Loaded once, and only if some symbol is not already cached.
        if not universe:
            universe.update(load_universe(tuple(config["PSXDATA_SYMBOLS"])))
        return universe

    out = []
    for symbol in config["PSXDATA_SYMBOLS"]:
        try:
            p = engine_payload(symbol, with_series=True, limit=SPARK_SESSIONS, frames=frames)
        except Exception:  # noqa: BLE001 - one missing symbol must not blank the page
            log.warning("engine unavailable for symbol", extra={"event_type": "engine_error",
                                                                 "details": {"symbol": symbol}})
            out.append({"symbol": symbol, "name": names.get(symbol, symbol), "sector": secs.get(symbol) or "Other",
                        "status": "unavailable"})
            continue
        if p.get("status") != "ok":
            out.append({"symbol": symbol, "name": names.get(symbol, symbol), "sector": secs.get(symbol) or "Other",
                        "status": p.get("status")})
            continue
        s = p["series"]
        out.append({"symbol": symbol, "name": names.get(symbol, symbol), "sector": secs.get(symbol) or "Other",
                    "status": "ok", "is_index": p["is_index"],
                    "session_date": p["session_date"], "price": p["price"], "balance": p["balance"],
                    "story": p["story"][:2], "cards": _slim(p, CARD_KEYS), "matrix": _slim(p, MATRIX_KEYS),
                    "spark": {"time": s["time"], "close": s["close"]}})
    return out


def market_headlines(limit: int = 8) -> list[dict[str, Any]]:
    from app.services.research import views

    items, _ = views.news_for("MARKET", 1, limit * 3)
    seen, out = set(), []
    for it in items:
        if it["cluster_id"] in seen:
            continue
        seen.add(it["cluster_id"])
        out.append({k: it[k] for k in ("title", "url", "publisher", "published_at", "sentiment_label")})
        if len(out) == limit:
            break
    return out


def freshness() -> dict[str, Any]:
    last_refresh = db.session.scalar(select(JobRun).where(JobRun.job_type == "refresh.latest",
                                                          JobRun.status == JobStatus.SUCCEEDED)
                                     .order_by(JobRun.finished_at.desc()).limit(1))
    last_pipeline = db.session.scalar(select(PipelineRun).where(PipelineRun.status.in_(["succeeded", "partial"]))
                                      .order_by(PipelineRun.started_at.desc()).limit(1))
    return {"last_refresh_at": last_refresh.finished_at.isoformat() if last_refresh and last_refresh.finished_at
            else None,
            "last_pipeline_at": last_pipeline.finished_at.isoformat() if last_pipeline and last_pipeline.finished_at
            else None}


def matrix_columns() -> list[dict[str, Any]]:
    from app.services.research.engine import PARAMETER_INDEX

    return [{"key": k, "name": PARAMETER_INDEX[k].name, "what": PARAMETER_INDEX[k].what,
             "rule": PARAMETER_INDEX[k].rule} for k in MATRIX_KEYS]
