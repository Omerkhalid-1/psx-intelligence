"""Operator CLI commands (`flask --app wsgi <command>`)."""

from __future__ import annotations

import json

import click
from flask import Flask, current_app

from app.services.orchestration import jobs


def register_commands(app: Flask) -> None:
    from app.cli_research import register_research_commands
    from app.site.cli import register_site_commands

    register_research_commands(app)
    register_site_commands(app)

    @app.cli.command("submit-job")
    @click.argument("job_type")
    @click.option("--params", default="{}", help="JSON object of job parameters")
    def submit_job_cmd(job_type: str, params: str) -> None:
        """Submit a background job and print its id and status (not its completion)."""
        try:
            parameters = json.loads(params)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"--params is not valid JSON: {exc}") from None
        try:
            result = jobs.submit_job(job_type, parameters=parameters, requested_by="cli")
        except jobs.UnknownJobType:
            raise click.BadParameter(f"unknown job type; allowed: {sorted(jobs.JOB_TASKS)}") from None
        click.echo(json.dumps({"job_id": str(result.job.id), "status": result.job.status.value}))

    @app.cli.command("ingest-psx")
    @click.argument("dataset", type=click.Choice(["bars", "quotes"]))
    @click.option("--full", is_flag=True, help="bars: reload all history (from PSXDATA_BACKFILL_START if set).")
    @click.option("--symbols", default="", help="Comma-separated subset of PSXDATA_SYMBOLS (default: all).")
    @click.option("--sync", "run_sync", is_flag=True,
                  help="Run in this process instead of queueing for a worker (same job record and code path).")
    def ingest_psx_cmd(dataset: str, full: bool, symbols: str, run_sync: bool) -> None:
        """Fetch daily bars or quotes from psxdata-api and store them."""
        from app.extensions import db
        from app.models.operations import JobRun
        from app.services.market_data import integration_status
        from app.tasks.celery_app import celery
        from app.tasks.ingestion_tasks import BARS_JOB, QUOTES_JOB

        status = integration_status(current_app.config)
        if not status.ready:
            raise click.ClickException("psxdata integration not ready: " + "; ".join(status.problems))
        if full and dataset != "bars":
            raise click.BadParameter("--full applies only to bars")
        chosen = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        unknown = sorted(set(chosen) - set(current_app.config["PSXDATA_SYMBOLS"]))
        if unknown:
            raise click.BadParameter(f"not in PSXDATA_SYMBOLS: {', '.join(unknown)}")
        job_type = BARS_JOB if dataset == "bars" else QUOTES_JOB
        params: dict = {"full_history": full} if dataset == "bars" else {}
        if chosen:
            params["symbols"] = chosen
        result = jobs.submit_job(job_type, parameters=params, requested_by="cli", dispatch=not run_sync)
        job_id = result.job.id
        if run_sync:
            celery.tasks[job_type].apply(kwargs={"job_id": str(job_id)})
            db.session.expire_all()
            job = db.session.get(JobRun, job_id)
            click.echo(json.dumps({"job_id": str(job_id), "status": job.status.value,
                                   "result": job.result_summary, "error": job.error_message}, indent=2))
            if job.status.value != "succeeded":
                raise SystemExit(1)
        else:
            click.echo(json.dumps({"job_id": str(job_id), "status": result.job.status.value,
                                   "poll": f"/api/v1/jobs/{job_id}"}))
