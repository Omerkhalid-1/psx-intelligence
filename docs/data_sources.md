# Data sources

Status of every candidate source **as verified on 2026-10-13**. "Verified" means a page was
fetched and read; it does not mean a licence has been obtained. **psxdata-api is operational**; other
sources are not. Ingestion connectors are built in Phase 2 against the access methods below.

## Summary

| source_id | Provider | Dataset | Access method decided | Licence status | Operational |
|---|---|---|---|---|---|
| `psxdata_api` | Self-hosted psxdata-api (psxdata library → dps.psx.com.pk) | Daily OHLCV (about 10 years), quotes, index list | Self-hosted REST wrapper | **Owner accepted the PSX-terms risk** (see below) | **Live: 12,364 bars loaded 2026-10-13** |
| `capital_stake` | Capital Stake (PSX-authorised redistributor) | Tickers, EOD, fundamentals | Licensed REST API | Not obtained | Removed (owner's request) |
| `psx_licensed_eod` | PSX Market Data (or authorised redistributor) | EOD OHLCV, indices, historical | Licensed feed: adapter interface only | **Not obtained** | No |
| `manual_file_import` | Operator-supplied files | EOD OHLCV / index CSV | Local file import | Depends on how the operator got the file | Phase 2 |
| `psx_dps_portal` | dps.psx.com.pk | Quotes, historical, announcements | **None: automated access prohibited** | n/a | Never (as is) |
| `psx_financials_portal` | financials.psx.com.pk | Company financial report PDFs | To be decided | Unclear | No |
| `psx_announcements` | PSX PUCARS / DPS announcements | Corporate announcements | Licensed product (PUCARS) or permission | **Not obtained** | No |
| `sbp_macro` | State Bank of Pakistan | Policy rate, FX, monetary data | To be decided (EasyData portal unverified) | Unverified | No |
| `pbs_cpi` | Pakistan Bureau of Statistics | CPI inflation | Published releases (manual) | Public publication; terms unverified | No |
| `news_*` | Business Recorder, Mettis Global, Dawn, The News | News | Requires an agreement or a licensed feed | **Not obtained** | No |

## Findings by source

### psxdata-api (`psxdata_api`), the active source

**Decision (2026-10-13, project owner):** market data comes from
[psxdata-api](https://github.com/mtauha/psxdata-api), self-hosted, wrapping the
[psxdata](https://pypi.org/project/psxdata/) library. This replaced the Capital Stake
integration, which had been built but never run live. See ADR-012.

**How it gets data (verified by reading psxdata 1.2.0's source):** psxdata requests pages and
JSON from `dps.psx.com.pk` (`/historical`, `/symbols`, `/indices/{name}`, `/screener`,
`/financial-reports-list`, `/trading-board`, `/sector-summary`, `/debt-market`,
`/eligible-scrips`) and sends a desktop-browser `User-Agent`. **PSX's DPS terms (see below)
prohibit automated access without written permission and restrict reuse. The project owner
accepted this risk.** Recorded as `license_status = owner_accepted_risk`. Mitigations here:
- One self-hosted instance with paced requests (≥ 1 s apart), a daily bar sync, quotes only
  in market hours, and incremental fetches. psxdata-api also caches.
- Bound to localhost (or the compose network only). The hosted copy
  (psxdata-api.fastapicloud.dev) is **not** used: it logs request telemetry on the author's
  infrastructure, and we should not depend on someone else's server.
- Data stays private: the dashboard is localhost-only, and nothing is redistributed.
- If PSX objects, or written permission or a licence becomes available, swap the provider. The
  adapter boundary (`MarketDataProvider`) makes that a contained change.

**Pinned versions:** psxdata-api `v0.5.0` (git commit `051a88b`, Docker `mtauha/psxdata-api:0.5.0`),
which pins `psxdata==1.2.0`. Both are small single-maintainer projects (psxdata reached 1.0 in
2026), so upgrades are deliberate: bump, then run the contract test.

**Contract (read from v0.5.0 source, verified live 2026-10-13):**

| Endpoint | Returns | Notes |
|---|---|---|
| `GET /health` | `{data: {status: "ok"}}` | Does not contact PSX. Used by our dependency health check |
| `GET /stocks/{symbol}/historical?start=&end=` | `[{date, open, high, low, close, volume, is_anomaly}]`, **newest first** | Daily OHLCV, **unadjusted**. Works for indices (KSE100). About 10 years available (SYS: 2,466 bars from 2016-10-13). Full history is cached and sliced: 30 min in market hours, otherwise until the next 09:00 PKT |
| `GET /stocks/{symbol}/quote` | `{symbol, price, change, change_pct, ldcp, pe_ratio, dividend_yield, market_cap, volume_avg_30d, change_1y_pct, free_float}` | **No timestamp.** `change_pct` is in percent. `change`, `ldcp` and `market_cap` were null in live responses. **404 for indices** |
| `GET /indices` | list of index names (18 live, incl. KSE100, KSE30, KMI30) | Used to classify symbols as index or stock |
| `GET /stocks/{symbol}/fundamentals` | financial report links (`year, type, period_ended, posting_date, document`) | Not ingested yet (Phase 4) |
| `GET /indices/{name}` | constituents with weights | Not ingested yet |

Envelope: `{data, meta: {timestamp, cached[, count]}}`. Errors: `{error: {status, code, message}}`
with 429 `rate_limited` (60 requests/min per client), 502 `upstream_data_error` (PSX data failed
psxdata's validation) or `proxy_unreachable`, 503 `psx_unavailable`, 404 `not_found`, 400/422.

**What we store:** `daily_bars` (unadjusted OHLCV keyed by symbol and session date, with
revisions and quality flags), `quote_snapshots`, and `instruments`. On a 502/503 the dashboard
keeps showing the last stored data, labelled.

**Live observations (2026-10-13):** the first full backfill stored 12,364 bars for SYS, FFC, MEBL,
MARI and KSE100. 24 bars carry quality flags. One record was rejected: PSX lists KSE100
**2024-01-22 twice**, so the first copy was kept and the duplicate quarantined. The KSE100 bar for
2026-10-12 has close 165,124.41 **below** its low of 165,168.50 (`is_anomaly: true`), so it is
stored with flags `provider_anomaly, close_outside_range`.

**Known gaps:** no corporate-action data and no adjusted prices, so returns across splits or
bonus issues are wrong until adjustments exist (Phase 5 blocker). No trading calendar endpoint.
Quote freshness is uncertain because quotes carry no timestamp.

### Capital Stake REST API (removed)
Built on 2026-10-13, never run against live data (no token), and **removed at the owner's
request** in favour of psxdata-api. Its documented endpoints (`/3.0/market/eod`,
`/eod-adj`, `/holidays`, …) remain a licensed alternative if one is needed.

### PSX Data Portal (dps.psx.com.pk), including `/historical` and `/announcements`
- **Provides:** quotes, index values, historical EOD per symbol (month/year search) and per date,
  plus a "Daily Downloads" page with the daily market summary.
- **Terms (read 2026-10-13):** content is for "information and/or educational purposes only".
  PSX does not guarantee timeliness or accuracy. Use of "spiders, robots, avatars, agents, tools or
  other devices" to navigate or search the site needs prior written permission. Only a single
  personal, non-commercial copy is allowed. Distributing or commercially using the data needs a
  licence from marketdatarequest@psx.com.pk.
- **Original decision:** do not scrape the DPS portal (ADR-004). **Superseded 2026-10-13** by the
  owner's decision to use psxdata-api, which reads DPS (ADR-012).
- **Path forward:** request written permission or a licence from PSX Market Data, or use an
  authorised redistributor (see below).

### PSX data vending / licensing (psx.com.pk data-services-vending)
- **Products:** Real-time L1 / L1+ / L2, **End-of-Day** (OHLC and volume per security),
  **Historical** (all past sessions; explicitly marketed for backtesting), and **Corporate
  Announcements (PUCARS)**.
- **Licensing:** commercial use, internal use or redistribution needs PSX approval. Third parties
  receiving redistributed data need their own licence. Fees are not published.
- **Authorised local redistributors listed:** includes Capital Stake, Mettis Global and Sarmaaya
  Financials (redistribution requires PSX's prior approval).
- **Contact:** marketdatarequest@psx.com.pk.
- **Decision:** the **target production source** for prices, indices and corporate
  announcements is a licensed PSX EOD + Historical (+ PUCARS) feed. Field definitions, delivery
  format, timestamps and adjustment policy are **unknown until the licence documentation is
  received**. That is the main external blocker.

### Manual file import (`manual_file_import`)
- An operator places CSV files in `MARKET_DATA_IMPORT_DIR` (default `./data/imports`, gitignored).
- Every import records the file name, SHA-256, row counts and the operator's declared origin
  (`--origin`, e.g. "licensed vendor X, delivery 2026-10-12"). Rows keep that provenance.
- The application **does not** check whether the operator was entitled to the file. That is
  the operator's responsibility and is recorded in the `data_sources.license_status` field.
- Lets the pipeline, validation and analytics be built and tested before a licensed feed
  exists, without the application itself doing unauthorised collection.

### PSX financial reports (financials.psx.com.pk)
- Lists companies with year tabs (2022–2026). The fetched page shows no terms, API or download
  description beyond a copyright footer.
- **Unresolved:** whether automated retrieval of the report PDFs is permitted. Until confirmed,
  Phase 4 document ingestion works only on PDFs the operator supplies (same provenance model as
  file import).

### SECP (secp.gov.pk)
- Regulator. Possible source of statutory filings and company registry data. **Not yet
  evaluated**; Phase 4.

### State Bank of Pakistan (sbp.org.pk)
- Publishes the policy rate, exchange rates (including daily mark-to-market revaluation rates)
  and monetary statistics. The EasyData portal (easydata.sbp.org.pk) returned **HTTP 403** to
  our fetch, so its API, registration and terms are **unverified**.
- Third-party mirrors of SBP rates exist (e.g. frankfurter.dev). They are **not** treated as
  authoritative. They may only be used, if ever, after an ADR.
- **Decision pending (Phase 3):** manual entry from SBP monetary policy statements (low
  frequency: a handful of decisions a year) vs. an EasyData API key.

### Pakistan Bureau of Statistics (pbs.gov.pk) and Ministry of Finance (finance.gov.pk)
- Monthly CPI releases, Economic Survey and fiscal data. Publication is periodic, so
  `published_at` must be the **release date**, not the reference month (a March CPI print is
  published in early April). Access method: manual capture of release values with a document
  reference. **Not yet evaluated in detail.**

### News sources (Business Recorder, Mettis Global, Dawn Business, The News)
- Each requires a content licence or explicit permission for automated ingestion and
  retention. **None obtained.** Mettis Global is also a PSX-authorised data redistributor and may
  be a single counterparty for both news and EOD data. **Worth asking about.**
- Until a feed exists, Module 3 runs on PSX announcements (once licensed) and operator-supplied
  items only.

## Source registry

`config/sources.yaml` is the declarative registry (added in Phase 2). It is synced into the
`data_sources` table, which also holds runtime state (`last_successful_sync`,
`last_attempted_sync`, `last_error`, `data_quality_status`). A source with
`license_status != "licensed"` or `enabled: false` is never polled by the scheduler.

## Fixtures and demonstration data

Test fixtures live in `tests/fixtures/` and use the reserved symbol prefix `TEST_` with a
`source_id` of `test_fixture`. They are synthetic, are never loaded into the development or
production database by any application command, and must not be presented as market data.
