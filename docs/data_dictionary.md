# Data dictionary

Section A lists tables that **exist** (created by migrations). Section B is the planned design,
refined as each phase is implemented. Conventions:

- Instants are `timestamptz` (UTC). Session dates are `date` in exchange-local time.
- Money is `numeric(20,6)` with explicit `currency` (ISO 4217, normally `PKR`) and `unit`
  (`unit`, `thousand`, `million`).
- Missing values are SQL `NULL` plus, where relevant, a `missing_reason`. Zero is never a
  placeholder.
- `recorded_at` = when this application persisted the row.

## A. Implemented

### `job_runs` (migration `6f60f0960c4f`)
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| job_type | varchar(64) | Must be a key of `JOB_TASKS` |
| status | enum `job_status` | `queued`, `running`, `succeeded`, `failed`, `dispatch_failed` |
| idempotency_key | varchar(200) unique, null | Client-supplied. Duplicate submissions return the existing job |
| celery_task_id | varchar(64) | |
| correlation_id | varchar(64) | Shared with the originating request |
| requested_by | varchar(100) | `admin`, `cli`, `scheduler` |
| parameters | jsonb | Job input (validated per job type) |
| result_summary | jsonb, null | Small summary only. Real results go to domain tables |
| error_category | varchar(64) | `transient`, `permanent`, `unexpected`, `dispatch_error` |
| error_message | text | Redacted, ≤ 2000 chars |
| attempts | int | Incremented on each start |
| created_at / updated_at / started_at / finished_at | timestamptz | |

Indexes: `(status, created_at)`, `(job_type, created_at)`.

### `audit_events` (migration `6f60f0960c4f`)
Append-only. `id bigint PK`, `occurred_at`, `actor`, `action` (e.g. `job.submitted`),
`entity_type`, `entity_id`, `correlation_id`, `details jsonb`. Indexes on `(entity_type, entity_id)`
and `occurred_at`. Written in the same transaction as the change it records.

### `data_sources` (migration `1cd4e0807ecf`)
Source registry. `source_id` PK (e.g. `psxdata_api`), `source_name`, `dataset_type`,
`access_method`, `terms_reference`, `license_status`, `enabled`, `refresh_frequency`, plus runtime
state: `last_successful_sync`, `last_attempted_sync`, `last_error`, `data_quality_status`
(`unknown`|`ok`|`degraded`|`failing`). The descriptive columns are upserted from code and config on
each run.

### `ingestion_runs` (migration `1cd4e0807ecf`)
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| source_id | FK data_sources | |
| dataset | varchar(64) | `daily_bars` or `quotes` |
| job_run_id | FK job_runs, null | The background job that performed the run |
| status | enum `ingestion_status` | `running`, `succeeded`, `partially_succeeded`, `failed` |
| started_at / finished_at | timestamptz | |
| records_received | int | Items in the provider response |
| records_rejected | int | Failed validation and quarantined (tracked symbols, or unreadable symbol) |
| records_filtered | int | Skipped by design (e.g. no quotes for indices) |
| records_accepted | int | Valid records |
| records_stored | int | Rows inserted (new or revised). Unchanged re-fetches are not re-stored |
| error_category / error_summary | | e.g. `rate_limited`, `provider_unavailable`, `upstream_data_error`, `not_found`, `invalid_response`, `database_error`, `all_symbols_failed`, `abandoned`, `partial` |
| request_metadata | jsonb | Requested symbols, `full_history`, index lookup result, and per-symbol outcome (status, received, inserted, revised, unchanged, flagged, provider_cached, error). **Never headers or credentials** |

Indexes: `(source_id, dataset, started_at)`, `(status)`, and **partial unique**
`(source_id, dataset) WHERE status='running'`, which prevents overlapping runs.

### `ingestion_rejections` (migration `1cd4e0807ecf`)
`id`, `ingestion_run_id` (FK, cascade), `record_index`, `symbol` (null if unreadable), `reason`,
`raw_record` jsonb (truncated to 1 KB). At most 200 per run.

### `instruments` (migration `3acec32b200d`)
`symbol` PK, `instrument_kind` (`stock`|`index`, from psxdata-api `/indices`), `source_id`,
`first_seen_at`, `updated_at`.

