"""Public website: market pulse, per-stock indicator dashboards, "how a stock works", PSX news
search and the analysis-engine page. Read-only; every number comes from the analysis engine.

Pages load only same-origin scripts (vendored libraries included), so a strict Content Security
Policy applies to every site response.
"""

from flask import Blueprint

site_bp = Blueprint("site", __name__)

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    # Inline style attributes are used by the chart library and animations; no inline scripts anywhere.
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


@site_bp.app_context_processor
def _watch_meta():
    """`watch_meta()` for the navigation; a callable so pages that don't render it pay nothing."""
    from flask import current_app

    from app.site.data import watch_meta

    def meta():
        try:
            return watch_meta(current_app.config)
        except Exception:  # noqa: BLE001 - navigation must render even if metadata is unavailable
            return [{"symbol": s, "name": s, "sector": "Watchlist", "is_index": s == "KSE100"}
                    for s in current_app.config["PSXDATA_SYMBOLS"]]
    static = bool(current_app.config.get("SITE_STATIC_EXPORT"))
    return {"watch_meta": meta, "static_mode": static, "site_build": current_app.config.get("SITE_BUILD_INFO") or {},
            # GitHub Pages cannot send headers: the static edition carries its policy in a <meta> tag
            # (frame-ancestors is not supported there and is dropped).
            "static_csp": "; ".join(d for d in CSP.split("; ") if not d.startswith("frame-ancestors"))}


@site_bp.after_request
def _security_headers(response):
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    return response


from app.site import routes  # noqa: E402,F401
