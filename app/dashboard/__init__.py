"""Operations dashboard (/ops): ingestion status, system health, research pipeline and models.

When OPS_DASHBOARD_AUTH is on (default in production) every page requires HTTP Basic auth with
the admin token as the password (any username). The public website lives in app.site.
"""

import hmac

from flask import Blueprint, Response, current_app, request

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/ops")


@dashboard_bp.before_request
def _require_ops_auth():
    if not current_app.config.get("OPS_DASHBOARD_AUTH"):
        return None
    expected = current_app.config.get("ADMIN_API_TOKEN") or ""
    auth = request.authorization
    supplied = (auth.password or "") if auth and auth.type == "basic" else ""
    if expected and supplied and hmac.compare_digest(supplied.encode(), expected.encode()):
        return None
    return Response("Operations dashboard: authentication required.", 401,
                    {"WWW-Authenticate": 'Basic realm="PSX Intelligence ops", charset="UTF-8"'})


from app.dashboard import routes  # noqa: E402,F401
