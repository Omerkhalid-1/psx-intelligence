"""Read-side queries over persisted market data (used by the API, dashboard and health).
Nothing here calls the provider; pages and API reads are served from PostgreSQL."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy import func, select

from app.config import parse_window
from app.extensions import db
from app.models.market_data import DailyBar, IngestionRun, IngestionStatus, Instrument, QuoteSnapshot
from app.services.ingestion.providers.psxdata_api import SOURCE_ID
from app.services.ingestion.psx import DATASET_BARS, DATASET_QUOTES
from app.utils.time import PSX_TZ, psx_local_date, utcnow

SOURCE_LABEL = "psxdata-api (PSX Data Portal)"
PRICE_NOTE = ("Daily bars are unadjusted PSX session prices (no split/bonus/dividend adjustment). Quotes have no "
              "exchange timestamp: the time shown is when PSX Intelligence fetched them, and psxdata may serve "
              "cached values.")


@dataclass(frozen=True)
class IntegrationStatus:
    enabled: bool
    scheduled: bool
    problems: list[str]

    @property
    def ready(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {"source_id": SOURCE_ID, "enabled": self.enabled, "scheduled": self.scheduled, "ready": self.ready,
                "problems": self.problems}


def integration_status(config: Mapping[str, Any]) -> IntegrationStatus:
    enabled = bool(config["PSXDATA_ENABLED"])
    return IntegrationStatus(enabled=enabled, scheduled=bool(enabled and config["ENABLE_SCHEDULED_INGESTION"]),
                             problems=[] if enabled else ["PSXDATA_ENABLED is false"])


@dataclass(frozen=True)
class Freshness:
    dataset: str
    state: str  # not_configured | no_data | fresh | stale
    last_successful_run_at: datetime | None
    last_run: IngestionRun | None
    latest_observation: date | datetime | None  # newest session date (bars) or retrieval time (quotes)
    detail: str | None

    def as_dict(self) -> dict[str, Any]:
        run = self.last_run
        latest = self.latest_observation
        return {
            "dataset": self.dataset, "state": self.state, "detail": self.detail,
            "last_successful_run_at": _iso(self.last_successful_run_at),
            "latest_observation": latest.isoformat() if latest else None,
            "last_run": None if run is None else {
                "id": str(run.id), "status": run.status.value, "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at), "error_category": run.error_category,
                "error_summary": run.error_summary},
        }


def latest_run(dataset: str, *, successful: bool = False) -> IngestionRun | None:
    query = select(IngestionRun).where(IngestionRun.source_id == SOURCE_ID, IngestionRun.dataset == dataset)
    if successful:
        query = query.where(IngestionRun.status.in_([IngestionStatus.SUCCEEDED, IngestionStatus.PARTIALLY_SUCCEEDED]))
    return db.session.scalar(query.order_by(IngestionRun.started_at.desc()).limit(1))


def bars_freshness(config: Mapping[str, Any], status: IntegrationStatus | None = None,
                   now: datetime | None = None) -> Freshness:
    status = status or integration_status(config)
    symbols = tuple(config["PSXDATA_SYMBOLS"])
    newest = db.session.scalar(select(DailyBar.session_date).where(
        DailyBar.source_id == SOURCE_ID, DailyBar.is_current, DailyBar.symbol.in_(symbols))
        .order_by(DailyBar.session_date.desc()).limit(1))
    success = latest_run(DATASET_BARS, successful=True)
    if newest is None:
        state, detail = ("no_data" if status.ready else "not_configured"), None
    else:
        today = psx_local_date(now or utcnow())
        lag = int(np.busday_count(newest, today))  # weekdays after the newest bar, up to today
        limit = int(config["PSXDATA_BARS_STALE_AFTER_WEEKDAYS"])
        state = "stale" if lag > limit else "fresh"
        detail = f"newest bar {newest.isoformat()} is {lag} weekday(s) old (limit {limit}; holidays count)"
    return Freshness(DATASET_BARS, state, success.finished_at if success else None, latest_run(DATASET_BARS),
                     newest, detail)


def quotes_freshness(config: Mapping[str, Any], status: IntegrationStatus | None = None,
                     now: datetime | None = None) -> Freshness:
    status = status or integration_status(config)
    success = latest_run(DATASET_QUOTES, successful=True)
    newest = db.session.scalar(select(QuoteSnapshot.retrieved_at).where(QuoteSnapshot.source_id == SOURCE_ID)
                               .order_by(QuoteSnapshot.retrieved_at.desc()).limit(1))
    if newest is None:
        return Freshness(DATASET_QUOTES, "no_data" if status.ready else "not_configured",
                         success.finished_at if success else None, latest_run(DATASET_QUOTES), None, None)
    # An unchanged re-check stores no new row but still confirms the stored values.
    reference = success.finished_at if success else newest
    age = ((now or utcnow()) - reference).total_seconds() / 60
    limit = int(config["PSXDATA_QUOTES_STALE_AFTER_MINUTES"])
    state = "stale" if age > limit else "fresh"
    detail = f"last checked {age:.0f} min ago (limit {limit})"
    if state == "stale" and not within_window(config["PSXDATA_QUOTE_WINDOW"], now):
        detail += "; outside market hours this is expected"
    return Freshness(DATASET_QUOTES, state, reference, latest_run(DATASET_QUOTES), newest, detail)


@dataclass(frozen=True)
class SymbolSummary:
    symbol: str
    kind: str | None  # "stock" | "index" | None (not yet classified)
    bar: DailyBar | None
    previous_close: Decimal | None
    quote: QuoteSnapshot | None

    @property
    def day_change(self) -> Decimal | None:
        if self.bar is None or self.previous_close is None:
            return None
        return self.bar.close - self.previous_close

    @property
    def day_change_pct(self) -> Decimal | None:
        if self.day_change is None or not self.previous_close:
            return None
        return (self.day_change / self.previous_close * 100).quantize(Decimal("0.01"))

    def as_dict(self) -> dict[str, Any]:
        bar, quote = self.bar, self.quote
        return {
            "symbol": self.symbol, "instrument_kind": self.kind,
            "latest_bar": None if bar is None else {
                **bar_as_dict(bar), "previous_close": _s(self.previous_close),
                "day_change": _s(self.day_change), "day_change_pct": _s(self.day_change_pct)},
            "latest_quote": None if quote is None else {
                "price": _s(quote.price), "change": _s(quote.change), "change_pct": _s(quote.change_pct),
                "ldcp": _s(quote.ldcp), "pe_ratio": _s(quote.pe_ratio), "dividend_yield": _s(quote.dividend_yield),
                "volume_avg_30d": _s(quote.volume_avg_30d), "change_1y_pct": _s(quote.change_1y_pct),
                "provider_cached": quote.provider_cached, "retrieved_at": _iso(quote.retrieved_at)},
        }


def symbol_summaries(symbols: tuple[str, ...]) -> list[SymbolSummary]:
    kinds = dict(db.session.execute(
        select(Instrument.symbol, Instrument.instrument_kind).where(Instrument.symbol.in_(symbols))).all())
    out = []
    for symbol in symbols:
        bars = db.session.scalars(
            select(DailyBar).where(DailyBar.source_id == SOURCE_ID, DailyBar.symbol == symbol, DailyBar.is_current)
            .order_by(DailyBar.session_date.desc()).limit(2)).all()
        quote = db.session.scalar(
            select(QuoteSnapshot).where(QuoteSnapshot.source_id == SOURCE_ID, QuoteSnapshot.symbol == symbol)
            .order_by(QuoteSnapshot.retrieved_at.desc()).limit(1))
        out.append(SymbolSummary(symbol, kinds.get(symbol), bars[0] if bars else None,
                                 bars[1].close if len(bars) > 1 else None, quote))
    return sorted(out, key=lambda s: (s.kind != "index", s.symbol))


def price_history(symbol: str, *, start: date | None = None, end: date | None = None,
                  limit: int = 260) -> list[DailyBar]:
    """Current-revision daily bars, oldest first; `limit` keeps the most recent N in range."""
    query = select(DailyBar).where(DailyBar.source_id == SOURCE_ID, DailyBar.symbol == symbol, DailyBar.is_current)
    if start:
        query = query.where(DailyBar.session_date >= start)
    if end:
        query = query.where(DailyBar.session_date <= end)
    rows = db.session.scalars(query.order_by(DailyBar.session_date.desc()).limit(limit)).all()
    return list(reversed(rows))


def bar_count(symbol: str) -> int:
    return db.session.scalar(select(func.count()).select_from(DailyBar).where(
        DailyBar.source_id == SOURCE_ID, DailyBar.symbol == symbol, DailyBar.is_current)) or 0


def within_window(raw_window: str, now: datetime | None = None) -> bool:
    start, end = parse_window("PSXDATA_QUOTE_WINDOW", raw_window)
    local = (now or utcnow()).astimezone(PSX_TZ)
    return local.weekday() < 5 and start <= local.time() <= end


def bar_as_dict(bar: DailyBar) -> dict[str, Any]:
    return {"session_date": bar.session_date.isoformat(), "open": _s(bar.open), "high": _s(bar.high),
            "low": _s(bar.low), "close": _s(bar.close), "volume": bar.volume, "quality_flags": bar.quality_flags,
            "revision": bar.revision, "retrieved_at": _iso(bar.retrieved_at)}



def _s(value: Decimal | None) -> str | None:
    # Decimals are serialised as strings so clients never see binary floating-point artefacts.
    return None if value is None else str(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
