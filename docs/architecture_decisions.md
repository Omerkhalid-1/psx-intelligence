# Architecture decision records

Format: context → decision → alternatives → consequences. Status is `accepted` unless noted.

## ADR-001 Modular monolith + Celery workers
- **Context:** small, single-team project. Workloads are batch-oriented (daily data).
- **Decision:** one codebase, three process types (API, worker, beat), shared PostgreSQL.
- **Alternatives:** microservices (operational overhead without benefit at this scale); a single
  process with threads (expensive analysis would block web requests).
- **Consequences:** simple deploys and refactors. Module boundaries are kept by package
  discipline and contracts rather than by network boundaries.

## ADR-002 PostgreSQL is the only durable store; Redis is ephemeral
- **Decision:** forecasts, evidence, model registry, job and audit history live in PostgreSQL.
  Redis holds only broker messages, task results (expire after 24h) and the scheduler heartbeat.
- **Consequences:** losing Redis loses in-flight queue messages only. The `JobRun` table still
  shows those jobs, and stuck-job detection surfaces them.

## ADR-003 Jobs are persisted before dispatch
- **Decision:** `JobRun` is committed, then enqueued. Broker failure produces `dispatch_failed`.
  The API returns `202` with the job record, and clients poll `/api/v1/jobs/<id>`.
- **Alternative:** rely on Celery result backend state. Rejected because it is ephemeral and
  cannot express "accepted but never dispatched".
- **Consequences:** a small window exists where a job is `queued` but its message was lost
  (e.g. Redis restart). Stuck-job detection covers this.

## ADR-004 No scraping of the PSX Data Portal; file import first, licensed feed as target (**superseded by ADR-012**)
- **Context:** DPS terms (read 2026-10-13) prohibit robots and automated tools without written
  permission and restrict reuse. PSX sells EOD/Historical/PUCARS licences.
- **Decision:** the first market-data connector imports operator-supplied CSV files with recorded
  provenance. A licensed-feed adapter interface is defined, and the real implementation waits for
  vendor documentation.
- **Alternatives:** scraping DPS JSON endpoints (violates terms); third-party unofficial APIs
  (licensing unclear).
- **Consequences:** no live data until an operator supplies files or a licence is obtained. The
  dashboard shows "no data" states, never synthetic data.

## ADR-005 Celery 5.5.3 with billiard pinned to 4.2.1
- **Context:** billiard 4.3.x switched macOS to the `spawn` start method. The prefork pool then
  fails every task with `ValueError: not enough values to unpack` (reproduced locally).
- **Decision:** pin `billiard==4.2.1`. Linux/Docker deployments are unaffected either way.
- **Revisit:** when a Celery release supports spawn-based prefork on macOS.

## ADR-006 Migrations via Flask-Migrate (Alembic), tests run real migrations
- **Decision:** schema changes only through Alembic revisions. The test session drops the schema
  and runs `upgrade()`, so drift between models and migrations fails tests. Enum types are
  dropped explicitly in downgrades.

## ADR-007 Quantitative models produce numbers; LLMs never do
- **Decision:** every forecast number comes from a registered, versioned quantitative model. LLMs,
  if adopted, may only summarise validated structured outputs, with schema-validated output and
  numeric cross-checks against source records.

## ADR-008 Single bearer token for admin/job endpoints (initial release)
- **Decision:** `ADMIN_API_TOKEN` protects mutating and job-triggering endpoints. Constant-time
  compare. Endpoints return 503 if no token is configured (fail closed). Read-only endpoints and
  the dashboard are unauthenticated and **must not be exposed beyond a trusted network** until
  user authentication exists.
- **Revisit:** before any internet-facing deployment (Phase 8). Options: reverse-proxy SSO, or
  Flask-Login with per-user roles.

## ADR-009 Forecast target: price return on adjusted close (provisional)
- See `forecasting_methodology.md`. Provisional until the licensed data's corporate-action and
  adjustment fields are known. If only unadjusted prices are available, the target is computed
  from our own corporate-action adjustments, and forecasts across an unhandled corporate action
  are marked `invalid`.

## ADR-010 Point-in-time correctness enforced by `as_of` parameters, not by convention
- **Decision:** every feature or module function takes an explicit `as_of` session and queries
  with `published_at <= cutoff` / `session_date <= as_of`. Leakage tests (Phase 5) construct
  datasets with future rows and assert they never influence outputs.

## ADR-011 Capital Stake ticker ingestion (**withdrawn**)
Built 2026-10-13 and removed the same day at the owner's request (ADR-012). Never ran against
live data. Its design principles (adapter boundary, Decimal parsing, DB-enforced overlap guard,
bounded client-side retries, admin-only trigger with cooldown) carried over to ADR-013.

## ADR-012 Market data from self-hosted psxdata-api (owner decision)
- **Context:** the owner set a hard requirement to use psxdata-api / psxdata. Reading psxdata's
  source showed that it reads `dps.psx.com.pk` with a browser User-Agent, which PSX's terms
  restrict (ADR-004).
- **Decision:** use psxdata-api, **self-hosted** (local virtualenv via
  `services/psxdata-api/run.sh`, or Docker image `mtauha/psxdata-api:0.5.0`), pinned to v0.5.0 /
  psxdata 1.2.0. Call it from the backend only. The owner accepts the terms risk, recorded as
  `license_status = owner_accepted_risk` and in docs/data_sources.md.
- **Alternatives:** the hosted copy (rejected: third-party dependency and telemetry); importing
  `psxdata` directly into the app (rejected: the owner asked for psxdata-api, and a separate
  process isolates scraping code, dependencies and breakage); Capital Stake (licensed; removed by
  the owner).
- **Consequences:** data can break whenever PSX changes its site. The daily sync and the opt-in
  contract test surface this loudly. Prices are unadjusted with no corporate-action feed, which
  blocks correct return calculations across splits and bonus issues.

## ADR-013 psxdata ingestion design
- **Process boundary:** psxdata-api is a separate process. The app talks HTTP to it through
  `PsxDataApiClient`, the only code that knows its wire format. Everything else uses
  `DailyBarObservation` / `QuoteObservation`.
- **Storage:** daily bars are the system of record for history (`daily_bars`, keyed by source,
  symbol and session date). Quotes go to `quote_snapshots` (append-only, deduplicated per symbol).
  Pages and the API read only PostgreSQL. Only sync jobs call psxdata-api.
- **Revisions, not overwrites:** a changed bar gets a new revision and the old one is retired, so
  evidence behind past analysis survives.
- **Validation:** hard failures are rejected and quarantined (non-positive or non-finite prices,
  high below low, negative or fractional volume, future or invalid dates, duplicate dates).
  Plausible-but-suspect bars are **kept with `quality_flags`** (provider anomaly, open or close
  outside the range), so history has no silent gaps and analysis can exclude them explicitly.
- **Rate and politeness:** requests are spaced ≥ 1 s apart (psxdata-api allows 60/min). Bounded
  client retries honour Retry-After. A service outage aborts the remaining symbols. Sync jobs have
  `max_retries=0`. Daily bars run on weekdays at 18:00 PKT; quotes every 15 min within
  09:15–17:00 PKT. Incremental syncs re-fetch a 10-day overlap.
- **Isolation:** each symbol commits separately. One bad symbol never discards another's data.
- **Overlap guard:** a partial unique index allows only one running run per dataset.
