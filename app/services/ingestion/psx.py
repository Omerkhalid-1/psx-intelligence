"""PSX market-data ingestion via psxdata-api: daily OHLCV bars and live quotes.

Guarantees (both datasets):
  * At most one running sync per dataset: a partial unique index on
    ingestion_runs(source_id, dataset) WHERE status='running' rejects a concurrent run; a run
    left 'running' for ABANDONED_AFTER (dead worker) is marked failed and taken over.
  * Requests are paced (PSXDATA_REQUEST_SPACING_SECONDS, default 1s) to stay under
    psxdata-api's 60 requests/minute limit; a service outage aborts the remaining symbols.
  * Each symbol commits on its own, so one bad symbol never discards another's data; a database
    error rolls back only that symbol and is recorded.
  * Daily bars are keyed (source, symbol, session_date). Unchanged re-fetches store nothing;
    changed values add a new revision and retire the old row (history is never overwritten).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models.market_data import (
    DailyBar,
    DataSource,
    IngestionRejection,
    IngestionRun,
    IngestionStatus,
    Instrument,
    QuoteSnapshot,
)
from app.services.ingestion.providers.base import (
    DailyBarObservation,
    MarketDataProvider,
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
    QuoteObservation,
    RejectedRecord,
)
from app.services.ingestion.providers.psxdata_api import SOURCE_ID, PsxDataApiClient
from app.utils.logging import log_context, redact
from app.utils.time import utcnow

log = logging.getLogger(__name__)

DATASET_BARS = "daily_bars"
DATASET_QUOTES = "quotes"
DATASETS = (DATASET_BARS, DATASET_QUOTES)
ABANDONED_AFTER = timedelta(minutes=60)
MAX_STORED_REJECTIONS = 200

# Patchable pacing clock (tests replace it so they do not actually sleep).
_pace: Callable[[float], None] = time.sleep


class IngestionAlreadyRunning(Exception):
    pass


SOURCE = {
    "source_name": "psxdata-api (self-hosted; data read from the PSX Data Portal)",
    "dataset_type": "daily_bars,quotes",
    "access_method": "self_hosted_wrapper",
    "terms_reference": "psxdata-api v0.5.0 / psxdata 1.2.0 (MIT). Upstream: dps.psx.com.pk, whose terms restrict "
                       "automated access and reuse; see docs/data_sources.md",
    "license_status": "owner_accepted_risk",
}


def ensure_source(config: Mapping[str, Any]) -> DataSource:
    source = db.session.get(DataSource, SOURCE_ID)
    if source is None:
        source = DataSource(source_id=SOURCE_ID, data_quality_status="unknown")
        db.session.add(source)
    for key, value in SOURCE.items():
        setattr(source, key, value)
    source.enabled = bool(config["PSXDATA_ENABLED"])
    # Fits data_sources.refresh_frequency (varchar 64).
    interval, window = config["PSXDATA_QUOTE_POLL_INTERVAL_MINUTES"], config["PSXDATA_QUOTE_WINDOW"]
    source.refresh_frequency = f"bars Mon-Fri {config['PSXDATA_DAILY_SYNC_TIME']}; quotes {interval}m {window} (PKT)"
    return source


@dataclass
class _Totals:
    received: int = 0
    accepted: int = 0
    rejected: int = 0
    filtered: int = 0
    stored: int = 0


# -- daily bars -----------------------------------------------------------------------------------


def run_daily_bar_ingestion(
    config: Mapping[str, Any],
    *,
    full: bool = False,
    symbols: tuple[str, ...] | None = None,
    job_run_id: uuid.UUID | None = None,
    provider: MarketDataProvider | None = None,
) -> IngestionRun:
    symbols = symbols or tuple(config["PSXDATA_SYMBOLS"])
    client = provider or PsxDataApiClient.from_config(config)
    spacing = float(config["PSXDATA_REQUEST_SPACING_SECONDS"])
    run = _start_run(config, DATASET_BARS, job_run_id, {"symbols": list(symbols), "full_history": full})
    totals, per_symbol, aborted = _Totals(), {}, None

    with log_context(source_id=SOURCE_ID, run_id=str(run.id)):
        _sync_instrument_kinds(client, symbols, run)
        for symbol in symbols:
            if aborted:
                per_symbol[symbol] = {"status": "skipped", "reason": aborted}
                continue
            _pace(spacing)  # the index lookup (or previous symbol) just made a request
            start = _bars_start(config, symbol, full)
            try:
                result = client.fetch_daily_bars(symbol, start=start)
            except ProviderError as exc:
                per_symbol[symbol] = _symbol_failure(exc)
                if isinstance(exc, ProviderUnavailable | ProviderRateLimited):
                    aborted = f"aborted after {symbol}: {exc.category}"
                continue
            totals.received += result.received
            totals.rejected += len(result.rejected)
            entry = {"status": "ok", "start": start.isoformat() if start else None, "received": result.received,
                     "rejected": len(result.rejected), "provider_cached": result.provider_cached}
            try:
                inserted, revised, unchanged = _store_bars(run.id, symbol, result.observations, result.rejected)
            except SQLAlchemyError as exc:
                db.session.rollback()
                log.exception("storing bars failed", extra={"symbol": symbol, "error_category": "database_error"})
                per_symbol[symbol] = {"status": "failed", "error_category": "database_error",
                                      "error": type(exc).__name__}
                continue
            totals.accepted += len(result.observations)
            totals.stored += inserted + revised
            entry.update(accepted=len(result.observations), inserted=inserted, revised=revised, unchanged=unchanged,
                         flagged=sum(1 for o in result.observations if o.quality_flags),
                         latest_session=result.observations[-1].session_date.isoformat()
                         if result.observations else None)
            per_symbol[symbol] = entry

    return _finish(run.id, totals, per_symbol)


def _bars_start(config: Mapping[str, Any], symbol: str, full: bool) -> date | None:
    backfill = config.get("PSXDATA_BACKFILL_START")
    backfill_start = date.fromisoformat(backfill) if backfill else None
    if full:
        return backfill_start
    latest = db.session.scalar(
        select(DailyBar.session_date)
        .where(DailyBar.source_id == SOURCE_ID, DailyBar.symbol == symbol, DailyBar.is_current)
        .order_by(DailyBar.session_date.desc()).limit(1))
    if latest is None:
        return backfill_start  # first sync for this symbol is a backfill
    return latest - timedelta(days=int(config["PSXDATA_INCREMENTAL_OVERLAP_DAYS"]))


def _store_bars(run_id: uuid.UUID, symbol: str, observations: list[DailyBarObservation],
                rejected: list[RejectedRecord]) -> tuple[int, int, int]:
    now = utcnow()
    inserted = revised = unchanged = 0
    if observations:
        existing = {b.session_date: b for b in db.session.scalars(
            select(DailyBar).where(
                DailyBar.source_id == SOURCE_ID, DailyBar.symbol == symbol, DailyBar.is_current,
                DailyBar.session_date.between(observations[0].session_date, observations[-1].session_date)))}
        for obs in observations:
            current = existing.get(obs.session_date)
            if current is not None and _same_bar(current, obs):
                unchanged += 1
                continue
            revision = 1
            if current is not None:
                current.is_current = False
                current.superseded_at = now
                db.session.flush()  # retire the old row before inserting its replacement
                revision = current.revision + 1
                revised += 1
            else:
                inserted += 1
            db.session.add(DailyBar(
                source_id=SOURCE_ID, symbol=symbol, session_date=obs.session_date, open=obs.open, high=obs.high,
                low=obs.low, close=obs.close, volume=obs.volume, quality_flags=list(obs.quality_flags),
                revision=revision, is_current=True, ingestion_run_id=run_id, retrieved_at=now))
    _add_rejections(run_id, rejected)
    db.session.commit()
    return inserted, revised, unchanged


def _same_bar(bar: DailyBar, obs: DailyBarObservation) -> bool:
    return ((bar.open, bar.high, bar.low, bar.close, bar.volume, sorted(bar.quality_flags or []))
            == (obs.open, obs.high, obs.low, obs.close, obs.volume, sorted(obs.quality_flags)))


def _sync_instrument_kinds(client: MarketDataProvider, symbols: tuple[str, ...], run: IngestionRun) -> None:
    """Classify symbols as index or stock from the provider's index list (one request)."""
    try:
        index_names = set(client.fetch_index_names())
    except ProviderError as exc:
        _merge_metadata(run.id, {"index_lookup": f"failed: {exc.category}"})
        return
    for symbol in symbols:
        kind = "index" if symbol in index_names else "stock"
        instrument = db.session.get(Instrument, symbol)
        if instrument is None:
            db.session.add(Instrument(symbol=symbol, instrument_kind=kind, source_id=SOURCE_ID))
        else:
            instrument.instrument_kind = kind
    db.session.commit()
    _merge_metadata(run.id, {"index_lookup": "ok"})


