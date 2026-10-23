# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser . .
RUN mkdir -p logs artifacts data/imports && chown -R appuser:appuser logs artifacts data
USER appuser

EXPOSE 8000
# Overridden per service in compose.yaml (worker / beat / migrate).
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60", "--access-logfile", "-", "wsgi:app"]
