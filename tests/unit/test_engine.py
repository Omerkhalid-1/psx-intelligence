"""Analysis engine: new indicators (hand-computed and property checks), readings, balance, story,
series and the runtime self-checks. All price data here is SYNTHETIC."""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services.research import engine, engine_checks
from app.services.research import indicators as ind
from app.services.research.prices import price_frame_from_raw
from tests.factories import business_days, synthetic_ohlcv


def raw(n=400, seed=1, drift=0.0003, vol=0.015, start=date(2023, 1, 2), price=100.0) -> pd.DataFrame:
    df = synthetic_ohlcv(n, seed, start_price=price, drift=drift, vol=vol)
    df.index = pd.to_datetime(business_days(start, n))
    df["quality_flags"] = [[] for _ in range(n)]
    df["revision"] = 1
    return df.astype({"volume": float})


def frames(drift=0.0003, n=400, vol=0.015, stock_raw=None):
    bench = price_frame_from_raw("KSE100", raw(n, seed=9, price=1000.0), [], is_index=True)
    sraw = stock_raw if stock_raw is not None else raw(n, seed=3, drift=drift, vol=vol)
    stock = price_frame_from_raw("SYS", sraw, [], is_index=False, benchmark=bench.frame)
    return stock, bench


# ---- new indicators ---------------------------------------------------------------------------------


def test_bollinger_identities():
    c = pd.Series(100 + np.sin(np.arange(80) / 4) * 5)
    bb = ind.bollinger(c, 20, 2.0)
    np.testing.assert_allclose(bb["bb_mid"], ind.sma(c, 20), equal_nan=True)
    width = bb["bb_upper"] - bb["bb_lower"]
    np.testing.assert_allclose(width, 4 * c.rolling(20).std(ddof=0), equal_nan=True)
    at_upper = (bb["bb_upper"] - bb["bb_lower"]) * 1.0 + bb["bb_lower"]
    np.testing.assert_allclose(((at_upper - bb["bb_lower"]) / width).dropna(), 1.0)
    flat = ind.bollinger(pd.Series([50.0] * 30), 20)
    assert flat["bb_pct_b"].iloc[-1] != flat["bb_pct_b"].iloc[-1]  # zero width: %B undefined (NaN), not inf


def test_stochastic_extremes_and_flat():
    up = pd.Series(np.arange(1.0, 41.0))
    st = ind.stochastic(up + 0.5, up - 0.5, up, 14, 3)
    # window low 26.5, high 40.5, close 40: (40 - 26.5) / (40.5 - 26.5)
    assert st["stoch_k"].iloc[-1] == pytest.approx(100 * 13.5 / 14)
    rising = ind.stochastic(up, up - 1, up, 14, 3)
    assert rising["stoch_k"].iloc[-1] == pytest.approx(100.0)
    falling = ind.stochastic(up[::-1].reset_index(drop=True) + 1, up[::-1].reset_index(drop=True),
                             up[::-1].reset_index(drop=True), 14, 3)
    assert falling["stoch_k"].iloc[-1] == pytest.approx(0.0)
    flat = ind.stochastic(pd.Series([5.0] * 20), pd.Series([5.0] * 20), pd.Series([5.0] * 20), 14, 3)
    assert flat["stoch_k"].iloc[-1] == 50.0
    assert st["stoch_k"].iloc[:13].isna().all()


def test_adx_strength_and_direction():
    t = np.arange(300.0)
    up = pd.Series(100 + t)
    a_up = ind.adx(up + 1, up - 1, up, 14)
    assert a_up["adx"].iloc[-1] > 40 and a_up["plus_di"].iloc[-1] > a_up["minus_di"].iloc[-1]
    down = pd.Series(400 - t)
    a_dn = ind.adx(down + 1, down - 1, down, 14)
    assert a_dn["minus_di"].iloc[-1] > a_dn["plus_di"].iloc[-1]
    rng = np.random.default_rng(0)
    walk = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    a_rw = ind.adx(walk + 1, walk - 1, walk, 14)
    assert a_rw["adx"].iloc[-1] < a_up["adx"].iloc[-1]
    for col in ("adx", "plus_di", "minus_di"):
        v = a_rw[col].dropna()
        assert ((v >= 0) & (v <= 100)).all()


def test_obv_hand_example():
    out = ind.obv(pd.Series([10, 11, 10.5, 10.5, 12]), pd.Series([100, 200, 300, 400, 500.0]))
    assert out.tolist() == [0, 200, -100, -100, 400]