# -- quotes -------------------------------------------------------------------------------------------------


def run_quote_ingestion(
    config: Mapping[str, Any],
    *,
    symbols: tuple[str, ...] | None = None,
    job_run_id: uuid.UUID | None = None,
    provider: MarketDataProvider | None = None,
) -> IngestionRun:
    symbols = symbols or tuple(config["PSXDATA_SYMBOLS"])
    client = provider or PsxDataApiClient.from_config(config)
    spacing = float(config["PSXDATA_REQUEST_SPACING_SECONDS"])
    run = _start_run(config, DATASET_QUOTES, job_run_id, {"symbols": list(symbols)})
    kinds = dict(db.session.execute(select(Instrument.symbol, Instrument.instrument_kind)).all())
    totals, per_symbol, aborted, requested = _Totals(), {}, None, 0

    with log_context(source_id=SOURCE_ID, run_id=str(run.id)):
        for symbol in symbols:
            if aborted:
                per_symbol[symbol] = {"status": "skipped", "reason": aborted}
                continue
            if kinds.get(symbol) == "index":
                # psxdata-api has no quote for indices (404); index levels come from daily bars.
                totals.filtered += 1
                per_symbol[symbol] = {"status": "not_applicable", "reason": "no quotes for indices"}
                continue
            if requested:
                _pace(spacing)
            requested += 1
            try:
                result = client.fetch_quote(symbol)
            except ProviderNotFound:
                # psxdata-api builds quotes from the PSX screener, which does not cover every listed symbol.
                # A coverage gap is recorded (and shown) but is not a failed fetch; daily bars are unaffected.
                totals.filtered += 1
                per_symbol[symbol] = {"status": "not_available", "reason": "no live quote from psxdata-api"}
                continue
            except ProviderError as exc:
                per_symbol[symbol] = _symbol_failure(exc)
                if isinstance(exc, ProviderUnavailable | ProviderRateLimited):
                    aborted = f"aborted after {symbol}: {exc.category}"
                continue
            totals.received += result.received
            totals.rejected += len(result.rejected)
            totals.accepted += len(result.observations)
            try:
                stored = _store_quotes(run.id, result.observations, result.rejected, bool(result.provider_cached))
            except SQLAlchemyError as exc:
                db.session.rollback()
                per_symbol[symbol] = {"status": "failed", "error_category": "database_error",
                                      "error": type(exc).__name__}
                continue
            totals.stored += stored
            per_symbol[symbol] = {"status": "ok" if result.observations else "rejected", "stored": stored,
                                  "unchanged": len(result.observations) - stored,
                                  "provider_cached": result.provider_cached}

    return _finish(run.id, totals, per_symbol)


