"""Market data served from our own database (never proxied to psxdata-api)."""

from __future__ import annotations

from datetime import date

from flask import current_app
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api import api_v1
from app.api.responses import ApiError, ok
from app.api.validation import parse_query
from app.services.market_data import (
    PRICE_NOTE,
    SOURCE_LABEL,
    bar_as_dict,
    bar_count,
    bars_freshness,
    integration_status,
    price_history,
    quotes_freshness,
    symbol_summaries,
)


class HistoryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date | None = None
    end: date | None = None
    limit: int = Field(default=260, ge=1, le=5000)

    @model_validator(mode="after")
    def _order(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("start must not be after end")
        return self


def _tracked() -> tuple[str, ...]:
    return tuple(current_app.config["PSXDATA_SYMBOLS"])


@api_v1.get("/stocks")
def stocks():
    config = current_app.config
    status = integration_status(config)
    bars, quotes = bars_freshness(config, status), quotes_freshness(config, status)
    warnings = [f"{f.dataset}: {f.state}" + (f" ({f.detail})" if f.detail else "")
                for f in (bars, quotes) if f.state != "fresh"]
    return ok([s.as_dict() for s in symbol_summaries(_tracked())], meta={
        "source": SOURCE_LABEL, "note": PRICE_NOTE,
        "data_status": {"daily_bars": bars.state, "quotes": quotes.state},
        "as_of": {"daily_bars": bars.latest_observation.isoformat() if bars.latest_observation else None,
                  "quotes": quotes.latest_observation.isoformat() if quotes.latest_observation else None},
    }, warnings=warnings)


@api_v1.get("/stocks/<symbol>/history")
def stock_history(symbol: str):
    symbol = symbol.upper()
    if symbol not in _tracked():
        raise ApiError(404, "not_found", f"{symbol} is not a tracked symbol")
    params = parse_query(HistoryParams)
    rows = price_history(symbol, start=params.start, end=params.end, limit=params.limit)
    warnings = [] if rows else ["no daily bars stored for this symbol/range"]
    flagged = sum(1 for r in rows if r.quality_flags)
    if flagged:
        warnings.append(f"{flagged} bar(s) carry quality flags; inspect quality_flags before using them")
    return ok([bar_as_dict(r) for r in rows], meta={
        "symbol": symbol, "count": len(rows), "total_stored": bar_count(symbol), "adjusted": False,
        "source": SOURCE_LABEL, "note": PRICE_NOTE,
        "data_status": bars_freshness(current_app.config).state,
    }, warnings=warnings)


@api_v1.get("/market/quotes")
def market_quotes():
    config = current_app.config
    fresh = quotes_freshness(config)
    rows = [{"symbol": s.symbol, "instrument_kind": s.kind, **(s.as_dict()["latest_quote"] or {})}
            for s in symbol_summaries(_tracked()) if s.quote is not None]
    warnings = [] if fresh.state == "fresh" else [f"quotes: {fresh.state}" + (f" ({fresh.detail})" if fresh.detail
                                                                              else "")]
    return ok(rows, meta={"source": SOURCE_LABEL, "note": PRICE_NOTE, "data_status": fresh.state,
                          "last_checked_at": fresh.last_successful_run_at.isoformat()
                          if fresh.last_successful_run_at else None}, warnings=warnings)