@pytest.mark.parametrize("fn", [
    lambda c, h, lo, v: ind.bollinger(c)["bb_pct_b"], lambda c, h, lo, v: ind.stochastic(h, lo, c)["stoch_k"],
    lambda c, h, lo, v: ind.adx(h, lo, c)["adx"], lambda c, h, lo, v: ind.obv(c, v),
])
def test_new_indicators_are_causal(fn):
    rng = np.random.default_rng(5)
    n = 300
    c = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, n + 40))))
    h, lo = c * 1.01, c * 0.99
    v = pd.Series(rng.integers(1000, 5000, n + 40).astype(float))
    pd.testing.assert_series_equal(fn(c.iloc[:n], h.iloc[:n], lo.iloc[:n], v.iloc[:n]), fn(c, h, lo, v).iloc[:n],
                                   check_names=False)


# ---- engine -----------------------------------------------------------------------------------------


def test_uptrend_reads_positive_and_downtrend_negative():
    up = engine.evaluate(*frames(drift=0.004, vol=0.008))
    down = engine.evaluate(*frames(drift=-0.004, vol=0.008))
    assert up["status"] == down["status"] == "ok"
    assert up["balance"]["score"] > 0.5 and up["balance"]["label"] == "strongly_positive"
    assert down["balance"]["score"] < -0.5 and down["balance"]["label"] == "strongly_negative"
    trend_up = {r["key"]: r["tone"] for r in up["readings"] if r["group"] == "trend"}
    assert trend_up["price_vs_sma200"] == 1 and trend_up["ma_cross"] == 1 and trend_up["sma50_slope"] == 1


def test_every_reading_is_documented_and_explained():
    out = engine.evaluate(*frames())
    keys = {r["key"] for r in out["readings"]}
    assert keys <= set(engine.PARAMETER_INDEX)
    assert {"rsi14", "macd", "adx", "bollinger", "obv", "excess_return", "zones"} <= keys
    for r in out["readings"]:
        assert r["headline"] and r["rule"] and r["group"] in engine.GROUPS
        assert r["tone"] in (None, -1, 0, 1)
    bal = out["balance"]
    assert bal["positive"] + bal["neutral"] + bal["negative"] == bal["directional"]
    assert out["story"][0].startswith("SYS's indicators are")
    json.dumps(out)  # serialisable (no NaN / numpy types)
    assert "NaN" not in json.dumps(out)


def test_index_has_no_relative_readings_and_short_history_is_reported():
    stock, bench = frames()
    idx = engine.evaluate(bench, bench)
    assert all(r["group"] != "relative" for r in idx["readings"])
    short = engine.evaluate(*frames(n=40))
    assert short["status"] == "insufficient_history" and short["min_rows"] == engine.MIN_HISTORY


def test_engine_is_deterministic_and_series_aligned():
    stock, bench = frames()
    assert json.dumps(engine.evaluate(stock, bench)) == json.dumps(engine.evaluate(stock, bench))
    s = engine.series(engine.compute_frame(stock, bench), limit=250)
    assert len(s["time"]) == 250
    assert all(len(v) == 250 for v in s.values())
    assert s["time"] == sorted(s["time"])


def test_inconsistent_provider_bar_is_contained():
    sraw = raw(seed=3)
    sraw.iloc[-1, sraw.columns.get_loc("close")] = sraw["high"].iloc[-1] * 1.02  # close above the high
    x = engine.compute_frame(*frames(stock_raw=sraw))
    assert x["high"].iloc[-1] >= x["close"].iloc[-1]
    assert x["stoch_k"].iloc[-1] <= 100.0


def test_parameter_guide_covers_groups():
    guide = engine.parameter_guide()
    assert {g["group"] for g in guide} == set(engine.GROUPS)
    assert all(g["formula"] and g["how_to_read"] and g["rule"] for g in guide)


# ---- self-checks --------------------------------------------------------------------------------------


def test_self_checks_pass_on_clean_data():
    stock, bench = frames()
    report = engine_checks.run_checks({"SYS": stock, "KSE100": bench}, ("SYS", "KSE100"))
    by = {(c["scope"], c["name"]): c["status"] for c in report["checks"]}
    assert by[("engine", "rsi_reference")] == "pass"
    for scope in ("SYS", "KSE100"):
        for name in ("ohlc_consistency", "identities", "bounds", "no_look_ahead", "determinism"):
            assert by[(scope, name)] == "pass", (scope, name)
    assert report["summary"]["fail"] == 0


def test_self_check_catches_look_ahead(monkeypatch):
    real = engine.compute_frame

    def leaky(pf, bench):
        x = real(pf, bench)
        x["sma20"] = x["close"].rolling(21, center=True, min_periods=1).mean()  # uses future closes
        return x

    monkeypatch.setattr(engine, "compute_frame", leaky)
    stock, bench = frames()
    report = engine_checks.run_checks({"SYS": stock, "KSE100": bench}, ("SYS",))
    look = next(c for c in report["checks"] if c["name"] == "no_look_ahead")
    assert look["status"] == "fail" and "sma20" in look["detail"]
    assert report["status"] == "fail"