_QUOTE_FIELDS = ("price", "change", "change_pct", "ldcp", "pe_ratio", "dividend_yield", "market_cap",
                 "volume_avg_30d", "change_1y_pct")


def _store_quotes(run_id: uuid.UUID, observations: list[QuoteObservation], rejected: list[RejectedRecord],
                  provider_cached: bool) -> int:
    stored = 0
    now = utcnow()
    for obs in observations:
        latest = db.session.scalar(
            select(QuoteSnapshot).where(QuoteSnapshot.source_id == SOURCE_ID, QuoteSnapshot.symbol == obs.symbol)
            .order_by(QuoteSnapshot.retrieved_at.desc()).limit(1))
        if latest is not None and all(getattr(latest, f) == getattr(obs, f) for f in _QUOTE_FIELDS):
            continue  # unchanged since the last stored quote; the run itself records the check
        db.session.add(QuoteSnapshot(ingestion_run_id=run_id, source_id=SOURCE_ID, provider_cached=provider_cached,
                                     retrieved_at=now, symbol=obs.symbol,
                                     **{f: getattr(obs, f) for f in _QUOTE_FIELDS}))
        stored += 1
    _add_rejections(run_id, rejected)
    db.session.commit()
    return stored


# -- run lifecycle --------------------------------------------------------------------------------------------


