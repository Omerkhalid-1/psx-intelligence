"""Daily research workflow for one market session, with explicit step dependencies.

Steps (a failed step stops every step that depends on it; independent steps still run):
  1 market_data        incremental daily-bar ingestion (psxdata-api)          independent
  2 market_session     newest validated session (KSE100 bar) is newer than the last completed run
                       (checked after ingestion, so a manual run picks up a session fetched just now) needs 1
  3 data_quality       quality checks; blocks downstream on new unexplained gaps needs 2
  4 corporate_actions  re-verify recorded actions against prices               needs 2
  5 news               discovery + scoring + daily aggregation (if enabled)    independent
  6 features           feature snapshots + research summaries                  needs 3, 4
  7 forecasts          forecasts from approved models (immutable)              needs 6
  8 evaluation         evaluate matured forecasts                              needs 3
  9 ensemble_weights   recompute weights from matured live errors               needs 8
 10 retraining         policy check; candidate training/validation if enabled  needs 6, 8
 11 promotion          promote passing candidates if ENABLE_MODEL_PROMOTION    needs 10
When there is no new session (and not forced) only the independent steps run (fresh bars are still
fetched and news is still refreshed); session-dependent steps are recorded as skipped.
Last known good results are never overwritten: forecasts and outcomes are insert-only.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.forecasting import ModelVersion, PipelineRun
from app.models.research import AnalysisSnapshot, DataQualityIssue
from app.utils.logging import log_context, redact
from app.utils.time import utcnow

log = logging.getLogger(__name__)

DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "market_data": (), "market_session": ("market_data",), "data_quality": ("market_session",),
    "corporate_actions": ("market_session",), "news": (), "features": ("data_quality", "corporate_actions"),
    "forecasts": ("features",), "evaluation": ("data_quality",), "ensemble_weights": ("evaluation",),
    "retraining": ("features", "evaluation"), "promotion": ("retraining",),
}


# Steps that do not depend on a new market session (they refresh source data).
INDEPENDENT_STEPS = frozenset({"market_data", "market_session", "news"})


class StepSkipped(Exception):
    """A step decided there is nothing to do; dependents still run."""


class PipelineBusy(Exception):
    pass


def run_daily_pipeline(config: Mapping[str, Any], *, job_run_id: uuid.UUID | None = None, force: bool = False,
                       skip_ingestion: bool = False) -> PipelineRun:
    run = PipelineRun(id=uuid.uuid4(), job_run_id=job_run_id, status="running", steps=[])
    db.session.add(run)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise PipelineBusy("a pipeline run is already in progress") from None

    ctx: dict[str, Any] = {"force": force}
    status: dict[str, str] = {}
    no_new_session = False
    with log_context(run_id=str(run.id), analysis_module="pipeline"):
        for name, fn in _steps(config, skip_ingestion).items():
            if no_new_session and name not in INDEPENDENT_STEPS:
                status[name] = "skipped"
                _record(run, name, "skipped", "no new session")
                continue
            blocked = [d for d in DEPENDENCIES[name] if status.get(d) not in ("succeeded", "skipped")]
            if blocked:
                status[name] = "blocked"
                _record(run, name, "blocked", f"dependency did not succeed: {', '.join(blocked)}")
                continue
            started = utcnow()
            try:
                result = fn(ctx) or {}
                status[name] = "succeeded"
                _record(run, name, "succeeded", None, result, started)
            except StepSkipped as exc:
                status[name] = "skipped"
                _record(run, name, "skipped", str(exc), None, started)
            except Exception as exc:  # noqa: BLE001 - a failed step is recorded, dependents blocked
                db.session.rollback()
                status[name] = "failed"
                log.exception("pipeline step failed", extra={"event_type": "pipeline_step_failed",
                                                             "details": {"step": name}})
                _record(run, name, "failed", redact(f"{type(exc).__name__}: {exc}")[:500],
                        {"trace": redact(traceback.format_exc(limit=3))[-1500:]}, started)
            if name == "market_session" and status[name] == "skipped" and not force:
                no_new_session = True  # nothing session-dependent to do; independent steps still run
    run.session_date = ctx.get("session")
    values = set(status.values())
    independent_failed = any(status.get(n) == "failed" for n in INDEPENDENT_STEPS)
    run.status = ("partial" if no_new_session and independent_failed else
                  "skipped" if no_new_session else
                  "succeeded" if values <= {"succeeded", "skipped"} else
                  "failed" if "succeeded" not in values else "partial")
    run.finished_at = utcnow()
    db.session.commit()
    return run


def _record(run: PipelineRun, name: str, status: str, message: str | None, result: dict[str, Any] | None = None,
            started: Any = None) -> None:
    run.steps = [*run.steps, {"step": name, "status": status, "message": message, "result": result or {},
                              "started_at": started.isoformat() if started else None,
                              "finished_at": utcnow().isoformat()}]
    db.session.commit()


def _steps(config: Mapping[str, Any], skip_ingestion: bool) -> dict[str, Callable[[dict], dict | None]]:
    from app.services.forecasting import live, policy, registry
    from app.services.forecasting.dataset import build_panel
    from app.services.ingestion.psx import run_daily_bar_ingestion
    from app.services.research.analysis import METHOD_VERSION, summarize
    from app.services.research.corporate_actions import sync_corporate_actions
    from app.services.research.features import FEATURE_SET_TECH
    from app.services.research.prices import BENCHMARK, load_universe
    from app.services.research.quality import run_quality_checks

    symbols = tuple(config["PSXDATA_SYMBOLS"])

    def market_session(ctx: dict) -> dict:
        frames = load_universe(symbols)
        latest = frames[BENCHMARK].latest_session
        last = db.session.scalar(
            select(PipelineRun.session_date).where(PipelineRun.status.in_(["succeeded", "partial"]))
            .order_by(PipelineRun.session_date.desc()).limit(1))
        ctx["session"] = latest
        if latest is None:
            raise RuntimeError("no benchmark bars stored")
        if last is not None and latest <= last and not ctx["force"]:
            raise StepSkipped(f"no new session since {last}")
        return {"session": latest.isoformat(), "previous": last.isoformat() if last else None}

    def market_data(ctx: dict) -> dict:
        if skip_ingestion or not config["PSXDATA_ENABLED"]:
            raise StepSkipped("ingestion disabled or skipped; using stored bars")
        r = run_daily_bar_ingestion(config)
        if r.status.value == "failed":
            raise RuntimeError(f"daily bar ingestion failed: {r.error_summary}")
        return {"ingestion_run_id": str(r.id), "status": r.status.value, "stored": r.records_stored}

    def data_quality(ctx: dict) -> dict:
        summary = run_quality_checks(config)
        errors = db.session.scalars(select(DataQualityIssue).where(
            DataQualityIssue.resolved_at.is_(None), DataQualityIssue.severity == "error",
            DataQualityIssue.session_date == ctx["session"])).all()
        if errors:
            raise RuntimeError(f"{len(errors)} data-quality error(s) on the new session: "
                               + "; ".join(f"{e.symbol} {e.check_name}" for e in errors))
        return summary

    def corporate_actions(ctx: dict) -> dict:
        return sync_corporate_actions(Path(config["CORPORATE_ACTIONS_FILE"]), actor="pipeline")

    def news(ctx: dict) -> dict:
        if not config.get("NEWS_ENABLED"):
            raise StepSkipped("NEWS_ENABLED is false")
        from app.services.news import ingest
        from app.services.news.relevance import load_news_config
        from app.services.news.sentiment import get_model
        from app.services.research.prices import observed_calendar

        cfg = load_news_config(Path(config["NEWS_CONFIG"]))
        r = ingest.discover(cfg)
        scored = ingest.score_pending(get_model(config["NEWS_SENTIMENT_MODEL"], Path(config["ARTIFACTS_DIR"])))
        cal = observed_calendar(load_universe(symbols))
        recent = cal[cal >= cal[-10]] if len(cal) > 10 else cal
        from app.services.news.sentiment import MODEL_IDS

        written = ingest.rebuild_daily_sentiment((*[s for s in symbols if s != BENCHMARK], "MARKET"), recent,
                                                 MODEL_IDS[config["NEWS_SENTIMENT_MODEL"]],
                                                 ingest.coverage_start(config))
        return {"discovery": r.status.value, "new_articles": r.records_stored, "scored": scored, "aggregates": written}

    def features(ctx: dict) -> dict:
        frames = load_universe(symbols)
        ctx["panel"] = build_panel(config, feature_set=FEATURE_SET_TECH, horizons=tuple(config["FORECAST_HORIZONS"]),
                                   frames=frames)
        written = 0
        for s in symbols:
            summary = summarize(frames[s], frames[BENCHMARK])
            if summary.get("status") != "ok":
                continue
            d = date.fromisoformat(summary["session_date"])
            if db.session.scalar(select(AnalysisSnapshot.id).where(AnalysisSnapshot.symbol == s,
                                                                   AnalysisSnapshot.session_date == d,
                                                                   AnalysisSnapshot.method_version == METHOD_VERSION)):
                continue
            db.session.add(AnalysisSnapshot(symbol=s, session_date=d, method_version=METHOD_VERSION, summary=summary))
            written += 1
        written += persist_feature_snapshots(ctx["panel"], ctx["session"])
        db.session.commit()
        return {"snapshots_written": written}

    def forecasts(ctx: dict) -> dict:
        if not live.approved_versions():
            raise RuntimeError("no approved model versions; run `flask --app wsgi models bootstrap`")
        return live.generate_forecasts(config, panel=ctx["panel"])

    def evaluation(ctx: dict) -> dict:
        return live.evaluate_matured(config)

    def ensemble_weights(ctx: dict) -> dict:
        exp = registry.latest_experiment("tech_walkforward_v1")
        retained = {int(h): bool(v.get("adaptive_weights_retained")) for h, v in
                    ((exp.results or {}).get("selection", {}) if exp else {}).items()}
        return live.update_ensemble_weights(ctx["session"], tuple(config["FORECAST_HORIZONS"]),
                                            adaptive_retained=retained)

    def retraining(ctx: dict) -> dict:
        decisions = policy.decide(config, ctx["panel"], ctx["session"])
        horizons = tuple(h for h, d in decisions.items() if d["retrain"])
        if not horizons:
            raise StepSkipped("no retraining condition met")
        if not config["ENABLE_MODEL_TRAINING"]:
            raise StepSkipped(f"retraining warranted for h={horizons} but ENABLE_MODEL_TRAINING is false")
        cands = registry.train_candidates(config, horizons, actor="pipeline", panel=ctx["panel"])
        ctx["candidates"] = cands
        return {"decisions": {str(h): d for h, d in decisions.items()}, "candidates": len(cands)}

    def promotion(ctx: dict) -> dict:
        cands: list[ModelVersion] = ctx.get("candidates", [])
        if not cands:
            raise StepSkipped("no candidates")
        if not config["ENABLE_MODEL_PROMOTION"]:
            raise StepSkipped(f"{len(cands)} candidate(s) await manual review (ENABLE_MODEL_PROMOTION is false)")
        promoted = rejected = 0
        for mv in cands:
            evidence = registry.validate_candidate(mv, config, ctx["panel"])
            if evidence["passed"]:
                registry.promote(mv, actor="pipeline", evidence=evidence)
                promoted += 1
            else:
                registry.reject(mv, actor="pipeline", evidence=evidence)
                rejected += 1
        return {"promoted": promoted, "rejected": rejected}

    return {"market_data": market_data, "market_session": market_session, "data_quality": data_quality,
            "corporate_actions": corporate_actions, "news": news, "features": features, "forecasts": forecasts,
            "evaluation": evaluation, "ensemble_weights": ensemble_weights, "retraining": retraining,
            "promotion": promotion}


def persist_feature_snapshots(panel, session: date | None) -> int:  # noqa: ANN001
    """Store the origin session's feature rows (immutable per data version) for forecast traceability."""
    from app.models.research import FeatureSnapshot

    if session is None:
        return 0
    rows = panel.data[panel.data["date"] == pd.Timestamp(session)]
    written = 0
    for _, r in rows.iterrows():
        exists = db.session.scalar(select(FeatureSnapshot.id).where(
            FeatureSnapshot.symbol == r["symbol"], FeatureSnapshot.session_date == session,
            FeatureSnapshot.feature_set_version == panel.feature_set,
            FeatureSnapshot.data_version == _short(panel.data_version)))
        if exists:
            continue
        values = {f: (None if r[f] != r[f] else float(r[f])) for f in panel.features}
        db.session.add(FeatureSnapshot(symbol=r["symbol"], session_date=session, feature_set_version=panel.feature_set,
                                       data_version=_short(panel.data_version), values=values,
                                       adjustment_status="adjusted" if r["usable"] else "unusable"))
        written += 1
    return written


def _short(v: str) -> str:
    import hashlib

    return v if len(v) <= 64 else hashlib.sha256(v.encode()).hexdigest()
