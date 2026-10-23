"""Technical indicators as pure, causal functions of pandas Series.

Every function uses only observations at or before each row (rolling / ewm windows, shifts of
+1 only), so the value at session t is computable at t's close. Windows with insufficient history
return NaN rather than a partial estimate (min_periods = window).

Conventions:
  * Wilder smoothing (RSI, ATR) uses ewm(alpha=1/n, adjust=False) seeded after n observations.
  * Volatility is the sample standard deviation of daily log returns, annualised with 245 PSX
    sessions per year (approximate observed count).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SESSIONS_PER_YEAR = 245


def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n, min_periods=n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    out = close.ewm(span=n, adjust=False, min_periods=n).mean()
    return out


def _wilder(series: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing: first value = simple mean of the first n, then recursive."""
    values = series.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = np.where(~np.isnan(values))[0]
    if len(valid) < n:
        return pd.Series(out, index=series.index)
    start = valid[0]
    if np.isnan(values[start:start + n]).any():
        return pd.Series(out, index=series.index)
    out[start + n - 1] = values[start:start + n].mean()
    for i in range(start + n, len(values)):
        v = values[i]
        out[i] = out[i - 1] if np.isnan(v) else out[i - 1] + (v - out[i - 1]) / n
    return pd.Series(out, index=series.index)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI in [0, 100]."""
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), n)
    loss = _wilder((-delta).clip(lower=0), n)
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    out[(loss == 0) & (gain > 0)] = 100.0
    out[(loss == 0) & (gain == 0)] = 50.0
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1, skipna=False)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    tr.iloc[0] = np.nan  # needs a previous close
    return _wilder(tr, n)


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1))


def roc(close: pd.Series, n: int) -> pd.Series:
    """Rate of change (simple return) over n sessions."""
    return close / close.shift(n) - 1


def realized_vol(close: pd.Series, n: int) -> pd.Series:
    return log_returns(close).rolling(n, min_periods=n).std() * np.sqrt(SESSIONS_PER_YEAR)


def drawdown(close: pd.Series, n: int | None = None) -> pd.Series:
    """Close relative to its running (or n-session) peak, minus 1 (<= 0)."""
    peak = close.cummax() if n is None else close.rolling(n, min_periods=1).max()
    return close / peak - 1


def max_drawdown(close: pd.Series) -> float:
    return float(drawdown(close).min()) if len(close) else float("nan")


def relative_volume(volume: pd.Series, n: int = 20) -> pd.Series:
    """Volume relative to the previous n sessions' average (excludes today from the baseline)."""
    base = volume.shift(1).rolling(n, min_periods=n).mean()
    return volume / base


def donchian_position(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 20) -> pd.Series:
    """Where the close sits in the previous n-session high/low channel (0 = low, 1 = high; can
    exceed [0, 1] on a breakout)."""
    hi = high.shift(1).rolling(n, min_periods=n).max()
    lo = low.shift(1).rolling(n, min_periods=n).min()
    width = (hi - lo).replace(0, np.nan)
    return (close - lo) / width


