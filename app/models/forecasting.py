"""Forecasting tables: experiments, model registry, live forecasts, outcomes, ensemble weights,
model lifecycle events and daily pipeline runs.

Immutability rules:
  * `forecasts` rows are written before their outcome exists and never updated.
  * `forecast_outcomes` are separate rows written once the target session has a validated close.
  * `model_versions` rows are never deleted; status moves candidate -> approved -> retired (or rejected),
    and `model_events` records every transition for audit and rollback.
"""

from __future__ import annotations

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
from app.models.base import recorded_at_col, uuid_pk


class Experiment(db.Model):
    """A reproducible walk-forward evaluation: configuration in, metrics out."""

    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # models, horizons, windows, seeds
    data_version: Mapped[str] = mapped_column(String(64), nullable=False)
    code_version: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # running | completed | failed
    results: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # metric tables by split/model/horizon
    conclusion: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = recorded_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacktestPrediction(db.Model):
    """One out-of-sample prediction made inside an experiment (training data strictly before origin)."""

    __tablename__ = "backtest_predictions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"),
                                                     nullable=False)
    model_key: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    origin_date: Mapped[date] = mapped_column(Date, nullable=False)
    horizon: Mapped[int] = mapped_column(Integer, nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False)  # validation | holdout
    predicted_return: Mapped[float] = mapped_column(nullable=False)
    lower: Mapped[float | None] = mapped_column()
    upper: Mapped[float | None] = mapped_column()
    prob_up: Mapped[float | None] = mapped_column()
    actual_return: Mapped[float] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("experiment_id", "model_key", "symbol", "origin_date", "horizon"),
        Index("ix_backtest_lookup", "experiment_id", "model_key", "horizon", "split"),
    )


class ModelVersion(db.Model):
    """A trained model artifact in the registry (one per model_key x horizon x training run)."""

    __tablename__ = "model_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    model_key: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. ridge_tech
    horizon: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    model_type: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    features: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    hyperparameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    train_start: Mapped[date] = mapped_column(Date, nullable=False)
    train_end: Mapped[date] = mapped_column(Date, nullable=False)  # last origin whose label matured
    n_train: Mapped[int] = mapped_column(Integer, nullable=False)
    data_version: Mapped[str] = mapped_column(String(64), nullable=False)
    code_version: Mapped[str | None] = mapped_column(String(64))
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Out-of-sample evidence used to judge the candidate (from the evaluation window before train_end).
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Empirical residual quantiles used for the conformal prediction interval.
    interval_calibration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # candidate|approved|retired|rejected
    trained_at: Mapped[datetime] = recorded_at_col()
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("model_key", "horizon", "version"),
        # At most one approved version per model_key and horizon.
        Index("uq_model_versions_approved", "model_key", "horizon", unique=True,
              postgresql_where=text("status = 'approved'")),
        CheckConstraint("status IN ('candidate', 'approved', 'retired', 'rejected')", name="status_valid"),
    )


class ModelEvent(db.Model):
    """Append-only log of training, promotion, rejection, rollback and adaptation decisions."""

    __tablename__ = "model_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = recorded_at_col()
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_versions.id"))
    horizon: Mapped[int | None] = mapped_column(Integer)
    session_date: Mapped[date | None] = mapped_column(Date)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)  # applied|skipped|rejected|promoted|...
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index("ix_model_events_type_time", "event_type", "occurred_at"),)


class EnsembleWeights(db.Model):
    """Ensemble weights in force from `session_date` for a horizon, with the evidence used."""

    __tablename__ = "ensemble_weights"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    horizon: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)  # equal | inverse_mse_shrunk
    weights: Mapped[dict[str, float]] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    computed_at: Mapped[datetime] = recorded_at_col()

    __table_args__ = (UniqueConstraint("session_date", "horizon", "method"),)


class Forecast(db.Model):
    """An immutable live forecast, stored before its outcome is known."""

    __tablename__ = "forecasts"

    id: Mapped[uuid.UUID] = uuid_pk()
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    origin_date: Mapped[date] = mapped_column(Date, nullable=False)  # last completed session used
    horizon: Mapped[int] = mapped_column(Integer, nullable=False)  # in trading sessions
    model_key: Mapped[str] = mapped_column(String(64), nullable=False)  # a model, or "ensemble"
    model_version_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    ensemble_weights_id: Mapped[int | None] = mapped_column(ForeignKey("ensemble_weights.id"))
    feature_set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("feature_snapshots.id"))
    data_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # Price basis: the return target uses split/bonus-adjusted closes; at the origin the adjusted and
    # raw close coincide (no later actions can be known), so reference_price is the raw origin close.
    reference_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    predicted_return: Mapped[float] = mapped_column(nullable=False)  # mean estimate (simple return)
    median_return: Mapped[float | None] = mapped_column()
    lower_return: Mapped[float | None] = mapped_column()  # interval_level two-sided interval
    upper_return: Mapped[float | None] = mapped_column()
    interval_level: Mapped[float | None] = mapped_column()
    interval_status: Mapped[str] = mapped_column(String(32), nullable=False)  # calibrated|uncalibrated|...
    prob_up: Mapped[float | None] = mapped_column()
    prob_status: Mapped[str] = mapped_column(String(32), nullable=False)  # calibrated|not_calibrated|...
    implied_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    reliability: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = recorded_at_col()

    __table_args__ = (
        UniqueConstraint("symbol", "origin_date", "horizon", "model_key"),
        Index("ix_forecasts_symbol_origin", "symbol", "origin_date"),
    )


class ForecastOutcome(db.Model):
    __tablename__ = "forecast_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    forecast_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("forecasts.id"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # evaluated | invalid
    invalid_reason: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)  # e.g. corporate action inside the window
    target_date: Mapped[date | None] = mapped_column(Date)
    actual_return: Mapped[float | None] = mapped_column()  # adjusted basis
    actual_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))  # raw close at target
    error: Mapped[float | None] = mapped_column()  # predicted - actual
    direction_correct: Mapped[bool | None] = mapped_column(Boolean)
    in_interval: Mapped[bool | None] = mapped_column(Boolean)
    evaluated_at: Mapped[datetime] = recorded_at_col()


class PipelineRun(db.Model):
    """One execution of the daily research workflow for a market session."""

    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    session_date: Mapped[date | None] = mapped_column(Date)
    job_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("job_runs.id"))
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # running|succeeded|partial|failed|skipped
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    started_at: Mapped[datetime] = recorded_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_pipeline_one_running", "status", unique=True, postgresql_where=text("status = 'running'")),
    )
