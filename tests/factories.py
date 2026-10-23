"""Deterministic SYNTHETIC market data for tests (never real prices; never loaded outside tests)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from app.extensions import db
from app.models.market_data import DailyBar, DataSource, IngestionRun, IngestionStatus, Instrument
from app.utils.time import utcnow

SOURCE = "psxdata_api"


def business_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def synthetic_ohlcv(n: int, seed: int, start_price: float = 100.0, drift: float = 0.0003,
                    vol: float = 0.015) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = close * np.exp(rng.normal(0, vol / 3, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    volume = rng.integers(10_000, 1_000_000, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


def seed_bars(symbols: tuple[str, ...] = ("SYS", "FFC", "MEBL", "MARI", "KSE100"), n: int = 400,
              start: date = date(2024, 1, 1), skip: dict[str, set[int]] | None = None,
              multiply_before: dict[str, tuple[int, float]] | None = None) -> list[date]:
    """Insert synthetic bars for `symbols` on n business days. `skip` drops given row positions for a
    symbol (missing sessions); `multiply_before` = {symbol: (pos, m)} multiplies prices before pos by m
    (simulating an unadjusted bonus/split at pos)."""
    if db.session.get(DataSource, SOURCE) is None:
        db.session.add(DataSource(source_id=SOURCE, source_name="test", dataset_type="daily_bars",
                                  access_method="test", license_status="test", enabled=True))
        db.session.flush()
    run = IngestionRun(source_id=SOURCE, dataset="daily_bars", status=IngestionStatus.SUCCEEDED, started_at=utcnow(),
                       finished_at=utcnow())
    db.session.add(run)
    db.session.flush()
    days = business_days(start, n)
    for i, symbol in enumerate(symbols):
        df = synthetic_ohlcv(n, seed=100 + i, start_price=1000.0 if symbol == "KSE100" else 100.0 + 10 * i)
        if multiply_before and symbol in multiply_before:
            pos, m = multiply_before[symbol]
            df.loc[: pos - 1, ["open", "high", "low", "close"]] *= m
        kind = "index" if symbol == "KSE100" else "stock"
        if db.session.get(Instrument, symbol) is None:
            db.session.add(Instrument(symbol=symbol, instrument_kind=kind, source_id=SOURCE))
        for j, (d, row) in enumerate(zip(days, df.itertuples(), strict=True)):
            if skip and j in skip.get(symbol, set()):
                continue
            db.session.add(DailyBar(source_id=SOURCE, symbol=symbol, session_date=d, open=_d(row.open),
                                    high=_d(row.high), low=_d(row.low), close=_d(row.close), volume=int(row.volume),
                                    quality_flags=[], revision=1, is_current=True, ingestion_run_id=run.id,
                                    retrieved_at=utcnow()))
    db.session.commit()
    return days


def _d(x: float) -> Decimal:
    return Decimal(str(round(float(x), 4)))
