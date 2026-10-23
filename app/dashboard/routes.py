"""Dashboard pages. Handlers only call service-layer functions; no analytical logic here."""

from __future__ import annotations

import logging

from flask import current_app, render_template
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.dashboard import dashboard_bp
from app.extensions import db
from app.models.operations import JobRun, JobStatus
from app.services import health
from app.services.market_data import (
    PRICE_NOTE,
    SOURCE_LABEL,
    bars_freshness,
    integration_status,
    price_history,
    quotes_freshness,
    symbol_summaries,
)
from app.services.orchestration.jobs import find_stuck_jobs

log = logging.getLogger(__name__)


def _market_context(*, with_charts: bool = False) -> dict:
    """Market data for templates; a database failure becomes an explicit error state."""
    config = current_app.config
    symbols = tuple(config["PSXDATA_SYMBOLS"])
    try:
        status = integration_status(config)
        summaries = symbol_summaries(symbols)
        context = {
            "integration": status,
            "bars_fresh": bars_freshness(config, status),
            "quotes_fresh": quotes_freshness(config, status),
            "summaries": summaries,
            "has_data": any(s.bar is not None or s.quote is not None for s in summaries),
            "tracked_symbols": symbols,
            "source_label": SOURCE_LABEL,
            "price_note": PRICE_NOTE,
            "market_error": None,
        }
        if with_charts:
            # Last ~1 year of closes per symbol, rendered client-side by Plotly.
            context["charts"] = {
                s: {"x": [b.session_date.isoformat() for b in bars], "y": [float(b.close) for b in bars]}
                for s in symbols if (bars := price_history(s, limit=260))
            }
        return context
    except SQLAlchemyError:
        db.session.rollback()
        log.exception("failed to load market data for dashboard", extra={"event_type": "dashboard_error"})
        return {"market_error": "Market data could not be loaded from the database."}


@dashboard_bp.get("/")
def index():
    checks = health.check_all()
    return render_template("dashboard/index.html", overall=health.overall_status(checks), checks=checks,
                           **_market_context(with_charts=True), **_research_context())


def _research_health() -> dict | None:
    from app.services.research import views

    try:
        return views.research_health()
    except SQLAlchemyError:
        db.session.rollback()
        return None


@dashboard_bp.get("/system")
def system():
    checks = health.check_all()
    recent_jobs = db.session.scalars(select(JobRun).order_by(JobRun.created_at.desc()).limit(25)).all()
    failed_jobs = db.session.scalars(
        select(JobRun)
        .where(JobRun.status.in_([JobStatus.FAILED, JobStatus.DISPATCH_FAILED]))
        .order_by(JobRun.created_at.desc())
        .limit(10)
    ).all()
    return render_template("dashboard/system.html", overall=health.overall_status(checks), checks=checks,
                           recent_jobs=recent_jobs, failed_jobs=failed_jobs, stuck_jobs=find_stuck_jobs(),
                           research=_research_health(), **_market_context())


# --------------------------------------------------------------------------------------------------
# Research pages (all data from persisted results via app.services.research.views)


def _research_context() -> dict:
    from app.services.research import views

    try:
        return {"overview": views.market_overview(current_app.config), "research_error": None}
    except SQLAlchemyError:
        db.session.rollback()
        log.exception("failed to load research overview", extra={"event_type": "dashboard_error"})
        return {"overview": None, "research_error": "Research data could not be loaded."}


@dashboard_bp.get("/stocks/<symbol>")
def stock_page(symbol: str):
    from flask import abort

    from app.services.research import views

    symbol = symbol.upper()
    if symbol not in current_app.config["PSXDATA_SYMBOLS"]:
        abort(404)
    analysis = views.analysis_for(symbol)
    news_key = "MARKET" if symbol == "KSE100" else symbol
    news, news_total = views.news_for(news_key, 1, 25)
    forecasts = views.latest_forecasts(symbol)
    history = {h: views.forecast_history(symbol, h, "ensemble", 120) for h in current_app.config["FORECAST_HORIZONS"]}
    backtest = views.backtest_series(symbol, 1, "ens_equal_tech", 250)
    return render_template(
        "dashboard/stock.html", symbol=symbol, analysis=analysis,
        series=views.indicator_series(symbol, None, None, 750), news=news, news_total=news_total,
        news_key=news_key, sentiment=views.sentiment_daily(news_key, limit=500), forecasts=forecasts,
        forecast_rows=views.forecast_rows(forecasts),
        history=history, backtest=backtest, tracked=current_app.config["PSXDATA_SYMBOLS"])


@dashboard_bp.get("/models")
def models_page():
    from app.services.forecasting.registry import latest_experiment
    from app.services.research import views

    e1, e2, e3 = (latest_experiment(n) for n in ("tech_walkforward_v1", "news_vs_tech_v1", "signal_studies_v1"))
    return render_template("dashboard/models.html", e1=e1, e2=e2, e3=e3, live=views.live_performance_table(),
                           registry=views.model_overview())
