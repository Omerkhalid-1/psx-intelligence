"""Runtime self-checks for the analysis engine, run against the live stored data.

These complement the unit tests: the tests prove the code on synthetic and reference inputs at
build time; these prove the deployed engine, on today's data, still satisfies the same properties.

  reference    Wilder RSI reproduces the published StockCharts example table.
  identities   MACD histogram = MACD - signal; Bollinger middle = SMA20; OBV steps = +-volume.
  bounds       RSI, stochastic and ADX in [0, 100]; ATR and bandwidth non-negative.
  causality    every indicator at past dates is unchanged when the latest sessions are removed
               (no look-ahead).
  determinism  evaluating twice gives identical output.
  data         enough history, OHLC consistency, freshness of the latest session.
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any

import numpy as np
import pandas as pd

from app.services.research import engine
from app.services.research import indicators as ind
from app.services.research.prices import BENCHMARK, PriceFrame
from app.utils.time import psx_local_date, utcnow

# Wilder RSI reference example (StockCharts ChartSchool "RSI" table): closes and RSI(14) values.
RSI_REFERENCE_CLOSES = (44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
                        45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222)
RSI_REFERENCE_VALUES = {14: 70.53, 15: 66.32, 16: 66.55, 17: 69.41, 18: 66.36}
RSI_TOLERANCE = 0.05
CAUSALITY_HOLDOUT = 20  # sessions removed for the look-ahead check
CAUSALITY_COLUMNS = ("sma20", "sma50", "sma200", "rsi14", "macd", "signal", "hist", "atr14", "vol20", "bb_upper",
                     "bb_lower", "bb_pct_b", "stoch_k", "stoch_d", "adx", "plus_di", "minus_di", "obv", "drawdown",
                     "rel_volume20", "high_52w", "low_52w", "rs", "rs_sma50", "beta")
STALE_AFTER_WEEKDAYS = 2


def _check(name: str, scope: str, ok: bool | None, detail: str) -> dict[str, Any]:
    status = "warn" if ok is None else "pass" if ok else "fail"
    return {"name": name, "scope": scope, "status": status, "detail": detail}


def reference_checks() -> list[dict[str, Any]]:
    r = ind.rsi(pd.Series(RSI_REFERENCE_CLOSES), 14)
    worst = max(abs(float(r.iloc[i]) - v) for i, v in RSI_REFERENCE_VALUES.items())
    return [_check("rsi_reference", "engine", worst <= RSI_TOLERANCE,
                   f"Wilder RSI vs published table: worst difference {worst:.3f} (tolerance {RSI_TOLERANCE})")]


def _max_abs(a: pd.Series, b: pd.Series) -> float:
    d = (a - b).abs().dropna()
    return float(d.max()) if len(d) else 0.0


def symbol_checks(pf: PriceFrame, bench: PriceFrame) -> list[dict[str, Any]]:
    s = pf.symbol
    out: list[dict[str, Any]] = []
    n = len(pf.frame)
    out.append(_check("history", s, n >= engine.MIN_HISTORY,
                      f"{n} stored sessions (engine needs {engine.MIN_HISTORY})"))
    if n < engine.MIN_HISTORY:
        return out

    f = pf.frame
    inconsistent = ((f["adj_low"] > f[["adj_open", "adj_close"]].min(axis=1) + 1e-9)
                    | (f["adj_high"] < f[["adj_open", "adj_close"]].max(axis=1) - 1e-9) | (f["adj_close"] <= 0))
    flagged = f["quality_flags"].apply(lambda fl: bool(fl) and "provider_anomaly" in fl)
    unflagged, known = int((inconsistent & ~flagged).sum()), int((inconsistent & flagged).sum())
    out.append(_check("ohlc_consistency", s, False if unflagged else (None if known else True),
                      f"{unflagged} unflagged inconsistent bar(s); {known} known provider anomalies (flagged at "
                      f"ingestion; the engine widens their high/low to contain open/close)"))
    latest = pf.latest_session
    lag = int(np.busday_count(latest, psx_local_date(utcnow()))) if latest else None
    out.append(_check("freshness", s, None if lag is None or lag > STALE_AFTER_WEEKDAYS else True,
                      f"latest session {latest} ({lag} weekday(s) ago; warn after {STALE_AFTER_WEEKDAYS})"))

    x = engine.compute_frame(pf, bench)
    sma20 = ind.sma(x["close"], 20)
    identity = max(_max_abs(x["hist"], x["macd"] - x["signal"]),
                   _max_abs(x["bb_mid"], sma20) / float(x["close"].mean()))
    obv_steps = x["obv"].diff().abs().iloc[1:]
    moved = x["close"].diff().iloc[1:] != 0
    obv_ok = _max_abs(obv_steps[moved], x["volume"].iloc[1:][moved]) < 1e-6
    out.append(_check("identities", s, identity < 1e-9 and obv_ok,
                      f"MACD/Bollinger identity error {identity:.1e}; OBV steps match volume: {obv_ok}"))

    def within(col: str, lo: float, hi: float) -> bool:
        v = x[col].dropna()
        return bool(((v >= lo - 1e-9) & (v <= hi + 1e-9)).all())
    bounds = {c: within(c, 0, 100) for c in ("rsi14", "stoch_k", "stoch_d", "adx", "plus_di", "minus_di")}
    bounds["atr14"] = bool((x["atr14"].dropna() >= 0).all())
    bounds["bb_bandwidth"] = bool((x["bb_bandwidth"].dropna() >= 0).all())
    failed = [k for k, ok in bounds.items() if not ok]
    out.append(_check("bounds", s, not failed, "all indicators within their ranges" if not failed else
                      f"out of range: {', '.join(failed)}"))

    cut = n - CAUSALITY_HOLDOUT
    trunc = dataclasses.replace(pf, frame=pf.frame.iloc[:cut])
    bench_trunc = trunc if pf.symbol == bench.symbol else dataclasses.replace(
        bench, frame=bench.frame.loc[: pf.frame.index[cut - 1]])
    past = engine.compute_frame(trunc, bench_trunc)
    full = x.iloc[:cut]
    worst_col, worst = None, 0.0
    for col in CAUSALITY_COLUMNS:
        if col not in past or col not in full:
            continue
        scale = max(float(full[col].abs().max() or 1.0), 1e-12)
        diff = _max_abs(full[col], past[col]) / scale
        if diff > worst:
            worst_col, worst = col, diff
    out.append(_check("no_look_ahead", s, worst < 1e-9,
                      f"indicators up to {pf.frame.index[cut - 1].date()} unchanged after removing the last "
                      f"{CAUSALITY_HOLDOUT} sessions (worst relative change {worst:.1e}"
                      + (f" in {worst_col})" if worst_col else ")")))

    a, b = engine.evaluate(pf, bench, frame=x), engine.evaluate(pf, bench)
    same = json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    bal = a.get("balance", {})
    counts_ok = bal.get("positive", 0) + bal.get("neutral", 0) + bal.get("negative", 0) == bal.get("directional", 0)
    out.append(_check("determinism", s, same and counts_ok,
                      f"two evaluations identical: {same}; balance counts consistent: {counts_ok}"))
    return out


def run_checks(frames: dict[str, PriceFrame], symbols: tuple[str, ...]) -> dict[str, Any]:
    started = time.perf_counter()
    checks = reference_checks()
    bench = frames.get(BENCHMARK)
    for s in symbols:
        pf = frames.get(s)
        if pf is None or pf.frame.empty or bench is None:
            checks.append(_check("history", s, False, "no stored bars"))
            continue
        try:
            checks.extend(symbol_checks(pf, bench))
        except Exception as exc:  # noqa: BLE001 - a crashing check is itself a failed check
            checks.append(_check("engine_error", s, False, f"{type(exc).__name__}: {str(exc)[:200]}"))
    summary = {k: sum(c["status"] == k for c in checks) for k in ("pass", "warn", "fail")}
    return {"engine_version": engine.ENGINE_VERSION, "ran_at": utcnow().isoformat(),
            "today": psx_local_date(utcnow()).isoformat(),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "status": "fail" if summary["fail"] else "warn" if summary["warn"] else "pass",
            "summary": summary, "checks": checks}
