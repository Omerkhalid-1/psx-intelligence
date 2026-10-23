"""On-demand "get the latest information" refresh: live quotes, then the daily research pipeline.

One refresh = one quote sync (latest traded prices) followed by one pipeline run, which fetches new
daily bars and news and, only if a new market session arrived, recomputes analysis and forecasts.
Each part is attempted independently so a quote outage does not block the bar/news refresh.
Nothing here is new logic: it composes the existing, already-guarded ingestion and pipeline code
(single-flight locks, rate spacing, insert-only forecasts).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from app.utils.logging import redact

log = logging.getLogger(__name__)

REFRESH_JOB = "refresh.latest"
# Minimum gap between manual refreshes: every refresh ends up as requests to PSX and Google News.
REFRESH_COOLDOWN_SECONDS = 120


def run_refresh(config: Mapping[str, Any], *, job_run_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Run quotes then the pipeline. Returns a per-part summary; never raises for a single part."""
    from app.services.ingestion.psx import IngestionAlreadyRunning, run_quote_ingestion
    from app.services.market_data import integration_status
    from app.services.pipeline import PipelineBusy, run_daily_pipeline

    out: dict[str, Any] = {}

    status = integration_status(config)
    if not status.ready:
        out["quotes"] = {"status": "skipped", "message": "psxdata integration not ready: " + "; ".join(status.problems)}
    else:
        try:
            q = run_quote_ingestion(config, job_run_id=job_run_id)
            out["quotes"] = {"status": q.status.value, "stored": q.records_stored, "ingestion_run_id": str(q.id),
                             "message": q.error_summary}
        except IngestionAlreadyRunning as exc:
            out["quotes"] = {"status": "skipped", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - one part failing must not stop the other
            log.exception("refresh: quote sync failed", extra={"event_type": "refresh_part_failed"})
            out["quotes"] = {"status": "failed", "message": redact(f"{type(exc).__name__}: {exc}")[:300]}

    try:
        run = run_daily_pipeline(config, job_run_id=job_run_id)
        out["pipeline"] = {"status": run.status, "pipeline_run_id": str(run.id),
                           "session": run.session_date.isoformat() if run.session_date else None,
                           "steps": {s["step"]: s["status"] for s in run.steps},
                           "new_session": any(s["step"] == "features" and s["status"] == "succeeded"
                                              for s in run.steps)}
    except PipelineBusy as exc:
        out["pipeline"] = {"status": "skipped", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("refresh: pipeline failed", extra={"event_type": "refresh_part_failed"})
        out["pipeline"] = {"status": "failed", "message": redact(f"{type(exc).__name__}: {exc}")[:300]}

    out["status"] = overall_status(out)
    return out


def overall_status(out: Mapping[str, Any]) -> str:
    parts = [out.get("quotes", {}).get("status"), out.get("pipeline", {}).get("status")]
    if all(p == "failed" for p in parts):
        return "failed"
    if any(p in ("failed", "partial", "partially_succeeded") for p in parts):
        return "partial"
    return "succeeded"
