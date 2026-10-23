"""Read models for the research API and dashboard. Everything here reads persisted results or
cheap deterministic computations over stored bars; nothing calls a provider or trains a model."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select

from app.extensions import db
from app.models.forecasting import (
    EnsembleWeights,
    Experiment,
    Forecast,
    ForecastOutcome,
    ModelEvent,
    ModelVersion,
    PipelineRun,
)
from app.models.research import (
    AnalysisSnapshot,
    CorporateAction,
    DataQualityIssue,
    NewsArticle,
    NewsMention,
    NewsSentimentDaily,
)
from app.services.forecasting import registry
from app.services.news.relevance import RELEVANCE_THRESHOLD
from app.services.research.analysis import compute_indicator_frame, summarize
from app.services.research.prices import BENCHMARK, PriceFrame, load_universe

SENTIMENT_METHOD = "dedup_mean_v1"


def _f(x: Any, nd: int = 6) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if np.isfinite(v) else None


def analysis_for(symbol: str, frames: dict[str, PriceFrame] | None = None) -> dict[str, Any]:
    """Latest stored research summary; computed on the fly (cheap, deterministic) only if the latest
    session has not been analysed yet, and flagged as such."""
    frames = frames or load_universe((symbol,))
    pf = frames[symbol]
    stored = db.session.scalar(select(AnalysisSnapshot).where(AnalysisSnapshot.symbol == symbol)
                               .order_by(AnalysisSnapshot.session_date.desc()).limit(1))
    if stored is not None and pf.latest_session and stored.session_date >= pf.latest_session:
        return {**stored.summary, "source": "stored", "computed_at": stored.computed_at.isoformat()}
    out = summarize(pf, frames[BENCHMARK], quality_caveats=quality_caveats(symbol))
    return {**out, "source": "computed_on_request"}


def quality_caveats(symbol: str) -> list[str]:
    issues = db.session.execute(select(DataQualityIssue.check_name, DataQualityIssue.severity, func.count())
                                .where(DataQualityIssue.symbol == symbol, DataQualityIssue.resolved_at.is_(None),
                                       DataQualityIssue.severity.in_(["error", "warning"]))
                                .group_by(DataQualityIssue.check_name, DataQualityIssue.severity)).all()
    caveats = [f"{n} open {sev} finding(s): {check.replace('_', ' ')}" for check, sev, n in issues]
    caveats.append("Prices are split/bonus-adjusted from cited corporate actions; cash dividends are not adjusted.")
    return caveats


def indicator_series(symbol: str, start: date | None, end: date | None, limit: int) -> dict[str, Any]:
    frames = load_universe((symbol,))
    if frames[symbol].frame.empty or BENCHMARK not in frames or frames[BENCHMARK].frame.empty:
        return {"dates": [], "series": {}, "actions": []}
    x = compute_indicator_frame(frames[symbol], frames[BENCHMARK])
    if start:
        x = x[x.index >= pd.Timestamp(start)]
    if end:
        x = x[x.index <= pd.Timestamp(end)]
    x = x.iloc[-limit:]
    cols = [c for c in ("open", "high", "low", "close", "raw_close", "volume", "sma20", "sma50", "sma200", "ema20",
                        "rsi14", "macd", "signal", "hist", "atr14", "vol20", "drawdown", "rel_volume20", "rs_ratio",
                        "beta", "corr") if c in x]
    return {"dates": [d.date().isoformat() for d in x.index],
            "series": {c: [_f(v) for v in x[c].to_numpy()] for c in cols},
            "actions": [{"ex_date": a.ex_date.isoformat(), "type": a.action_type,
                         "multiplier": str(a.share_multiplier)} for a in db.session.scalars(
                select(CorporateAction).where(CorporateAction.symbol == symbol))]}


def news_for(symbol: str, page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
    q = (select(NewsArticle, NewsMention).join(NewsMention, NewsMention.article_id == NewsArticle.id)
         .where(NewsMention.symbol == symbol, NewsMention.relevance >= RELEVANCE_THRESHOLD))
    total = db.session.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.session.execute(q.order_by(NewsArticle.published_at.desc().nulls_last())
                              .offset((page - 1) * page_size).limit(page_size)).all()
    return [article_dict(a, m) for a, m in rows], total


def article_dict(a: NewsArticle, m: NewsMention) -> dict[str, Any]:
    return {"id": a.id, "title": a.title, "publisher": a.publisher, "publisher_domain": a.publisher_domain,
            "url": a.url, "published_at": a.published_at.isoformat() if a.published_at else None,
            "published_precision": a.published_precision, "available_at": a.available_at.isoformat(),
            "backfilled": a.backfilled, "cluster_id": a.cluster_id, "is_cluster_lead": a.cluster_id in (None, a.id),
            "symbol": m.symbol, "relevance": float(m.relevance), "relevance_reason": m.relevance_reason,
            "event_category": m.event_category, "sentiment_label": m.sentiment_label,
            "sentiment_score": _f(m.sentiment_score, 4), "sentiment_confidence": _f(m.sentiment_confidence, 4),
            "sentiment_model": m.sentiment_model, "explanation": m.explanation}


def sentiment_daily(symbol: str, method: str = SENTIMENT_METHOD, limit: int = 250) -> list[dict[str, Any]]:
    rows = db.session.scalars(select(NewsSentimentDaily).where(NewsSentimentDaily.symbol == symbol,
                                                               NewsSentimentDaily.method == method)
                              .order_by(NewsSentimentDaily.session_date.desc()).limit(limit)).all()
    return [{"date": r.session_date.isoformat(), "score": _f(r.score, 4), "label": r.label, "n_events": r.n_events,
             "n_articles": r.n_articles} for r in reversed(rows)]


def forecast_dict(f: Forecast, o: ForecastOutcome | None) -> dict[str, Any]:
    return {"id": str(f.id), "symbol": f.symbol, "origin_date": f.origin_date.isoformat(), "horizon": f.horizon,
            "model_key": f.model_key, "predicted_return": _f(f.predicted_return), "median_return": _f(f.median_return),
            "lower_return": _f(f.lower_return), "upper_return": _f(f.upper_return), "interval_level": f.interval_level,
            "interval_status": f.interval_status, "prob_up": _f(f.prob_up, 4), "prob_status": f.prob_status,
            "reference_price": str(f.reference_price), "implied_price": str(round(f.implied_price, 2)),
            "price_range": [round(float(f.reference_price) * (1 + f.lower_return), 2),
                            round(float(f.reference_price) * (1 + f.upper_return), 2)]
            if f.lower_return is not None else None,
            "reliability": f.reliability, "warnings": f.warnings, "model_version_ids": f.model_version_ids,
            "feature_set_version": f.feature_set_version, "created_at": f.created_at.isoformat(),
            "outcome": None if o is None else {
                "status": o.status, "target_date": o.target_date.isoformat() if o.target_date else None,
                "actual_return": _f(o.actual_return), "actual_price": str(o.actual_price) if o.actual_price else None,
                "error": _f(o.error), "direction_correct": o.direction_correct, "in_interval": o.in_interval,
                "invalid_reason": o.invalid_reason, "note": o.note}}


def forecasts_query(symbol: str | None = None, horizon: int | None = None, model_key: str | None = None):  # noqa: ANN201
    q = select(Forecast, ForecastOutcome).outerjoin(ForecastOutcome, ForecastOutcome.forecast_id == Forecast.id)
    if symbol:
        q = q.where(Forecast.symbol == symbol)
    if horizon:
        q = q.where(Forecast.horizon == horizon)
    if model_key:
        q = q.where(Forecast.model_key == model_key)
    return q


def latest_forecasts(symbol: str) -> dict[str, Any]:
    origin = db.session.scalar(select(func.max(Forecast.origin_date)).where(Forecast.symbol == symbol))
    if origin is None:
        return {"origin_date": None, "by_horizon": {}}
    rows = db.session.execute(forecasts_query(symbol).where(Forecast.origin_date == origin)).all()
    by_h: dict[str, dict[str, Any]] = {}
    for f, o in rows:
        primary, _ = registry.primary_model(f.horizon)
        entry = by_h.setdefault(str(f.horizon), {"primary_model": primary, "models": {}})
        entry["models"][f.model_key] = forecast_dict(f, o)
    return {"origin_date": origin.isoformat(), "by_horizon": by_h}


def forecast_rows(latest: dict[str, Any]) -> list[dict[str, Any]]:
    """Display order per horizon: primary first, then ensemble and the two baselines (no duplicates)."""
    rows = []
    for h in sorted(latest.get("by_horizon", {}), key=int):
        entry = latest["by_horizon"][h]
        order = [entry["primary_model"], "ensemble", "zero", "hist_mean"]
        for key in dict.fromkeys(order):
            if key in entry["models"]:
                rows.append({"horizon": int(h), "key": key, "is_primary": key == entry["primary_model"],
                             "f": entry["models"][key]})
    return rows


def forecast_history(symbol: str, horizon: int, model_key: str, limit: int = 250) -> list[dict[str, Any]]:
    rows = db.session.execute(forecasts_query(symbol, horizon, model_key).order_by(Forecast.origin_date.desc())
                              .limit(limit)).all()
    return [forecast_dict(f, o) for f, o in reversed(rows)]


def backtest_series(symbol: str, horizon: int, model_key: str, limit: int = 500) -> list[dict[str, Any]]:
    """Out-of-sample walk-forward predictions vs actuals from the latest E1 experiment."""
    from app.models.forecasting import BacktestPrediction

    exp = registry.latest_experiment("tech_walkforward_v1")
    if exp is None:
        return []
    rows = db.session.scalars(select(BacktestPrediction).where(
        BacktestPrediction.experiment_id == exp.id, BacktestPrediction.symbol == symbol,
        BacktestPrediction.horizon == horizon, BacktestPrediction.model_key == model_key)
        .order_by(BacktestPrediction.origin_date.desc()).limit(limit)).all()
    return [{"date": r.origin_date.isoformat(), "pred": r.predicted_return, "actual": r.actual_return,
             "lower": r.lower, "upper": r.upper, "split": r.split} for r in reversed(rows)]


def experiment_summary(exp: Experiment | None) -> dict[str, Any] | None:
    if exp is None:
        return None
    return {"id": str(exp.id), "name": exp.name, "hypothesis": exp.hypothesis, "status": exp.status,
            "started_at": exp.started_at.isoformat(), "finished_at": exp.finished_at.isoformat() if exp.finished_at
            else None, "conclusion": exp.conclusion, "config": exp.config, "data_version": exp.data_version,
            "code_version": exp.code_version}


def live_performance_table() -> list[dict[str, Any]]:
    from app.services.forecasting.live import live_performance

    df = live_performance()
    if df.empty:
        return []
    out = []
    for (h, key), g in df.groupby(["horizon", "model_key"]):
        dirs = g["direction_correct"].dropna()
        inside = g["in_interval"].dropna()
        out.append({"horizon": int(h), "model": key, "n": int(len(g)), "mae": _f(g["error"].abs().mean()),
                    "rmse": _f(np.sqrt((g["error"] ** 2).mean())),
                    "directional_accuracy": _f(dirs.mean(), 4) if len(dirs) else None,
                    "coverage_90": _f(inside.mean(), 4) if len(inside) else None,
                    "first_target": str(g["target_date"].min()), "last_target": str(g["target_date"].max())})
    return out


def model_overview() -> dict[str, Any]:
    versions = db.session.scalars(select(ModelVersion).order_by(ModelVersion.horizon, ModelVersion.model_key,
                                                                ModelVersion.version.desc())).all()
    events = db.session.scalars(select(ModelEvent).order_by(ModelEvent.occurred_at.desc()).limit(50)).all()
    weights = db.session.scalars(select(EnsembleWeights).order_by(EnsembleWeights.session_date.desc()).limit(30)).all()
    primaries = {h: registry.primary_model(h)[0] for h in (1, 5, 20)}
    return {
        "versions": [{"id": str(v.id), "model_key": v.model_key, "horizon": v.horizon, "version": v.version,
                      "status": v.status, "train_start": v.train_start.isoformat(),
                      "train_end": v.train_end.isoformat(),
                      "n_train": v.n_train, "trained_at": v.trained_at.isoformat(),
                      "approved_at": v.approved_at.isoformat() if v.approved_at else None,
                      "evaluation": v.evaluation, "interval": v.interval_calibration,
                      "artifact_sha256": v.artifact_sha256[:12]} for v in versions],
        "events": [{"occurred_at": e.occurred_at.isoformat(), "type": e.event_type, "decision": e.decision,
                    "horizon": e.horizon, "reason": e.reason, "actor": e.actor,
                    "session": e.session_date.isoformat() if e.session_date else None} for e in events],
        "ensemble_weights": [{"session": w.session_date.isoformat(), "horizon": w.horizon, "method": w.method,
                              "weights": w.weights, "evidence": w.evidence} for w in weights],
        "primary_models": primaries,
    }


def research_health(symbols: tuple[str, ...] | None = None) -> dict[str, Any]:
    """`symbols` limits the data-quality counts to the tracked watchlist (stored bars for symbols that
    were tried and dropped keep their historical findings, which are not current concerns)."""
    if symbols is None:
        from flask import current_app

        symbols = tuple(current_app.config["PSXDATA_SYMBOLS"])
    last_pipeline = db.session.scalar(select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(1))
    last_forecast = db.session.scalar(select(func.max(Forecast.created_at)))
    last_eval = db.session.scalar(select(func.max(ForecastOutcome.evaluated_at)))
    last_train = db.session.scalar(select(ModelEvent).where(ModelEvent.event_type == "training")
                                   .order_by(ModelEvent.occurred_at.desc()).limit(1))
    last_adapt = db.session.scalar(select(ModelEvent).where(ModelEvent.event_type.in_(["retrain_check", "promotion"]))
                                   .order_by(ModelEvent.occurred_at.desc()).limit(1))
    dq = dict(db.session.execute(select(DataQualityIssue.severity, func.count()).where(
        DataQualityIssue.resolved_at.is_(None), DataQualityIssue.symbol.in_(symbols))
        .group_by(DataQualityIssue.severity)).all())
    pending = db.session.scalar(select(func.count()).select_from(Forecast).outerjoin(
        ForecastOutcome, ForecastOutcome.forecast_id == Forecast.id).where(ForecastOutcome.id.is_(None)))
    return {
        "pipeline": None if last_pipeline is None else {
            "id": str(last_pipeline.id), "status": last_pipeline.status,
            "session": last_pipeline.session_date.isoformat() if last_pipeline.session_date else None,
            "started_at": last_pipeline.started_at.isoformat(), "steps": last_pipeline.steps},
        "last_forecast_at": last_forecast.isoformat() if last_forecast else None,
        "last_evaluation_at": last_eval.isoformat() if last_eval else None,
        "pending_forecasts": pending,
        "last_training": None if last_train is None else {"at": last_train.occurred_at.isoformat(),
                                                          "decision": last_train.decision, "reason": last_train.reason},
        "last_adaptation_decision": None if last_adapt is None else {
            "at": last_adapt.occurred_at.isoformat(), "type": last_adapt.event_type, "decision": last_adapt.decision,
            "reason": last_adapt.reason},
        "approved_models": db.session.scalar(select(func.count()).select_from(ModelVersion).where(
            ModelVersion.status == "approved")),
        "open_data_quality": dq,
    }


def market_overview(config: Mapping[str, Any]) -> dict[str, Any]:
    symbols = tuple(config["PSXDATA_SYMBOLS"])
    frames = load_universe(symbols)
    rows = []
    for s in symbols:
        df = frames[s].frame
        if df.empty:
            continue
        c = df["adj_close"]
        r = {f"{n}d": _f(c.iloc[-1] / c.iloc[-1 - n] - 1, 4) if len(c) > n else None for n in (1, 5, 20, 60, 250)}
        summary = analysis_for(s, frames)
        rows.append({"symbol": s, "is_index": frames[s].is_index, "session": df.index[-1].date().isoformat(),
                     "close": _f(df["close"].iloc[-1], 2), "returns": r,
                     "trend": (summary.get("trend") or {}).get("classification"),
                     "volatility_regime": (summary.get("risk") or {}).get("volatility_regime"),
                     "vol20": (summary.get("risk") or {}).get("vol20"),
                     "sentiment": (sentiment_daily("MARKET" if s == BENCHMARK else s, limit=1) or [None])[-1]})
    stocks = [r for r in rows if not r["is_index"] and r["returns"]["20d"] is not None]
    ranked = sorted(stocks, key=lambda r: r["returns"]["20d"], reverse=True)
    news = db.session.execute(select(NewsArticle, NewsMention).join(NewsMention, NewsMention.article_id ==
                                                                     NewsArticle.id)
                              .where(NewsMention.relevance >= RELEVANCE_THRESHOLD)
                              .order_by(NewsArticle.published_at.desc().nulls_last()).limit(12)).all()
    return {"latest_session": frames[BENCHMARK].latest_session.isoformat() if frames[BENCHMARK].latest_session
            else None, "coverage": {"symbols": list(symbols), "note": "Tracked watchlist of 4 stocks + KSE100; not the "
                                                                    "whole PSX. Breadth is not computed."},
            "instruments": rows, "top_20d": ranked[:2], "bottom_20d": ranked[-2:][::-1],
            "recent_news": [article_dict(a, m) for a, m in news]}
