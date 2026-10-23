"""Website pages. Each page renders a shell plus its initial data as JSON (read by the page script);
the browser fetches anything slow or interactive from /api/v1."""

from __future__ import annotations

import logging

from flask import Response, abort, current_app, render_template, request
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.site import data, site_bp

log = logging.getLogger(__name__)


def _render(template: str, page: str, payload: dict, **ctx):
    return render_template(template, page=page, page_data=payload,
                           tracked=list(current_app.config["PSXDATA_SYMBOLS"]), **ctx)


def _safe(fn, fallback):  # noqa: ANN001
    try:
        return fn()
    except SQLAlchemyError:
        db.session.rollback()
        log.exception("website data unavailable", extra={"event_type": "site_error"})
        return fallback


@site_bp.get("/")
def home():
    wl = _safe(lambda: data.watchlist(current_app.config), [])
    payload = {"watchlist": wl, "headlines": _safe(data.market_headlines, []), "freshness": _safe(data.freshness, {}),
               "columns": data.matrix_columns()}
    return _render("site/home.html", "home", payload, watchlist=wl)


@site_bp.get("/stock/<symbol>")
def stock(symbol: str):
    from app.api.responses import ApiError
    from app.api.site import engine_payload

    symbol = symbol.strip().upper()
    if symbol not in current_app.config["PSXDATA_SYMBOLS"]:
        abort(404)
    try:
        payload = _safe(lambda: engine_payload(symbol, with_series=True, limit=750), None)
    except ApiError:
        payload = None
    names = data.company_names(current_app.config)
    sector = data.sectors(current_app.config).get(symbol, "")
    from app.services.research.engine import GROUPS, parameter_guide

    return _render("site/stock.html", "stock",
                   {"symbol": symbol, "name": names.get(symbol, symbol), "engine": payload,
                    "parameters": parameter_guide(), "groups": GROUPS},
                   symbol=symbol, name=names.get(symbol, symbol), engine=payload, sector=sector)


@site_bp.get("/learn")
def learn():
    symbol = (request.args.get("symbol") or "").strip().upper()
    tracked = current_app.config["PSXDATA_SYMBOLS"]
    if symbol not in tracked:
        symbol = next((s for s in tracked if s != "KSE100"), tracked[0])
    return _render("site/learn.html", "learn", {"symbol": symbol})


@site_bp.get("/news")
def news():
    return _render("site/news.html", "news", {"symbol": (request.args.get("symbol") or "").strip().upper()[:12]})


@site_bp.get("/engine")
def engine_page():
    from app.services.research.engine import ENGINE_VERSION, GROUPS, parameter_guide

    return _render("site/engine.html", "engine", {"groups": GROUPS}, parameters=parameter_guide(), groups=GROUPS,
                   engine_version=ENGINE_VERSION)


@site_bp.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /api/\nDisallow: /ops/\n", mimetype="text/plain")


@site_bp.app_errorhandler(404)
def not_found(exc):  # noqa: ANN001
    if request.path.startswith("/api/"):
        from app.api.responses import error_response

        return error_response(404, "not_found", "Not found")
    return render_template("site/404.html", page="404", page_data={},
                           tracked=list(current_app.config["PSXDATA_SYMBOLS"])), 404
