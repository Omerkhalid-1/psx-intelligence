"""Indicator correctness against reference values, edge cases and causality (no look-ahead)."""

import numpy as np
import pandas as pd
import pytest

from app.services.research import indicators as ind

# Wilder RSI reference example (StockCharts "RSI" ChartSchool table).
RSI_CLOSES = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931,
              46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222]
RSI_EXPECTED = {14: 70.53, 15: 66.32, 16: 66.55, 17: 69.41, 18: 66.36}


def test_rsi_matches_wilder_reference():
    r = ind.rsi(pd.Series(RSI_CLOSES), 14)
    assert r.iloc[:14].isna().all()
    for i, expected in RSI_EXPECTED.items():
        assert r.iloc[i] == pytest.approx(expected, abs=0.05), i


def test_rsi_edge_cases():
    assert ind.rsi(pd.Series(np.arange(1.0, 31.0)), 14).iloc[-1] == 100.0  # only gains
    assert ind.rsi(pd.Series([5.0] * 30), 14).iloc[-1] == 50.0  # no movement
    assert ind.rsi(pd.Series(np.arange(30.0, 0.0, -1)), 14).iloc[-1] == pytest.approx(0.0)


def test_sma_and_ema():
    s = pd.Series([1.0, 2, 3, 4, 5, 6])
    assert ind.sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0, 5.0]
    assert ind.sma(s, 3).iloc[:2].isna().all()
    e = ind.ema(pd.Series([10.0] * 20), 5)
    assert e.iloc[4:].tolist() == pytest.approx([10.0] * 16)
    # EMA recursion: e_t = a*x_t + (1-a)*e_{t-1}, a = 2/(n+1), seeded with the first value.
    x = pd.Series([1.0, 2.0, 3.0, 4.0])
    a = 2 / 4
    manual = [1.0]
    for v in x.iloc[1:]:
        manual.append(a * v + (1 - a) * manual[-1])
    assert ind.ema(x, 3).iloc[2:].tolist() == pytest.approx(manual[2:])


def test_macd_identities():
    c = pd.Series(np.linspace(100, 130, 80))
    m = ind.macd(c)
    np.testing.assert_allclose(m["hist"], m["macd"] - m["signal"], equal_nan=True)
    assert m["macd"].iloc[-1] > 0  # rising prices: fast EMA above slow EMA


def test_atr_constant_range():
    n = 30
    close = pd.Series([100.0] * n)
    high, low = close + 2, close - 2
    assert ind.atr(high, low, close, 14).iloc[-1] == pytest.approx(4.0)
    assert ind.atr(high, low, close, 14).iloc[:14].isna().all()


def test_true_range_uses_previous_close_gap():
    high, low, close = pd.Series([10.0, 12.0]), pd.Series([9.0, 11.5]), pd.Series([9.5, 12.0])
    assert ind.true_range(high, low, close).iloc[1] == pytest.approx(2.5)  # gap up: high - prev close


def test_drawdown_and_max_drawdown():
    c = pd.Series([100.0, 120, 90, 110, 130])
    assert ind.drawdown(c).tolist() == pytest.approx([0, 0, -0.25, 110 / 120 - 1, 0])
    assert ind.max_drawdown(c) == pytest.approx(-0.25)


def test_realized_vol_annualisation():
    rng = np.random.default_rng(0)
    c = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 5000))))
    assert ind.realized_vol(c, 4000).iloc[-1] == pytest.approx(0.01 * np.sqrt(245), rel=0.05)


def test_relative_volume_excludes_today():
    v = pd.Series([100.0] * 25 + [1000.0])
    assert ind.relative_volume(v, 20).iloc[-1] == pytest.approx(10.0)


def test_rolling_beta_of_asset_equal_to_market_is_one():
    rng = np.random.default_rng(1)
    m = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300))))
    out = ind.rolling_beta_corr(m, m, 120)
    assert out["beta"].iloc[-1] == pytest.approx(1.0)
    assert out["corr"].iloc[-1] == pytest.approx(1.0)


@pytest.mark.parametrize("fn", [
    lambda c, h, lo, v: ind.sma(c, 20), lambda c, h, lo, v: ind.ema(c, 20), lambda c, h, lo, v: ind.rsi(c, 14),
    lambda c, h, lo, v: ind.macd(c)["hist"], lambda c, h, lo, v: ind.atr(h, lo, c, 14),
    lambda c, h, lo, v: ind.realized_vol(c, 20), lambda c, h, lo, v: ind.donchian_position(h, lo, c, 20),
    lambda c, h, lo, v: ind.relative_volume(v, 20), lambda c, h, lo, v: ind.drawdown(c, 252),
])
def test_indicators_are_causal(fn):
    """Appending future observations must not change any earlier value."""
    rng = np.random.default_rng(2)
    n = 300
    c = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, n + 50))))
    h, lo = c * 1.01, c * 0.99
    v = pd.Series(rng.integers(1000, 5000, n + 50).astype(float))
    past = fn(c.iloc[:n], h.iloc[:n], lo.iloc[:n], v.iloc[:n])
    full = fn(c, h, lo, v).iloc[:n]
    pd.testing.assert_series_equal(past, full, check_names=False)


def test_swing_structure_and_zones():
    t = np.arange(120)
    up = pd.Series(100 + t * 0.5 + 3 * np.sin(t / 3))
    assert ind.swing_structure(up + 1, up - 1) == "higher_highs_higher_lows"
    down = pd.Series(200 - t * 0.5 + 3 * np.sin(t / 3))
    assert ind.swing_structure(down + 1, down - 1) == "lower_highs_lower_lows"
    zones = ind.support_resistance_zones(up + 1, up - 1, up, atr_value=2.0)
    assert all(z["high"] < up.iloc[-1] for z in zones["support"])
    assert ind.support_resistance_zones(up, up, up, atr_value=float("nan")) == {"support": [], "resistance": []}
