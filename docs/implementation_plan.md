# Implementation plan and status

| Phase | Scope | Status |
|---|---|---|
| 0 | Repository assessment, architecture, ADRs, data-source assessment, forecast semantics | **Done** (2026-10-13) |
| 1 | App factory, config, PostgreSQL + migrations, Redis, Celery + job model, JSON logging, correlation IDs, health, dashboard shell, Docker, tests | **Done** (Docker image not built locally: daemon was not running) |
| 2 | psxdata-api ingestion: daily OHLCV history + revisions, quotes, instruments, scheduler, sync API, Overview with charts | **Done. Live: 12,364 bars loaded 2026-10-13** (Capital Stake version removed) |
| 2b | Trading calendar (holidays), corporate actions / adjustment factors, company and sector records | Next. **No corporate-action source yet** (blocks adjusted returns) |
| 3 | Module 1 (market regime), Module 2 (technical), module run records, API | Planned |
| 4 | Announcements/news ingestion (pending licences), PDF extraction, fundamentals (Module 3, 4) | Planned, partly blocked |
| 5 | Point-in-time features, baselines, walk-forward evaluation, forecast persistence (Module 5) | Planned |
| 6 | Outcome evaluation, model registry, candidate training, approval/rollback | Planned |
| 7 | Dashboard pages 1–5, OpenAPI document, pagination/freshness everywhere | Planned |
| 8 | Hardening: auth, rate limits, SRI, backup drill, egress controls, release docs | Planned |

## Repository state at start (Phase 0)

The `financial-advisor` directory contained only a Node-style `.gitignore`. It is not a git
repository and had no existing code to preserve. The `.gitignore` was replaced with a Python one.

## Unresolved decisions (need an owner)

| # | Decision | Blocks | Default if no answer |
|---|---|---|---|
| U1 | ~~How market data will be obtained~~ **psxdata-api, self-hosted (owner decision 2026-10-13)**, owner accepts PSX-terms risk (ADR-012) | | |
| U9 | Source of corporate actions (splits, bonus, dividends) for adjusted returns: psxdata has none | Forecast targets (ADR-009) | Flag windows containing large unexplained gaps; no adjustment |
| U2 | Licensed feed's format, adjustment policy and corporate-action fields | Final target definition (ADR-009) | Store raw closes + own adjustments |
| U3 | Permitted use: personal research vs. internal commercial vs. redistribution | Display/retention rules, exposure of dashboard | Personal research, localhost only |
| U4 | News sources and licences | Module 3 beyond PSX announcements | Module 3 limited to announcements/operator items |
| U5 | Macro data access (SBP EasyData API vs. manual entry) | Module 1 macro features | Manual entry of policy rate / CPI with document refs |
| U6 | Intended users and deployment (single analyst on a laptop vs. shared server) | Auth design (ADR-008) | Single trusted user |
| U7 | Watchlist confirmation: `ENGROH` and others must be verified against current PSX listings | Phase 2 seed data | Load with `status=unverified` until verified from a permitted source |
| U8 | LLM use for synthesis (provider, budget, data-sharing constraints) | Module 5 narrative | Deterministic template narrative, no LLM |

## Phase 2 detailed plan

1. Models + migration: `data_sources`, `companies`, `instruments`, `watchlist`,
   `market_calendar_sessions`, `ingestion_runs`, `data_quality_issues`, `market_prices`,
   `market_indices`.
2. `config/sources.yaml` and `config/watchlist.yaml` with CLI sync commands (`flask sync-sources`,
   `flask sync-watchlist`). All ten symbols are seeded as `unverified`.
3. Canonical CSV import format (documented), connector interface (`MarketDataConnector`), and
   `FileImportConnector`. `LicensedFeedConnector` raises `SourceNotConfigured`.
4. Validation rules (OHLC consistency, positive prices, duplicates, calendar gaps, implausible
   moves) → `data_quality_issues` with severity.
5. Idempotent upsert with revisions. Re-importing the same file is a no-op. A changed value
   creates a new revision.
6. Ingestion job type + API (`GET/POST /api/v1/ingestion/runs`, `GET /api/v1/watchlist`,
   `GET /api/v1/stocks`, `/stocks/<symbol>/history`) with freshness metadata.
7. Tests with `TEST_` fixtures.
