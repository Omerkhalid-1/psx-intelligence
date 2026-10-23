# PSX Intelligence

Research, forecasting and forecast-evaluation platform for companies listed on the Pakistan
Stock Exchange. Flask API + dashboard, Celery workers, PostgreSQL.

> **Status:** foundation and market-data ingestion done. **Daily OHLCV history (about 10 years) and
> live quotes for SYS, FFC, MEBL, MARI and KSE100 load from a self-hosted
> [psxdata-api](https://github.com/mtauha/psxdata-api)** (verified live 2026-10-13). Analysis
> modules and forecasts do not exist yet. psxdata reads the PSX Data Portal, whose terms restrict
> automated use. The owner has accepted that risk; see
> [docs/implementation_plan.md](docs/implementation_plan.md) and
> [docs/data_sources.md](docs/data_sources.md).
>
> Forecasts this system produces are model estimates with uncertainty, not investment advice.

## Quick start (local, Python 3.12)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # edit DATABASE_URL, REDIS_URL, CELERY_*, ADMIN_API_TOKEN
.venv/bin/flask --app wsgi db upgrade
.venv/bin/flask --app wsgi run                                   # API + dashboard on :5000
.venv/bin/celery -A app.tasks.worker:celery worker --loglevel INFO
.venv/bin/celery -A app.tasks.worker:celery beat --loglevel INFO
```

Then open http://localhost:5000/ (overview) and http://localhost:5000/system (health and jobs).

Verify the pipeline end to end:

```bash
curl -s -X POST localhost:5000/api/v1/jobs -H "Authorization: Bearer $ADMIN_API_TOKEN" \
     -H 'Content-Type: application/json' -d '{"job_type":"system.self_check"}'
curl -s localhost:5000/api/v1/jobs/<id>
curl -s localhost:5000/api/v1/health/dependencies
```

## Market data (psxdata-api)

```bash
services/psxdata-api/run.sh &                            # self-hosted service on 127.0.0.1:8100 (pinned v0.5.0)
# .env: PSXDATA_ENABLED=true   (symbols: PSXDATA_SYMBOLS, default SYS,FFC,MEBL,MARI,KSE100)
.venv/bin/flask --app wsgi ingest-psx bars --full --sync # load ~10 years of daily bars
.venv/bin/flask --app wsgi ingest-psx quotes --sync      # latest quotes
```

Then open the Overview page, or call `GET /api/v1/stocks` and `/api/v1/stocks/SYS/history`.
For automatic daily bars (18:00 PKT) and 15-minute quotes, set `ENABLE_SCHEDULED_INGESTION=true`
and run the Celery worker and beat. Details: [docs/operations.md](docs/operations.md).

**Get the latest information now:** use **Refresh latest data** at the top of any dashboard page (admin
token; needs a worker), `POST /api/v1/refresh` (admin), or `.venv/bin/flask --app wsgi refresh` (inline).
It fetches quotes, new bars and news, then recomputes analysis and forecasts if a new session arrived.


## Deploying the website

- **GitHub Pages (static edition)**: `.github/workflows/pages.yml` rebuilds and publishes a daily snapshot
  after each PSX close. Setup takes about five commands; see [docs/deployment.md](docs/deployment.md).
- **Server edition** (live search, refresh, `/ops`): `docker compose up --build`; see the same guide.

## Docker

```bash
cp .env.example .env   # set POSTGRES_PASSWORD, SECRET_KEY, ADMIN_API_TOKEN; APP_ENV=production
docker compose up --build
```

The API is bound to `127.0.0.1:8000` because the dashboard has no user authentication yet.

## Tests

```bash
export TEST_DATABASE_URL=postgresql+psycopg://user@localhost:5432/psx_intelligence_test  # disposable DB
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Without `TEST_DATABASE_URL`, database-backed tests are skipped (reported as such) and unit tests
still run.

## API (implemented so far)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/v1/health` | – | Liveness |
| GET | `/api/v1/health/dependencies` | – | PostgreSQL, Redis, workers, scheduler, stuck jobs. 503 if DB down |
| GET | `/api/v1/jobs` | – | `page`, `page_size` (≤200), `status`, `job_type` |
| GET | `/api/v1/jobs/<id>` | – | |
| POST | `/api/v1/jobs` | Bearer | `{job_type, parameters?, idempotency_key?}` → 202 |
| GET | `/api/v1/ingestion/status` | – | Integration readiness, per-dataset freshness, last runs |
| GET | `/api/v1/ingestion/runs` | – | `page`, `page_size`, `status`, `dataset` |
| GET | `/api/v1/ingestion/runs/<id>` | – | Counts, safe request metadata, quarantined rejections |
| POST | `/api/v1/ingestion/runs` | Bearer | `{dataset: daily_bars\|quotes, full_history?, symbols?}` → 202. 409 if active, 429 within 60 s, 503 if disabled |
| GET | `/api/v1/stocks` | – | Per tracked symbol: kind, latest bar (with day change), latest quote, freshness |
| GET | `/api/v1/stocks/<symbol>/history` | – | Daily bars from PostgreSQL: `start`, `end`, `limit` (≤ 5000, default 260) |
| GET | `/api/v1/market/quotes` | – | Latest stored quote per stock |

Every response uses the envelope `{data, meta, warnings, errors}`. `meta.correlation_id` matches
the `X-Correlation-ID` response header.

## Documentation

[architecture](docs/architecture.md) ·
[decisions](docs/architecture_decisions.md) ·
[data dictionary](docs/data_dictionary.md) ·
[data sources](docs/data_sources.md) ·
[forecasting methodology](docs/forecasting_methodology.md) ·
[security](docs/security.md) ·
[operations](docs/operations.md) ·
[testing](docs/testing_strategy.md) ·
[plan](docs/implementation_plan.md)
