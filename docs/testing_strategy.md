# Testing strategy

## Layout

| Directory | Scope | Needs |
|---|---|---|
| `tests/unit` | Pure functions: config, logging, indicators, metrics, calendars | nothing |
| `tests/integration` | Services with a real PostgreSQL: jobs, ingestion, repositories, migrations | `TEST_DATABASE_URL` |
| `tests/api` | Flask test client against the full app | `TEST_DATABASE_URL` |
| `tests/data_quality` | Validation rules on fixture datasets | varies |
| `tests/forecasting` | Target construction, leakage, walk-forward, reproducibility | varies |
| `tests/fixtures` | Deterministic, clearly synthetic data (`TEST_` symbols) | |

## Rules

- DB tests use **PostgreSQL** (JSONB, enums and partial indexes behave differently in SQLite).
  The session fixture drops the schema and runs the **real Alembic migrations**. Each test ends
  with `TRUNCATE … RESTART IDENTITY CASCADE`.
- Tests marked `db` skip with an explicit reason when `TEST_DATABASE_URL` is unset. Unit tests
  always run.
- Celery runs **eagerly** under test (`CELERY_TASK_ALWAYS_EAGER`), so job tests execute the real
  task wrapper synchronously. Broker-dependent behaviour (dispatch failure) is simulated by
  monkeypatching `apply_async`.
- Live dependency probes (worker ping, scheduler heartbeat) are stubbed in API tests so results
  do not depend on whether a worker happens to be running.
- `external` marker: tests needing credentials or network feeds. Never run by default.
- No test may call a real external data source.

## Running

```bash
.venv/bin/pytest -q                      # everything available
.venv/bin/pytest -q tests/unit           # no database needed
.venv/bin/pytest -q -m "not external"    # explicit
.venv/bin/ruff check .
```

## Phase-specific test plans

- **Phase 2:** duplicate rows, re-import of the same file (idempotency), OHLC inconsistency,
  negative/zero prices, missing sessions vs calendar, symbol not on watchlist, revision handling.
- **Phase 3:** indicators against hand-computed examples (SMA, EMA, RSI with Wilder smoothing,
  MACD, ATR, Bollinger), insufficient history → explicit `insufficient_history`, no future rows
  used (append a future spike and assert unchanged output).
- **Phase 5:** target uses calendar sessions, statements published after origin are excluded,
  scaler fit only on train folds, embargo enforced, identical inputs → identical forecasts.
- **Phase 6:** pending forecasts not evaluated before target session, outcomes stored separately,
  candidate failing acceptance criteria is rejected, promotion requires explicit approval.
