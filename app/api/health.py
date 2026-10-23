from __future__ import annotations

from flask import current_app

from app.api import api_v1
from app.api.responses import ok
from app.models.market_data import IngestionStatus
from app.services import health
from app.services.market_data import bars_freshness, integration_status, quotes_freshness
from app.services.orchestration.jobs import find_stuck_jobs


@api_v1.get("/health")
def liveness():
    """Liveness: the API process is up. Does not touch dependencies."""
    return ok({"status": "ok", "service": "api", "environment": current_app.config["ENV_NAME"]})


@api_v1.get("/health/dependencies")
def dependencies():
    checks = health.check_all()
    status = health.overall_status(checks)
    warnings = [f"{c.name}: {c.status}" + (f" ({c.detail})" if c.detail else "")
                for c in checks if c.status != "ok"]
    stuck = find_stuck_jobs() if status != "unavailable" else []
    if stuck:
        warnings.append(f"{len(stuck)} job(s) appear stuck (queued or running far longer than expected)")
    data = {"status": status, "dependencies": [c.model_dump() for c in checks],
            "jobs": {"stuck": [str(j.id) for j in stuck]}}
    if status != "unavailable":
        data["data_sources"], source_warnings = _data_source_health()
        warnings.extend(source_warnings)
    body, _ = ok(data, warnings=warnings)
    return body, (200 if status != "unavailable" else 503)


def _data_source_health() -> tuple[list[dict], list[str]]:
    """Data freshness is reported alongside, not inside, dependency status: stale data is a
    data problem, not an outage, and a deliberately disabled source is not a fault."""
    config = current_app.config
    status = integration_status(config)
    datasets = [bars_freshness(config, status), quotes_freshness(config, status)]
    warnings = []
    if status.enabled:
        for f in datasets:
            if f.state == "stale":
                warnings.append(f"psxdata {f.dataset}: stale ({f.detail})")
            if f.last_run is not None and f.last_run.status == IngestionStatus.FAILED:
                warnings.append(f"psxdata {f.dataset}: last sync failed ({f.last_run.error_category})")
    return [{"source_id": "psxdata_api", "integration": status.as_dict(),
             "datasets": {f.dataset: f.as_dict() for f in datasets}}], warnings