def rolling_beta_corr(asset: pd.Series, market: pd.Series, n: int = 120) -> pd.DataFrame:
    """Rolling beta and correlation of daily log returns (aligned on common dates)."""
    a, m = log_returns(asset), log_returns(market.reindex(asset.index))
    cov = a.rolling(n, min_periods=n).cov(m)
    var = m.rolling(n, min_periods=n).var()
    corr = a.rolling(n, min_periods=n).corr(m)
    return pd.DataFrame({"beta": cov / var, "corr": corr})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands: SMA(n) +- k population standard deviations (Bollinger's convention, ddof=0).

    pct_b = (close - lower) / (upper - lower): 0 at the lower band, 1 at the upper band.
    bandwidth = (upper - lower) / middle: relative width, low values = a "squeeze".
    """
    mid = close.rolling(n, min_periods=n).mean()
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower).replace(0, np.nan)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": upper, "bb_lower": lower,
                         "bb_pct_b": (close - lower) / width, "bb_bandwidth": width / mid})


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14, smooth: int = 3) -> pd.DataFrame:
    """Fast stochastic oscillator: %K = 100 * (close - lowest low) / (highest high - lowest low)
    over n sessions including today; %D = SMA(smooth) of %K. A flat window gives 50."""
    hh = high.rolling(n, min_periods=n).max()
    ll = low.rolling(n, min_periods=n).min()
    rng = hh - ll
    k = 100 * (close - ll) / rng.replace(0, np.nan)
    k = k.where(~((rng == 0) & hh.notna()), 50.0)
    return pd.DataFrame({"stoch_k": k, "stoch_d": k.rolling(smooth, min_periods=smooth).mean()})


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.DataFrame:
    """Wilder's Average Directional Index with +DI / -DI (all in [0, 100]).

    +DM = up-move if it exceeds the down-move and is positive (else 0); -DM symmetric.
    +DI = 100 * Wilder(+DM) / ATR, DX = 100 * |+DI - -DI| / (+DI + -DI), ADX = Wilder(DX).
    ADX measures trend *strength*, not direction; the DI pair gives direction.
    """
    up, down = high.diff(), -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)
    plus_dm.iloc[0] = minus_dm.iloc[0] = np.nan  # needs a previous bar
    atr_n = atr(high, low, close, n)
    plus_di = 100 * _wilder(plus_dm, n) / atr_n
    minus_di = 100 * _wilder(minus_dm, n) / atr_n
    total = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / total
    return pd.DataFrame({"adx": _wilder(dx, n), "plus_di": plus_di, "minus_di": minus_di})


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-balance volume: cumulative volume added on up closes, subtracted on down closes."""
    direction = np.sign(close.diff()).fillna(0.0)
    return (direction * volume).cumsum()


def swing_structure(high: pd.Series, low: pd.Series, n: int = 5, lookback: int = 60) -> str:
    """Classify the most recent pivots as higher-highs/higher-lows (up), lower-highs/lower-lows
    (down) or mixed, using pivots confirmed by n sessions on each side within `lookback` sessions."""
    h, lo = high.iloc[-lookback:], low.iloc[-lookback:]
    pivots_h = [h.iloc[i] for i in range(n, len(h) - n) if h.iloc[i] == h.iloc[i - n:i + n + 1].max()]
    pivots_l = [lo.iloc[i] for i in range(n, len(lo) - n) if lo.iloc[i] == lo.iloc[i - n:i + n + 1].min()]
    if len(pivots_h) < 2 or len(pivots_l) < 2:
        return "insufficient_pivots"
    hh, hl = pivots_h[-1] > pivots_h[-2], pivots_l[-1] > pivots_l[-2]
    if hh and hl:
        return "higher_highs_higher_lows"
    if not hh and not hl:
        return "lower_highs_lower_lows"
    return "mixed"


def support_resistance_zones(high: pd.Series, low: pd.Series, close: pd.Series, atr_value: float,
                             lookback: int = 250, pivot_n: int = 5, max_zones: int = 3) -> dict[str, list[dict]]:
    """Estimated zones from clustered swing pivots.

    Method: confirmed swing highs/lows (pivot_n sessions each side) within `lookback` sessions are
    clustered greedily with tolerance = 1 x ATR; each cluster's zone is [min, max] of its pivots
    (at least +-0.25 ATR wide). Zones below the last close are support, above are resistance; ranked
    by proximity among multi-touch zones, then single-touch zones. These are descriptive estimates,
    not predicted turning points.
    """
    if not np.isfinite(atr_value) or atr_value <= 0 or len(close) < 2 * pivot_n + 1:
        return {"support": [], "resistance": []}
    h, lo = high.iloc[-lookback:], low.iloc[-lookback:]
    pivots = []
    for i in range(pivot_n, len(h) - pivot_n):
        if h.iloc[i] == h.iloc[i - pivot_n:i + pivot_n + 1].max():
            pivots.append((float(h.iloc[i]), h.index[i]))
        if lo.iloc[i] == lo.iloc[i - pivot_n:i + pivot_n + 1].min():
            pivots.append((float(lo.iloc[i]), lo.index[i]))
    pivots.sort()
    clusters: list[list[tuple[float, pd.Timestamp]]] = []
    for price, ts in pivots:
        if clusters and price - clusters[-1][-1][0] <= atr_value:
            clusters[-1].append((price, ts))
        else:
            clusters.append([(price, ts)])
    last = float(close.iloc[-1])
    zones = []
    for c in clusters:
        prices = [p for p, _ in c]
        lo_z, hi_z = min(prices) - 0.25 * atr_value, max(prices) + 0.25 * atr_value
        zones.append({"low": round(lo_z, 4), "high": round(hi_z, 4), "touches": len(c),
                      "last_touch": _label(max(t for _, t in c)),
                      "distance_pct": round(((lo_z + hi_z) / 2 / last - 1) * 100, 2)})
    support = [z for z in zones if z["high"] < last]
    resistance = [z for z in zones if z["low"] > last]
    # Nearest zones confirmed by at least two pivots first, then single-touch zones by proximity.
    rank = lambda z: (z["touches"] < 2, abs(z["distance_pct"]))  # noqa: E731
    return {"support": sorted(support, key=rank)[:max_zones], "resistance": sorted(resistance, key=rank)[:max_zones]}


def _label(ts: object) -> str:
    return ts.date().isoformat() if hasattr(ts, "date") else str(ts)
