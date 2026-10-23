"""Explainable research summaries computed from stored data (no external calls, no LLM).

Every statement in a summary is generated from a computed value that is included alongside it,
so the text is traceable to numbers. Thresholds below are descriptive conventions documented in
docs/research_methodology.md, not tested trading rules.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.services.research import indicators as ind
from app.services.research.prices import BENCHMARK, PriceFrame

METHOD_VERSION = "analysis_v1"
# Descriptive conventions (documented; not optimised).
TREND_SLOPE_FLAT = 0.002  # |SMA50 10-session slope| below this = flat
RSI_HIGH, RSI_LOW = 70, 30
VOL_REGIME_PCTS = (0.33, 0.67)  # terciles of the symbol's own 3-year vol20 distribution


def _f(x: Any, nd: int = 4) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(v) else round(v, nd)


def compute_indicator_frame(pf: PriceFrame, bench: PriceFrame) -> pd.DataFrame:
    """Indicator series for charts and summaries (adjusted prices)."""
    df = pf.frame
    c, h, lo, v = df["adj_close"], df["adj_high"], df["adj_low"], df["adj_volume"]
    out = pd.DataFrame(index=df.index)
    out["open"], out["high"], out["low"], out["close"] = df["adj_open"], h, lo, c
    out["raw_close"], out["volume"] = df["close"], v
    for n in (20, 50, 200):
        out[f"sma{n}"] = ind.sma(c, n)
    out["ema20"] = ind.ema(c, 20)
    out["rsi14"] = ind.rsi(c, 14)
    out = out.join(ind.macd(c))
    out["atr14"] = ind.atr(h, lo, c, 14)
    out["vol20"], out["vol60"] = ind.realized_vol(c, 20), ind.realized_vol(c, 60)
    out["drawdown"] = ind.drawdown(c)
    out["rel_volume20"] = ind.relative_volume(v, 20)
    out["vol_avg20"] = v.rolling(20, min_periods=20).mean()
    if pf.symbol != bench.symbol:
        bc = bench.frame["adj_close"].reindex(df.index)
        out["rs_ratio"] = (c / bc) / (c / bc).iloc[0]
        out = out.join(ind.rolling_beta_corr(c, bc, 120))
        out["excess_ret_60"] = c.pct_change(60, fill_method=None) - bc.pct_change(60, fill_method=None)
    return out


def summarize(pf: PriceFrame, bench: PriceFrame, ind_frame: pd.DataFrame | None = None,
              quality_caveats: list[str] | None = None) -> dict[str, Any]:
    df = pf.frame
    if len(df) < 60:
        return {"symbol": pf.symbol, "status": "insufficient_history", "rows": len(df)}
    x = ind_frame if ind_frame is not None else compute_indicator_frame(pf, bench)
    last = x.iloc[-1]
    session = x.index[-1].date()
    close = float(last["close"])
    c = x["close"]

    # --- trend
    sma50_slope = _f(x["sma50"].iloc[-1] / x["sma50"].iloc[-11] - 1) if len(x) > 60 else None
    above = {n: (None if pd.isna(last[f"sma{n}"]) else bool(close > last[f"sma{n}"])) for n in (20, 50, 200)}
    structure = ind.swing_structure(x["high"], x["low"])
    if sma50_slope is None:
        trend = "insufficient_history"
    elif abs(sma50_slope) < TREND_SLOPE_FLAT:
        trend = "sideways"
    elif sma50_slope > 0 and above[50]:
        trend = "uptrend"
    elif sma50_slope < 0 and not above[50]:
        trend = "downtrend"
    else:
        trend = "transitional"

    # --- momentum
    rsi_v, hist = _f(last["rsi14"], 2), _f(last["hist"])
    momentum_state = ("overbought_zone" if rsi_v and rsi_v >= RSI_HIGH else
                      "oversold_zone" if rsi_v and rsi_v <= RSI_LOW else "neutral_zone")
    returns = {f"{n}d": _f(c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n else None for n in (1, 5, 20, 60, 120, 250)}
    breakout = None
    if len(x) > 21:
        prior_high, prior_low = x["high"].iloc[-21:-1].max(), x["low"].iloc[-21:-1].min()
        breakout = "above_20d_high" if close > prior_high else "below_20d_low" if close < prior_low else "inside_range"

    # --- volatility & risk
    vol_hist = x["vol20"].iloc[-750:].dropna()
    regime = None
    if len(vol_hist) > 100 and pd.notna(last["vol20"]):
        lo_q, hi_q = vol_hist.quantile(VOL_REGIME_PCTS)
        regime = "low" if last["vol20"] <= lo_q else "high" if last["vol20"] >= hi_q else "normal"
    risk = {"vol20": _f(last["vol20"]), "vol60": _f(last["vol60"]), "atr14": _f(last["atr14"], 4),
            "atr14_pct": _f(last["atr14"] / close), "volatility_regime": regime,
            "drawdown_from_peak": _f(last["drawdown"]), "max_drawdown_1y": _f(ind.max_drawdown(c.iloc[-250:])),
            "downside_vol60": _f(np.log(c / c.shift(1)).iloc[-60:].clip(upper=0).std() * np.sqrt(245))}

    # --- volume
    volume = {"rel_volume20": _f(last.get("rel_volume20"), 2), "avg_volume20": _f(last.get("vol_avg20"), 0),
              "unusual_activity": bool(pd.notna(last.get("rel_volume20")) and last["rel_volume20"] >= 3)}
    if pf.is_index:
        volume["note"] = "index volume is the aggregate traded volume of constituents"

    # --- relative strength
    relative = None
    if pf.symbol != BENCHMARK and "beta" in x:
        relative = {"excess_return_60d": _f(last.get("excess_ret_60")), "beta_120": _f(last.get("beta"), 3),
                    "corr_120": _f(last.get("corr"), 3),
                    "rs_trend_60d": _f(x["rs_ratio"].iloc[-1] / x["rs_ratio"].iloc[-61] - 1) if len(x) > 61 else None}

    zones = ind.support_resistance_zones(x["high"], x["low"], c, float(last["atr14"]) if pd.notna(last["atr14"]) else
                                         float("nan"))

    # --- supporting / contradicting observations (each tied to a value)
    supports, contradicts = [], []
    def note(cond: bool | None, bullish: bool, text: str) -> None:  # noqa: E306
        if cond is not None:
            (supports if cond == bullish else contradicts).append(text)
    if trend in ("uptrend", "downtrend"):
        direction_up = trend == "uptrend"
        note(above[200], direction_up, f"close {'above' if above[200] else 'below'} SMA200")
        if hist is not None:
            note(hist > 0, direction_up, f"MACD histogram {hist:+.4f}")
        r20 = returns["20d"]
        if r20 is not None:
            note(r20 > 0, direction_up, f"20-session return {r20:+.1%}")
        if relative and relative["excess_return_60d"] is not None:
            note(relative["excess_return_60d"] > 0, direction_up,
                 f"60-session excess return vs KSE100 {relative['excess_return_60d']:+.1%}")
        if momentum_state != "neutral_zone":
            contradicts.append(f"RSI {rsi_v} in {momentum_state.replace('_', ' ')} (stretched move)")

    return {
        "symbol": pf.symbol, "session_date": session.isoformat(), "method_version": METHOD_VERSION,
        "status": "ok", "price_basis": "split/bonus-adjusted closes (cash dividends not adjusted)",
        "close": _f(close, 2), "raw_close": _f(last["raw_close"], 2),
        "trend": {"classification": trend, "sma50_slope_10d": sma50_slope, "above_sma": above,
                  "sma20": _f(last["sma20"], 2), "sma50": _f(last["sma50"], 2), "sma200": _f(last["sma200"], 2),
                  "swing_structure": structure},
        "momentum": {"rsi14": rsi_v, "rsi_state": momentum_state, "macd": _f(last["macd"]),
                     "macd_signal": _f(last["signal"]), "macd_hist": hist, "returns": returns, "breakout": breakout},
        "risk": risk, "volume": volume, "relative": relative, "zones": zones,
        "observations": {"supporting_trend": supports, "contradicting_trend": contradicts},
        "caveats": (quality_caveats or []) + [
            "Classifications use fixed descriptive thresholds (docs/research_methodology.md); they are not "
            "validated trading signals.",
            "Support/resistance zones are clusters of past swing points, not predicted turning points."],
    }
