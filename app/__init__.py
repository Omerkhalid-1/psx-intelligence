"""Application factory."""

from __future__ import annotations

import logging
import time

from flask import Flask, g, request

from app.config import BaseConfig, load_config
from app.extensions import db, migrate
from app.utils.logging import bind_context, configure_logging, new_correlation_id, reset_context

log = logging.getLogger(__name__)

CORRELATION_HEADER = "X-Correlation-ID"


def create_app(config: BaseConfig | str | None = None, *, service_name: str = "api") -> Flask:
    cfg = config if isinstance(config, BaseConfig) else load_config(config)
    cfg.validate()

    app = Flask(__name__)
    app.config.from_mapping(cfg.as_flask_mapping())

    configure_logging(level=cfg.LOG_LEVEL, fmt=cfg.LOG_FORMAT, environment=cfg.ENV_NAME,
                      service=service_name, log_file=cfg.LOG_FILE)

    db.init_app(app)
    from app import models  # noqa: F401  (register tables for migrations)

    migrate.init_app(app, db, directory=str(_migrations_dir()))

    from app.tasks.celery_app import init_celery

    init_celery(app)

    from app.api import api_v1, register_error_handlers

    app.register_blueprint(api_v1)
    register_error_handlers(app)

    from app.dashboard import dashboard_bp
    from app.site import site_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(site_bp)

    if cfg.TRUSTED_PROXY_COUNT:
        from werkzeug.middleware.proxy_fix import ProxyFix

        n = cfg.TRUSTED_PROXY_COUNT
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)  # type: ignore[method-assign]

    from app.utils.time import PSX_TZ

    app.jinja_env.globals["psx_tz"] = PSX_TZ
    app.jinja_env.globals["asset"] = _asset_url(app)
    _install_static_cache_policy(app, debug=cfg.DEBUG)
    _install_request_hooks(app)
    _register_cli(app)
    return app


def _install_static_cache_policy(app: Flask, *, debug: bool) -> None:
    """Versioned URLs (?v=<content hash>, from `asset()`) are cached for a year; anything else (e.g. an ES
    module imported by another module) must revalidate every time (cheap 304 via ETag), so a deploy can
    never leave a stale module in a browser cache."""
    def max_age(filename: str | None) -> int | None:  # noqa: ARG001 - Flask's hook signature
        return 31_536_000 if (request.args.get("v") and not debug) else 0

    app.get_send_file_max_age = max_age  # type: ignore[method-assign]


def _asset_url(app: Flask):
    """url_for('static') with ?v=<content hash>, recomputed when the file's mtime changes."""
    import hashlib
    from pathlib import Path

    from flask import url_for

    cache: dict[str, tuple[float, str]] = {}
    root = Path(app.static_folder or "")

    def asset(filename: str) -> str:
        path = root / filename
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return url_for("static", filename=filename)
        hit = cache.get(filename)
        if hit is None or hit[0] != mtime:
            hit = (mtime, hashlib.sha256(path.read_bytes()).hexdigest()[:12])
            cache[filename] = hit
        return url_for("static", filename=filename, v=hit[1])

    return asset


def _migrations_dir():
    from app.config import PROJECT_ROOT

    return PROJECT_ROOT / "migrations"


def _install_request_hooks(app: Flask) -> None:
    @app.before_request
    def start_request() -> None:
        supplied = request.headers.get(CORRELATION_HEADER, "")
        # Accept a caller-supplied ID only if it is short and safe to log.
        cid = supplied if 8 <= len(supplied) <= 64 and supplied.replace("-", "").isalnum() else new_correlation_id()
        g.correlation_id = cid
        g.request_started = time.perf_counter()
        g.log_token = bind_context(correlation_id=cid)

    @app.after_request
    def finish_request(response):
        response.headers[CORRELATION_HEADER] = g.get("correlation_id", "")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if app.config["ENV_NAME"] == "production" and request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path != "/api/v1/health":
            duration = int((time.perf_counter() - g.get("request_started", time.perf_counter())) * 1000)
            log.info("request completed", extra={
                "event_type": "http_request", "duration_ms": duration, "status": response.status_code,
                "details": {"method": request.method, "path": request.path}})
        return response

    @app.teardown_request
    def clear_context(_exc) -> None:
        token = g.pop("log_token", None)
        if token is not None:
            reset_context(token)


def _register_cli(app: Flask) -> None:
    from app.cli import register_commands

    register_commands(app)