### `daily_bars` (migration `3acec32b200d`)
| Column | Type | Notes |
|---|---|---|
| id | bigint PK | |
| source_id | FK data_sources | `psxdata_api` |
| symbol | varchar(32) | |
| session_date | date | PSX calendar date (exchange-local), as published |
| open / high / low / close | numeric(20,6) | **Unadjusted.** All > 0 and high ≥ low (DB check constraints) |
| volume | bigint | ≥ 0 |
| quality_flags | jsonb list | `provider_anomaly`, `open_outside_range`, `close_outside_range`. Empty when clean |
| revision | int | 1 on first sight. +1 when a re-fetch returns different values |
| is_current | bool | Exactly one current row per (source, symbol, session_date) (partial unique index) |
| superseded_at | timestamptz, null | When a newer revision replaced this row |
| ingestion_run_id | FK ingestion_runs | Provenance |
| retrieved_at / recorded_at | timestamptz | |

Unique `(source_id, symbol, session_date, revision)`. Index `(symbol, session_date)`. About 250
rows per symbol per year.

### `quote_snapshots` (migration `3acec32b200d`)
`symbol`, `price` (> 0), `change`, `change_pct` (percent units), `ldcp`, `pe_ratio`,
`dividend_yield`, `market_cap`, `volume_avg_30d`, `change_1y_pct` (all nullable, as reported),
`provider_cached` (psxdata `meta.cached`), `retrieved_at` (**the only timestamp: the source gives
no exchange time**), `ingestion_run_id`, `source_id`. A row is added only when values changed
since the symbol's previous snapshot. Unique `(ingestion_run_id, symbol)`.

`ticker_snapshots` (Capital Stake) was dropped in `3acec32b200d`, together with the
`ingestion_runs` columns `response_sha256`, `accepted_fingerprint` and `duplicate_of_run_id`.

## B. Planned

### Reference and sources (Phase 2)
- **companies**: `id`, `name`, `sector`, `industry`, `notes`.
- **instruments**: `id`, `company_id` FK, `symbol`, `instrument_type`, `status`
  (`active`|`suspended`|`delisted`|`unverified`), `valid_from`, `valid_to`. Unique
  `(symbol, valid_from)`. Supports symbol history.
- **watchlist**: `instrument_id` FK unique, `added_at`, `active`, `min_history_sessions`,
  `eligibility_overrides jsonb`.
- **data_quality_issues**: `id`, `ingestion_run_id` FK, `entity_type`, `entity_key`, `rule`,
  `severity` (`warning`|`error`), `message`, `detected_at`, `resolved_at`.

### Market data (Phase 2)
- **market_calendar_sessions**: `session_date` PK, `is_trading_day`, `session_type`
  (`normal`|`half_day`), `source_id`, `notes`.
- **market_prices**: `instrument_id`, `session_date`, `open/high/low/close` numeric, `volume`
  bigint, `turnover` numeric null, `adjustment_basis` (`raw`|`vendor_adjusted`), `source_id`,
  `ingestion_run_id`, `observed_at`, `retrieved_at`, `recorded_at`, `revision` int, `is_current`.
  Unique `(instrument_id, session_date, source_id, revision)`. Partial unique index on current rows.
  Corrections insert a new revision and keep the old one.
- **market_indices**: same shape keyed by `index_symbol` (KSE100, KSE30, KMI30, …).
- **corporate_actions**: `instrument_id`, `action_type`, `ex_date`, `ratio`/`amount`,
  `announced_at`, `source_id`.

### Research data (Phase 4)
`news_articles`, `news_events`, `financial_documents`, `financial_statements`,
`financial_metrics` (original and normalised value/unit, currency, page/table ref, extraction
method, confidence, validation status), `financial_ratios`, `feature_snapshots`.

### Analysis and forecasting (Phases 3, 5, 6)
`analysis_runs`, `agent_runs`, `agent_signals`, `forecasts` (immutable; unique
`(analysis_run_id, model_version_id, instrument_id, origin_session, horizon)`),
`forecast_evidence`, `forecast_outcomes`, `model_versions`, `model_evaluations`, `training_runs`.
