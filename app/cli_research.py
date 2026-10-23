"""Operator commands for the research layer: `flask --app wsgi <group> <command>`.

  research sync-corporate-actions | quality | analyze
  news     discover [--backfill-from YYYY-MM-DD] | train-sentiment | score | aggregate | evaluate-sentiment
  forecast experiment {e1,e2,e3} | generate | evaluate
  models   list | bootstrap | train | validate <id> | promote <id> | reject <id> | rollback <key> <h>
  pipeline run [--force] [--skip-ingestion]
Mutating model-governance commands require --actor (recorded in model_events / audit_events).
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path

import click
from flask import Flask, current_app
from flask.cli import AppGroup

from app.extensions import db


def _echo(obj) -> None:  # noqa: ANN001
    click.echo(json.dumps(obj, indent=2, default=str))


def register_research_commands(app: Flask) -> None:
    research, news, forecast, models, pipeline = (AppGroup(n) for n in ("research", "news", "forecast", "models",
                                                                        "pipeline"))

    @research.command("sync-corporate-actions")
    @click.option("--actor", default="cli")
    def sync_ca(actor: str) -> None:
        """Load config/corporate_actions.yaml and verify each action against prices."""
        from app.services.research.corporate_actions import sync_corporate_actions

        _echo(sync_corporate_actions(Path(current_app.config["CORPORATE_ACTIONS_FILE"]), actor=actor))

    @research.command("quality")
    def quality() -> None:
        """Run data-quality checks and upsert findings."""
        from app.services.research.quality import run_quality_checks

        _echo(run_quality_checks(current_app.config))

    @research.command("analyze")
    def analyze() -> None:
        """Compute and store research summaries + feature snapshots for the latest session."""
        from app.models.research import AnalysisSnapshot
        from app.services.forecasting.dataset import build_panel
        from app.services.pipeline import persist_feature_snapshots
        from app.services.research.analysis import summarize
        from app.services.research.prices import BENCHMARK, load_universe

        cfg = current_app.config
        frames = load_universe(tuple(cfg["PSXDATA_SYMBOLS"]))
        out = {}
        for s in cfg["PSXDATA_SYMBOLS"]:
            summary = summarize(frames[s], frames[BENCHMARK])
            out[s] = {k: summary.get(k) for k in ("session_date", "status")}
            if summary.get("status") == "ok":
                d = date.fromisoformat(summary["session_date"])
                exists = db.session.query(AnalysisSnapshot.id).filter_by(symbol=s, session_date=d,
                                                                          method_version=summary["method_version"]).first()
                if not exists:
                    db.session.add(AnalysisSnapshot(symbol=s, session_date=d, method_version=summary["method_version"],
                                                    summary=summary))
        panel = build_panel(cfg, horizons=tuple(cfg["FORECAST_HORIZONS"]), frames=frames)
        out["feature_snapshots"] = persist_feature_snapshots(panel, frames[BENCHMARK].latest_session)
        db.session.commit()
        _echo(out)

    @news.command("discover")
    @click.option("--backfill-from", default=None, help="YYYY-MM-DD: month-by-month historical search")
    def discover_cmd(backfill_from: str | None) -> None:
        from app.services.news.ingest import discover
        from app.services.news.relevance import load_news_config

        run = discover(load_news_config(Path(current_app.config["NEWS_CONFIG"])),
                       backfill_from=date.fromisoformat(backfill_from) if backfill_from else None)
        _echo({"run": str(run.id), "status": run.status.value, "items": run.records_received,
               "new_articles": run.records_stored, "errors": run.error_summary})

    @news.command("train-sentiment")
    @click.option("--dataset", default="data/reference/FinancialPhraseBank-v1.0/Sentences_75Agree.txt")
    def train_sentiment(dataset: str) -> None:
        """Train the PhraseBank TF-IDF + logistic regression sentiment model."""
        from app.services.news.sentiment import train_phrasebank_lr

        _echo(train_phrasebank_lr(Path(dataset), Path(current_app.config["ARTIFACTS_DIR"]) / "sentiment" /
                                  "phrasebank_lr_v1.joblib"))

    @news.command("score")
    @click.option("--model", "model_name", default=None)
    def score_cmd(model_name: str | None) -> None:
        from app.services.news.ingest import score_pending
        from app.services.news.sentiment import get_model

        name = model_name or current_app.config["NEWS_SENTIMENT_MODEL"]
        _echo({"model": name, "scored": score_pending(get_model(name, Path(current_app.config["ARTIFACTS_DIR"])))})

    @news.command("index-stored")
    @click.option("--symbols", default="", help="Comma-separated entities (default: all configured).")
    def index_stored_cmd(symbols: str) -> None:
        """Link already-stored headlines to newly configured entities (run after adding a symbol)."""
        from app.services.news import ingest
        from app.services.news.relevance import load_news_config

        chosen = tuple(s.strip().upper() for s in symbols.split(",") if s.strip()) or None
        _echo({"created_mentions": ingest.index_stored_articles(
            load_news_config(Path(current_app.config["NEWS_CONFIG"])), chosen)})

    @news.command("aggregate")
    def aggregate_cmd() -> None:
        from app.services.news import ingest
        from app.services.research.prices import BENCHMARK, load_universe, observed_calendar

        cfg = current_app.config
        cal = observed_calendar(load_universe(tuple(cfg["PSXDATA_SYMBOLS"])))
        syms = (*[s for s in cfg["PSXDATA_SYMBOLS"] if s != BENCHMARK], "MARKET")
        from app.services.news.sentiment import MODEL_IDS

        _echo({"written": ingest.rebuild_daily_sentiment(syms, cal, MODEL_IDS[cfg["NEWS_SENTIMENT_MODEL"]],
                                                         ingest.coverage_start(cfg))})

    @news.command("evaluate-sentiment")
    @click.option("--labels", default="config/news_eval_set.csv")
    def evaluate_sentiment(labels: str) -> None:
        """Compare sentiment models on the reviewed Pakistan-headline evaluation set."""
        from app.services.news.evaluation import evaluate_models

        _echo(evaluate_models(Path(labels), Path(current_app.config["ARTIFACTS_DIR"])))

    @forecast.command("experiment")
    @click.argument("which", type=click.Choice(["e1", "e2", "e3"]))
    @click.option("--sentiment-method", default="dedup_mean_v1")
    def experiment(which: str, sentiment_method: str) -> None:
        """Run a pre-registered walk-forward experiment and store its results."""
        from app.services.forecasting import experiments as E

        exp = {"e1": lambda: E.run_e1(current_app.config),
               "e2": lambda: E.run_e2(current_app.config, sentiment_method),
               "e3": lambda: E.run_e3(current_app.config)}[which]()
        _echo({"experiment_id": str(exp.id), "name": exp.name, "conclusion": exp.conclusion})

    @forecast.command("generate")
    def generate() -> None:
        from app.services.forecasting.live import generate_forecasts

        _echo(generate_forecasts(current_app.config))

    @forecast.command("evaluate")
    def evaluate_cmd() -> None:
        from app.services.forecasting.live import evaluate_matured

        _echo(evaluate_matured(current_app.config))

    @models.command("list")
    def list_models() -> None:
        from app.models.forecasting import ModelVersion

        rows = db.session.query(ModelVersion).order_by(ModelVersion.horizon, ModelVersion.model_key,
                                                       ModelVersion.version).all()
        _echo([{"id": str(m.id), "key": m.model_key, "h": m.horizon, "v": m.version, "status": m.status,
                "train_end": m.train_end, "n_train": m.n_train} for m in rows])

    @models.command("train")
    @click.option("--actor", required=True)
    def train(actor: str) -> None:
        """Train candidate versions of every production model for all horizons."""
        from app.services.forecasting.registry import train_candidates

        cands = train_candidates(current_app.config, tuple(current_app.config["FORECAST_HORIZONS"]), actor=actor)
        _echo([{"id": str(c.id), "key": c.model_key, "h": c.horizon, "v": c.version} for c in cands])

    @models.command("validate")
    @click.argument("version_id")
    def validate(version_id: str) -> None:
        from app.models.forecasting import ModelVersion
        from app.services.forecasting.dataset import build_panel
        from app.services.forecasting.registry import validate_candidate

        mv = db.session.get(ModelVersion, uuid.UUID(version_id))
        _echo(validate_candidate(mv, current_app.config, build_panel(current_app.config, horizons=(mv.horizon,))))

    @models.command("promote")
    @click.argument("version_id")
    @click.option("--actor", required=True)
    @click.option("--force-reason", default=None, help="Override failed criteria (recorded verbatim)")
    def promote_cmd(version_id: str, actor: str, force_reason: str | None) -> None:
        from app.models.forecasting import ModelVersion
        from app.services.forecasting.dataset import build_panel
        from app.services.forecasting.registry import PromotionRefused, promote, validate_candidate

        mv = db.session.get(ModelVersion, uuid.UUID(version_id))
        if mv is None:
            raise click.ClickException("unknown version")
        evidence = validate_candidate(mv, current_app.config, build_panel(current_app.config, horizons=(mv.horizon,)))
        try:
            promote(mv, actor=actor, evidence=evidence, force_reason=force_reason)
        except PromotionRefused as exc:
            raise click.ClickException(str(exc)) from None
        _echo({"promoted": version_id, "evidence": evidence})

    @models.command("rollback")
    @click.argument("model_key")
    @click.argument("horizon", type=int)
    @click.option("--actor", required=True)
    @click.option("--reason", required=True)
    def rollback_cmd(model_key: str, horizon: int, actor: str, reason: str) -> None:
        from app.services.forecasting.registry import PromotionRefused, rollback

        try:
            mv = rollback(model_key, horizon, actor=actor, reason=reason)
        except PromotionRefused as exc:
            raise click.ClickException(str(exc)) from None
        _echo({"approved": str(mv.id), "version": mv.version})

    @models.command("bootstrap")
    @click.option("--actor", required=True)
    @click.option("--keys", default="", help="Comma-separated subset of production model keys (default: all)")
    def bootstrap(actor: str, keys: str) -> None:
        """First-time setup: train all production models, validate each candidate, promote those that
        pass, and record the primary model per horizon from the latest E1 selection."""
        from app.services.forecasting.dataset import build_panel
        from app.services.forecasting.registry import (
            latest_experiment,
            promote,
            reject,
            set_primary,
            train_candidates,
            validate_candidate,
        )

        cfg = current_app.config
        exp = latest_experiment("tech_walkforward_v1")
        if exp is None:
            raise click.ClickException("run `flask --app wsgi forecast experiment e1` first")
        panel = build_panel(cfg, horizons=tuple(cfg["FORECAST_HORIZONS"]))
        out = []
        from app.services.forecasting.registry import PRODUCTION_KEYS

        chosen = tuple(k.strip() for k in keys.split(",") if k.strip()) or PRODUCTION_KEYS
        unknown = set(chosen) - set(PRODUCTION_KEYS)
        if unknown:
            raise click.BadParameter(f"unknown model keys: {sorted(unknown)}")
        for mv in train_candidates(cfg, tuple(cfg["FORECAST_HORIZONS"]), keys=chosen, actor=actor, panel=panel):
            evidence = validate_candidate(mv, cfg, panel)
            (promote if evidence["passed"] else reject)(mv, actor=actor, evidence=evidence)
            out.append({"key": mv.model_key, "h": mv.horizon, "status": mv.status, "reason": evidence["reason"]})
        for h, sel in (exp.results or {}).get("selection", {}).items():
            set_primary(int(h), sel["selected"], actor=actor,
                        evidence={"experiment_id": str(exp.id), "reason": sel["reason"]})
        _echo(out)

    @pipeline.command("run")
    @click.option("--force", is_flag=True, help="Run even if the session was already processed")
    @click.option("--skip-ingestion", is_flag=True)
    def run_pipeline(force: bool, skip_ingestion: bool) -> None:
        from app.services.pipeline import PipelineBusy, run_daily_pipeline

        try:
            run = run_daily_pipeline(current_app.config, force=force, skip_ingestion=skip_ingestion)
        except PipelineBusy as exc:
            raise click.ClickException(str(exc)) from None
        _echo({"run": str(run.id), "status": run.status, "session": run.session_date,
               "steps": [{k: s[k] for k in ("step", "status", "message")} for s in run.steps]})

    @click.command("refresh")
    def refresh_cmd() -> None:
        """Get the latest information now: quotes, then bars/news/analysis/forecasts (runs inline)."""
        from app.services.refresh import run_refresh

        out = run_refresh(current_app.config)
        _echo(out)
        if out["status"] == "failed":
            raise click.ClickException("refresh failed")

    for group in (research, news, forecast, models, pipeline, refresh_cmd):
        app.cli.add_command(group)
