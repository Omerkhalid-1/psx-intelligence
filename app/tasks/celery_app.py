"""Celery instance and Flask integration.

The worker entrypoint is `app.tasks.worker:celery`, which builds the Flask app so every
task runs inside an application context (database session, config, logging).
"""

from __future__ import annotations

from celery import Celery, Task
from celery.schedules import crontab
from flask import Flask

celery = Celery("psx_intelligence")

# Task modules registered with the worker. Add new modules here.
TASK_MODULES = ["app.tasks.system_tasks", "app.tasks.ingestion_tasks", "app.tasks.research_tasks"]

HEARTBEAT_INTERVAL_SECONDS = 60.0


class FlaskTask(Task):
    """Runs every task inside the most recently initialised Flask app's context."""

    def __call__(self, *args, **kwargs):
        with celery.flask_app.app_context():
            return self.run(*args, **kwargs)


celery.Task = FlaskTask


def _beat_schedule(app: Flask) -> dict:
    schedule = {
        "scheduler-heartbeat": {"task": "system.scheduler_heartbeat", "schedule": HEARTBEAT_INTERVAL_SECONDS},
    }
    # Market-data jobs exist in the schedule only when explicitly enabled; each trigger task
    # re-checks configuration (and, for quotes, market hours) on every tick.
    if app.config["ENABLE_SCHEDULED_INGESTION"] and app.config["PSXDATA_ENABLED"]:
        hour, minute = app.config["PSXDATA_DAILY_SYNC_TIME"].split(":")
        if app.config["ENABLE_RESEARCH_PIPELINE"]:
            # The research pipeline ingests bars itself (step market_data), so it replaces the
            # standalone daily-bar job rather than duplicating its requests.
            p_hour, p_minute = app.config["RESEARCH_PIPELINE_TIME"].split(":")
            schedule["research-pipeline"] = {
                "task": "pipeline.schedule_daily",
                "schedule": crontab(hour=int(p_hour), minute=int(p_minute), day_of_week="mon-fri"),
            }
        else:
            schedule["psx-daily-bars"] = {
                "task": "ingestion.schedule_psx_daily_bars",
                # Evaluated in the Celery timezone (Asia/Karachi), i.e. PSX local time.
                "schedule": crontab(hour=int(hour), minute=int(minute), day_of_week="mon-fri"),
            }
        schedule["psx-quotes"] = {
            "task": "ingestion.schedule_psx_quotes",
            "schedule": float(app.config["PSXDATA_QUOTE_POLL_INTERVAL_MINUTES"]) * 60,
        }
    return schedule


def init_celery(app: Flask) -> Celery:
    celery.conf.update(
        broker_url=app.config["CELERY_BROKER_URL"],
        result_backend=app.config["CELERY_RESULT_BACKEND"],
        task_always_eager=app.config["CELERY_TASK_ALWAYS_EAGER"],
        task_eager_propagates=False,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        # Crontab schedules are written in PSX local time; timestamps stay UTC (enable_utc).
        timezone="Asia/Karachi",
        enable_utc=True,
        # Redelivery on worker crash: tasks must be idempotent (they are keyed by job_id).
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_time_limit=60 * 60,
        task_soft_time_limit=55 * 60,
        result_expires=24 * 3600,
        broker_connection_retry_on_startup=True,
        worker_hijack_root_logger=False,
        beat_schedule=_beat_schedule(app),
    )
    celery.flask_app = app
    celery.set_default()
    for module in TASK_MODULES:
        __import__(module)
    app.extensions["celery"] = celery
    return celery
