# Architecture

PSX Intelligence is a **modular monolith with background workers**: one Python codebase,
deployed as three process types that share the code and the PostgreSQL database.

```
                 ┌───────────────────────────── one codebase (app/) ─────────────────────────────┐
 Browser ──────► │  api / dashboard (Flask, gunicorn)                                            │
 Operator CLI ─► │      │  thin handlers → services → repositories/ORM                           │
                 │      │  submits JobRun ──► Redis (broker) ──► Celery worker(s)               │
                 │      ▼                                          │                             │
                 │  PostgreSQL  ◄──────────────────────────────────┘  (all durable state)        │
                 │      ▲                                                                        │
                 │  Celery beat (schedules: heartbeat; later ingestion/evaluation if enabled)    │
                 └───────────────────────────────────────────────────────────────────────────────┘
 External sources ──► ingestion connectors (worker only) ──► validation ──► canonical tables
```

## Process types

| Process | Entrypoint | Responsibility |
|---|---|---|
| API + dashboard | `gunicorn wsgi:app` | REST API `/api/v1`, server-rendered dashboard. Never runs expensive analysis in a request. |
| Worker | `celery -A app.tasks.worker:celery worker` | Ingestion, document processing, analysis, outcome evaluation, candidate training. |
| Scheduler | `celery -A app.tasks.worker:celery beat` | Periodic triggers. Runs only a heartbeat until features are enabled by flags. |

## Layering (inside `app/`)

| Layer | Package | Rule |
|---|---|---|
| HTTP | `api/`, `dashboard/` | Parse and validate input (Pydantic), call a service, serialise. No business logic. |
| Contracts | `api/schemas.py`, `agents/contracts.py` | Pydantic models for external and inter-module contracts. |
| Services | `services/*` | Domain logic. Owns transactions. Framework-agnostic apart from the `db` session. |
| Analytical modules | `agents/*` | The five versioned modules. Pure functions of typed inputs → typed outputs, plus run records. |
| Persistence | `models/*`, `repositories/*` | ORM models and query helpers. |
| Tasks | `tasks/*` | Celery wrappers that bind work to a `JobRun` and call services. |
| Utilities | `utils/*` | Logging, time. No domain knowledge. |

## Data flow (target state)

1. **Ingestion** (worker): connector fetches or reads raw records → normalised records with
   `observed_at`/`published_at`/`retrieved_at` → validation → idempotent upsert into canonical
   tables, with an `ingestion_runs` row and `data_quality_issues` for anything flagged.
2. **Features**: point-in-time feature snapshots computed from canonical data with an explicit
   `as_of` cutoff and a feature-definition version.
3. **Modules 1–4** read features and canonical data up to the cutoff and write versioned
   results (`agent_runs`, `agent_signals`).
4. **Module 5** builds the forecast from features and module outputs with a registered model,
   persists an immutable `forecasts` row plus evidence references **before** the outcome exists.
5. **Evaluation** (scheduled): once the target session's validated close exists, a
   `forecast_outcomes` row is written. Forecasts are never modified.
6. **Training** (flag-gated): candidate models are trained and evaluated walk-forward. Promotion
   is a separate, audited, manual action.
7. **API/dashboard** read persisted results only, with freshness and status metadata.

## Cross-cutting concerns

- **Job lifecycle.** A `JobRun` row is written *before* enqueueing. States: `queued → running →
  succeeded | failed`, or `dispatch_failed` if the broker rejects the message. Retries apply only to
  `TransientJobError`, bounded with exponential backoff. Redelivered messages for finished jobs are
  skipped. Stuck jobs (queued > 15 min, running > 65 min) are surfaced in health and dashboard.
- **Correlation IDs.** Accepted from `X-Correlation-ID` when safe, otherwise generated. Stored on
  the `JobRun` and re-bound in the worker so a request and its background work share one ID.
- **Logging.** Structured JSON on stdout (optionally a rotating file), with secret redaction.
  Durable history (forecasts, audits, model registry) is in PostgreSQL, never only in logs.
- **Time.** Instants are UTC `timestamptz`. PSX session dates are exchange-local `date` values
  (Asia/Karachi), never derived by truncating a UTC timestamp.
- **Configuration.** Environment variables validated at startup, separate config classes per
  environment. Risky capabilities (scheduled ingestion, training, promotion) are off by default.

## What was deliberately not built

No microservices, Kubernetes, Kafka or vector database. PostgreSQL (with full-text search if needed)
covers the expected volumes: ten companies, daily bars, a few thousand documents. Revisit only
with a measured need. See ADR-001 and ADR-002.