def _start_run(config: Mapping[str, Any], dataset: str, job_run_id: uuid.UUID | None,
               metadata: dict[str, Any]) -> IngestionRun:
    source = ensure_source(config)
    now = utcnow()
    db.session.execute(
        update(IngestionRun)
        .where(IngestionRun.source_id == SOURCE_ID, IngestionRun.dataset == dataset,
               IngestionRun.status == IngestionStatus.RUNNING, IngestionRun.started_at < now - ABANDONED_AFTER)
        .values(status=IngestionStatus.FAILED, finished_at=now, error_category="abandoned",
                error_summary="run did not finish (worker stopped); superseded by a later run"))
    run = IngestionRun(id=uuid.uuid4(), source_id=SOURCE_ID, dataset=dataset, job_run_id=job_run_id,
                       status=IngestionStatus.RUNNING, started_at=now, request_metadata=metadata)
    db.session.add(run)
    source.last_attempted_sync = now
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise IngestionAlreadyRunning(f"a {dataset} sync is already running") from None
    log.info("ingestion started", extra={"event_type": "ingestion_started", "source_id": SOURCE_ID,
                                         "run_id": str(run.id), "details": {"dataset": dataset, **metadata}})
    return run


def _finish(run_id: uuid.UUID, totals: _Totals, per_symbol: dict[str, dict[str, Any]]) -> IngestionRun:
    db.session.rollback()
    run = db.session.get(IngestionRun, run_id)
    source = db.session.get(DataSource, SOURCE_ID)
    run.records_received, run.records_accepted = totals.received, totals.accepted
    run.records_rejected, run.records_filtered, run.records_stored = totals.rejected, totals.filtered, totals.stored
    run.request_metadata = {**run.request_metadata, "symbols": per_symbol}

    failed = {s: r for s, r in per_symbol.items() if r["status"] in ("failed", "skipped", "rejected")}
    ok = [s for s, r in per_symbol.items() if r["status"] == "ok"]
    problems = []
    if failed:
        problems.append("; ".join(f"{s}: {r.get('error_category') or r.get('reason') or r['status']}"
                                  for s, r in failed.items()))
    if totals.rejected:
        problems.append(f"{totals.rejected} record(s) rejected")

    if not ok:
        status = IngestionStatus.FAILED
        categories = {r.get("error_category") for r in failed.values() if r.get("error_category")}
        run.error_category = categories.pop() if len(categories) == 1 else "all_symbols_failed"
    elif problems:
        status, run.error_category = IngestionStatus.PARTIALLY_SUCCEEDED, "partial"
    else:
        status, run.error_category = IngestionStatus.SUCCEEDED, None
    run.status = status
    unavailable = sorted(s for s, r in per_symbol.items() if r["status"] == "not_available")
    notes = [f"no live quote available for: {', '.join(unavailable)}"] if unavailable else []
    run.error_summary = redact("; ".join(problems + notes))[:2000] or None
    run.finished_at = utcnow()
    if status != IngestionStatus.FAILED:
        source.last_successful_sync = run.finished_at
    source.last_error = redact("; ".join(problems))[:2000] or None
    source.data_quality_status = {IngestionStatus.SUCCEEDED: "ok", IngestionStatus.PARTIALLY_SUCCEEDED: "degraded",
                                  IngestionStatus.FAILED: "failing"}[status]
    db.session.commit()
    log.log(logging.INFO if status == IngestionStatus.SUCCEEDED else logging.WARNING, "ingestion finished", extra={
        "event_type": "ingestion_failed" if status == IngestionStatus.FAILED else "ingestion_completed",
        "source_id": SOURCE_ID, "run_id": str(run.id), "status": status.value, "error_category": run.error_category,
        "details": {"dataset": run.dataset, "received": totals.received, "accepted": totals.accepted,
                    "rejected": totals.rejected, "stored": totals.stored, "summary": run.error_summary}})
    return run


def _symbol_failure(exc: ProviderError) -> dict[str, Any]:
    return {"status": "failed", "error_category": exc.category, "error": redact(str(exc))[:300],
            "http_status": exc.metadata.get("http_status"), "attempts": exc.metadata.get("attempts")}


def _add_rejections(run_id: uuid.UUID, rejected: list[RejectedRecord]) -> None:
    already = db.session.query(IngestionRejection).filter_by(ingestion_run_id=run_id).count()
    for rec in rejected[: max(0, MAX_STORED_REJECTIONS - already)]:
        db.session.add(IngestionRejection(ingestion_run_id=run_id, record_index=rec.index, symbol=rec.symbol,
                                          reason=rec.reason[:300], raw_record=rec.raw))


def _merge_metadata(run_id: uuid.UUID, values: dict[str, Any]) -> None:
    run = db.session.get(IngestionRun, run_id)
    run.request_metadata = {**run.request_metadata, **values}
    db.session.commit()
