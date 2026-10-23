"""Source registry, ingestion provenance and normalised market observations.

Layering (kept in separate tables on purpose):
  data_sources          what a source is and its runtime health
  ingestion_runs        one fetch from a source: when, what came back, what was kept
  ingestion_rejections  quarantined records that failed validation (never silently dropped)
  instruments           symbols and whether each is a stock or an index
  daily_bars            validated daily OHLCV with revision history (provider field names do not leak here)
  quote_snapshots       validated live quotes, append-only
Derived indicators, forecasts and evaluations live in later, separate tables.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import recorded_at_col, updated_at_col, uuid_pk


class DataSource(db.Model):
    __tablename__ = "data_sources"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_name: Mapped[str] = mapped_column(String(200), nullable=False)
    dataset_type: Mapped[str] = mapped_column(String(64), nullable=False)
    access_method: Mapped[str] = mapped_column(String(32), nullable=False)
    terms_reference: Mapped[str | None] = mapped_column(Text)
    license_status: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refresh_frequency: Mapped[str | None] = mapped_column(String(64))
    last_successful_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    data_quality_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    created_at: Mapped[datetime] = recorded_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class IngestionStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIALLY_SUCCEEDED = "partially_succeeded"
    FAILED = "failed"


class IngestionRun(db.Model):
    """One sync of one dataset from a source (which may span several symbols/requests).

    Counters: received = records returned; rejected = failed validation (quarantined);
    filtered = skipped by design (e.g. no quote exists for an index); accepted = valid records;
    stored = rows inserted (new or revised). Unchanged re-fetched records are not re-stored.
    Per-symbol outcomes are in request_metadata["symbols"].
    """

    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"), nullable=False)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    job_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("job_runs.id"))
    status: Mapped[IngestionStatus] = mapped_column(
        Enum(IngestionStatus, name="ingestion_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=IngestionStatus.RUNNING,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_filtered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(Text)
    # Safe-to-retain request facts: endpoints, HTTP statuses, attempts, provider cache flags,
    # per-symbol results. Never headers or credentials.
    request_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_ingestion_runs_source_dataset_started", "source_id", "dataset", "started_at"),
        Index("ix_ingestion_runs_status", "status"),
        # Overlap guard: at most one in-flight run per source and dataset.
        Index("uq_ingestion_runs_one_running", "source_id", "dataset", unique=True,
              postgresql_where=text("status = 'running'")),
    )


class IngestionRejection(db.Model):
    __tablename__ = "ingestion_rejections"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False
    )
    record_index: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    raw_record: Mapped[Any] = mapped_column(JSONB)  # truncated copy of the offending item
    recorded_at: Mapped[datetime] = recorded_at_col()

    __table_args__ = (Index("ix_ingestion_rejections_run", "ingestion_run_id"),)


class Instrument(db.Model):
    """Symbols seen from a source and whether each is an index or a stock."""

    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    instrument_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # "stock" | "index"
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"), nullable=False)
    first_seen_at: Mapped[datetime] = recorded_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (CheckConstraint("instrument_kind IN ('stock', 'index')", name="instrument_kind_valid"),)


class DailyBar(db.Model):
    """Daily OHLCV for one PSX session, as published by the source (unadjusted).

    Revisions: re-fetching a session whose values changed inserts a new row (revision + 1) and
    marks the old one `is_current = false`, so evidence behind earlier analysis is never
    rewritten. `quality_flags` marks suspect-but-plausible bars (e.g. the provider's own
    anomaly flag, close outside [low, high]); analysis can exclude them explicitly.
    """

    __tablename__ = "daily_bars"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)  # PSX calendar date
    open: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quality_flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = recorded_at_col()

    __table_args__ = (
        UniqueConstraint("source_id", "symbol", "session_date", "revision"),
        Index("uq_daily_bars_current", "source_id", "symbol", "session_date", unique=True,
              postgresql_where=text("is_current")),
        Index("ix_daily_bars_symbol_date", "symbol", "session_date"),
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="prices_positive"),
        CheckConstraint("high >= low", name="high_not_below_low"),
        CheckConstraint("volume >= 0", name="volume_not_negative"),
    )


class QuoteSnapshot(db.Model):
    """A live quote as reported by the source when retrieved. The source supplies no exchange
    timestamp, so `retrieved_at` is the observation time; quotes may lag (provider caching)."""

    __tablename__ = "quote_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    change: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))  # percent units
    ldcp: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))  # last day close price
    pe_ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    dividend_yield: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    volume_avg_30d: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    change_1y_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    provider_cached: Mapped[bool] = mapped_column(Boolean, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = recorded_at_col()

    __table_args__ = (
        UniqueConstraint("ingestion_run_id", "symbol"),
        CheckConstraint("price > 0", name="price_positive"),
        Index("ix_quote_snapshots_symbol_retrieved", "symbol", "retrieved_at"),
    )
