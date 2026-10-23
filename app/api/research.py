"""Research API: analysis, news/sentiment, forecasts, experiments, models, data quality, pipeline.

Read endpoints serve persisted results (or cheap deterministic computations on stored bars).
Mutating endpoints are admin-only and enqueue background jobs; none trains a model or calls a
provider inside the request.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from flask import current_app, g
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from app.api import api_v1
from app.api.auth import require_admin
from app.api.responses import ApiError, ok
from app.api.schemas import JobOut, PaginationParams
from app.api.validation import parse_body, parse_query
from app.extensions import db
from app.models.forecasting import BacktestPrediction, Experiment, Forecast, PipelineRun
from app.models.operations import JobRun
from app.models.research import CorporateAction, DataQualityIssue
from app.services.orchestration import jobs
from app.services.research import views
from app.services.research.features import feature_docs
from app.utils.time import utcnow

DISCLAIMER = ("Research estimates with quantified uncertainty, not investment advice. Out-of-sample tests have "
              "not shown forecasting skill over the no-change baseline; see /api/v1/experiments.")


def _symbol(symbol: str) -> str:
    s = symbol.upper()
    if s not in current_app.config["PSXDATA_SYMBOLS"]:
        raise ApiError(404, "not_found", f"{s} is not a tracked symbol")
    return s


def _uuid(value: str, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ApiError(400, "invalid_id", f"{name} must be a UUID") from None


class RangeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date | None = None
    end: date | None = None
    limit: int = Field(default=500, ge=1, le=5000)


class ForecastListParams(PaginationParams):
    symbol: str | None = Field(default=None, max_length=16)
    horizon: int | None = Field(default=None, ge=1, le=60)
    model: str | None = Field(default=None, max_length=64, pattern=r"^[a-z0-9_]+$")
    evaluated: bool | None = None


class PredictionParams(PaginationParams):
    model: str = Field(max_length=64, pattern=r"^[a-z0-9_]+$")
    horizon: int = Field(ge=1, le=60)
    symbol: str | None = Field(default=None, max_length=16)
    split: Literal["validation", "holdout"] | None = None


class SentimentParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["dedup_mean_v1", "weighted_v1"] = "dedup_mean_v1"


class IssueParams(PaginationParams):
    severity: Literal["info", "warning", "error"] | None = None
    symbol: str | None = Field(default=None, max_length=16)
    open_only: bool = True


@api_v1.get("/market/overview")
def market_overview():
    data = views.market_overview(current_app.config)
    return ok(data, meta={"note": data["coverage"]["note"], "disclaimer": DISCLAIMER})


@api_v1.get("/stocks/<symbol>/indicators")
def stock_indicators(symbol: str):
    p = parse_query(RangeParams)
    return ok(views.indicator_series(_symbol(symbol), p.start, p.end, p.limit),
              meta={"price_basis": "split/bonus-adjusted (cash dividends not adjusted)", "features": feature_docs()})


@api_v1.get("/stocks/<symbol>/analysis")
def stock_analysis(symbol: str):
    data = views.analysis_for(_symbol(symbol))
    return ok(data, meta={"method_version": data.get("method_version"), "source": data.get("source")},
              warnings=data.get("caveats", [])[:3])


@api_v1.get("/stocks/<symbol>/news")
def stock_news(symbol: str):
    s = symbol.upper()
    if s != "MARKET":
        s = _symbol(s)
    p = parse_query(PaginationParams)
    rows, total = views.news_for(s, p.page, p.page_size)
    return ok(rows, meta={"page": p.page, "page_size": p.page_size, "total": total,
                          "note": "Headline tone only; not an estimate of price impact. Source: Google News RSS "
                                  "(discovery), links are provider redirects."})


@api_v1.get("/stocks/<symbol>/sentiment")
def stock_sentiment(symbol: str):
    s = symbol.upper()
    if s != "MARKET":
        s = _symbol(s)
    method = parse_query(SentimentParams).method
    return ok(views.sentiment_daily(s, method), meta={"method": method, "window_days": 7,
                                                       "insufficient": "no relevant event in the window"})


@api_v1.get("/stocks/<symbol>/forecasts")
def stock_forecasts(symbol: str):
    s = _symbol(symbol)
    return ok(views.latest_forecasts(s), meta={"disclaimer": DISCLAIMER})


@api_v1.get("/forecasts")
def list_forecasts():
    p = parse_query(ForecastListParams)
    q = views.forecasts_query(p.symbol.upper() if p.symbol else None, p.horizon, p.model)
    if p.evaluated is not None:
        from app.models.forecasting import ForecastOutcome

        q = q.where(ForecastOutcome.id.is_not(None) if p.evaluated else ForecastOutcome.id.is_(None))
    total = db.session.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.session.execute(q.order_by(Forecast.origin_date.desc(), Forecast.symbol, Forecast.horizon)
                              .offset((p.page - 1) * p.page_size).limit(p.page_size)).all()
    return ok([views.forecast_dict(f, o) for f, o in rows],
              meta={"page": p.page, "page_size": p.page_size, "total": total, "disclaimer": DISCLAIMER})


@api_v1.get("/forecasts/<forecast_id>")
def get_forecast(forecast_id: str):
    row = db.session.execute(views.forecasts_query().where(Forecast.id == _uuid(forecast_id, "forecast_id"))).first()
    if row is None:
        raise ApiError(404, "not_found", "Forecast not found")
    return ok(views.forecast_dict(*row))


@api_v1.get("/forecasts/performance")
def forecast_performance():
    exp = db.session.scalar(select(Experiment).where(Experiment.name == "tech_walkforward_v1")
                            .order_by(Experiment.finished_at.desc()).limit(1))
    return ok({"live": views.live_performance_table(), "walk_forward": views.experiment_summary(exp),
               "walk_forward_metrics": (exp.results or {}).get("by_model") if exp else None},
              meta={"note": "live = matured stored forecasts; walk_forward = out-of-sample backtest"})


@api_v1.get("/experiments")
def list_experiments():
    p = parse_query(PaginationParams)
    q = select(Experiment)
    total = db.session.scalar(select(func.count()).select_from(Experiment))
    rows = db.session.scalars(q.order_by(Experiment.started_at.desc()).offset((p.page - 1) * p.page_size)
                              .limit(p.page_size)).all()
    return ok([views.experiment_summary(e) for e in rows], meta={"page": p.page, "page_size": p.page_size,
                                                                 "total": total})


@api_v1.get("/experiments/<experiment_id>")
def get_experiment(experiment_id: str):
    exp = db.session.get(Experiment, _uuid(experiment_id, "experiment_id"))
    if exp is None:
        raise ApiError(404, "not_found", "Experiment not found")
    return ok({**views.experiment_summary(exp), "results": exp.results})


@api_v1.get("/experiments/<experiment_id>/predictions")
def experiment_predictions(experiment_id: str):
    eid = _uuid(experiment_id, "experiment_id")
    p = parse_query(PredictionParams)
    q = select(BacktestPrediction).where(BacktestPrediction.experiment_id == eid,
                                         BacktestPrediction.model_key == p.model,
                                         BacktestPrediction.horizon == p.horizon)
    if p.symbol:
        q = q.where(BacktestPrediction.symbol == p.symbol.upper())
    if p.split:
        q = q.where(BacktestPrediction.split == p.split)
    total = db.session.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.session.scalars(q.order_by(BacktestPrediction.origin_date.desc(), BacktestPrediction.symbol)
                              .offset((p.page - 1) * p.page_size).limit(p.page_size)).all()
    return ok([{"symbol": r.symbol, "origin_date": r.origin_date.isoformat(), "split": r.split,
                "predicted_return": r.predicted_return, "lower": r.lower, "upper": r.upper, "prob_up": r.prob_up,
                "actual_return": r.actual_return} for r in rows],
              meta={"page": p.page, "page_size": p.page_size, "total": total})


@api_v1.get("/models")
def list_models():
    return ok(views.model_overview())


@api_v1.get("/data-quality/issues")
def data_quality_issues():
    p = parse_query(IssueParams)
    q = select(DataQualityIssue)
    if p.open_only:
        q = q.where(DataQualityIssue.resolved_at.is_(None))
    if p.severity:
        q = q.where(DataQualityIssue.severity == p.severity)
    if p.symbol:
        q = q.where(DataQualityIssue.symbol == p.symbol.upper())
    total = db.session.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.session.scalars(q.order_by(DataQualityIssue.session_date.desc().nulls_first())
                              .offset((p.page - 1) * p.page_size).limit(p.page_size)).all()
    return ok([{"check": i.check_name, "symbol": i.symbol, "session_date": i.session_date.isoformat() if i.session_date
                else None, "severity": i.severity, "message": i.message, "details": i.details,
                "first_detected_at": i.first_detected_at.isoformat(),
                "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None} for i in rows],
              meta={"page": p.page, "page_size": p.page_size, "total": total})


@api_v1.get("/corporate-actions")
def corporate_actions():
    rows = db.session.scalars(select(CorporateAction).order_by(CorporateAction.symbol, CorporateAction.ex_date)).all()
    return ok([{"symbol": a.symbol, "ex_date": a.ex_date.isoformat(), "type": a.action_type,
                "share_multiplier": str(a.share_multiplier),
                "cash_dividend_per_share": str(a.cash_dividend_per_share) if a.cash_dividend_per_share else None,
                "status": a.status, "price_check": a.price_check, "price_check_detail": a.price_check_detail,
                "evidence": a.evidence, "sources": a.sources} for a in rows],
              meta={"method": "historical prices divided by the cumulative share multiplier of later actions"})


@api_v1.get("/pipeline/runs")
def pipeline_runs():
    p = parse_query(PaginationParams)
    total = db.session.scalar(select(func.count()).select_from(PipelineRun))
    rows = db.session.scalars(select(PipelineRun).order_by(PipelineRun.started_at.desc())
                              .offset((p.page - 1) * p.page_size).limit(p.page_size)).all()
    return ok([{"id": str(r.id), "session_date": r.session_date.isoformat() if r.session_date else None,
                "status": r.status, "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None, "steps": r.steps} for r in rows],
              meta={"page": p.page, "page_size": p.page_size, "total": total})


@api_v1.get("/research/health")
def research_health():
    return ok(views.research_health())


class PipelineTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False
    skip_ingestion: bool = False


class RefreshTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TrainingTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")

    @field_validator("actor")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


def _enqueue(job_type: str, params: dict) -> tuple:
    active = jobs.active_job(job_type)
    if active is not None:
        raise ApiError(409, "already_running", f"A {job_type} job is already queued or running",
                       {"job_id": str(active.id)})
    result = jobs.submit_job(job_type, parameters=params, requested_by=g.actor)
    return ok(JobOut.model_validate(result.job).model_dump(mode="json"), meta={"poll": f"/api/v1/jobs/{result.job.id}"},
              status=202)


@api_v1.post("/pipeline/runs")
@require_admin
def trigger_pipeline():
    body = parse_body(PipelineTrigger)
    return _enqueue("pipeline.daily", body.model_dump())


@api_v1.post("/models/training-runs")
@require_admin
def trigger_training():
    body = parse_body(TrainingTrigger)
    if not current_app.config["ENABLE_MODEL_TRAINING"]:
        raise ApiError(409, "training_disabled", "ENABLE_MODEL_TRAINING is false")
    return _enqueue("models.train", {"actor": body.actor})


@api_v1.post("/refresh")
@require_admin
def trigger_refresh():
    """Enqueue a refresh (latest quotes, then bars/news/analysis/forecasts via the pipeline).

    Idempotent while one is in flight: returns the active job (200) instead of starting another.
    """
    from app.services.refresh import REFRESH_COOLDOWN_SECONDS, REFRESH_JOB

    parse_body(RefreshTrigger)
    active = jobs.active_job(REFRESH_JOB)
    if active is not None:
        return ok(JobOut.model_validate(active).model_dump(mode="json"),
                  meta={"poll": f"/api/v1/jobs/{active.id}", "deduplicated": True},
                  warnings=["A refresh is already queued or running; returning it"])
    last = db.session.scalar(select(JobRun).where(JobRun.job_type == REFRESH_JOB)
                             .order_by(JobRun.created_at.desc()).limit(1))
    if last is not None:
        elapsed = (utcnow() - last.created_at).total_seconds()
        if elapsed < REFRESH_COOLDOWN_SECONDS:
            raise ApiError(429, "refresh_cooldown", f"Last refresh was requested {int(elapsed)}s ago; wait "
                                                    f"{REFRESH_COOLDOWN_SECONDS}s between refreshes",
                           {"job_id": str(last.id), "retry_after_seconds": int(REFRESH_COOLDOWN_SECONDS - elapsed)})
    result = jobs.submit_job(REFRESH_JOB, parameters={}, requested_by=g.actor)
    return ok(JobOut.model_validate(result.job).model_dump(mode="json"),
              meta={"poll": f"/api/v1/jobs/{result.job.id}", "deduplicated": False}, status=202)

