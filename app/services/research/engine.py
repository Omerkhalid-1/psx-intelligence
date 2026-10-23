"""Technical analysis engine: indicator readings with plain-language explanations.

For one symbol it computes every indicator (causal, from split/bonus-adjusted stored bars) and turns
each into a *reading*: value, state, a descriptive tone (+1 / 0 / -1), a one-line headline and the
exact rule that produced it. Readings roll up into an "indicator balance" and a short story.

Everything is deterministic and traceable: every sentence is generated from a value that is
returned next to it, and every threshold is listed in PARAMETERS (shown on the website). The tone
describes what recent prices did; it is not a prediction. Walk-forward tests in this project
(experiment tech_walkforward_v1) found that models built on these indicators did not forecast
returns better than a no-change baseline.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.services.research import indicators as ind
from app.services.research.analysis import compute_indicator_frame
from app.services.research.prices import BENCHMARK, PriceFrame

ENGINE_VERSION = "ta_engine_v1"
MIN_HISTORY = 60  # sessions before any reading is produced
SERIES_DEFAULT_LIMIT = 750

GROUPS = {
    "trend": "Trend",
    "momentum": "Momentum",
    "volatility": "Volatility",
    "volume": "Volume",
    "relative": "Relative strength",
    "levels": "Levels & risk",
}

# Descriptive thresholds (conventions, not optimised). Changing one changes ENGINE_VERSION.
SLOPE_FLAT = 0.002          # |10-session change of SMA50| below this = flat
RSI_BAND = (45.0, 55.0)     # neutral momentum band around the 50 midline
RSI_EXTREMES = (30.0, 70.0)
STOCH_EXTREMES = (20.0, 80.0)
ADX_WEAK, ADX_STRONG = 20.0, 25.0
ROC_BAND = 0.02
EXCESS_BAND = 0.02
UPDOWN_VOL_BAND = (0.8, 1.2)
BETA_BAND = (0.8, 1.2)
RELVOL_HIGH, RELVOL_LOW = 2.0, 0.5
BB_NEAR = (0.2, 0.8)
POS52_BAND = (0.2, 0.8)
DRAWDOWN_BANDS = (-0.05, -0.20)
SQUEEZE_PCT = 0.10          # bandwidth at or below its 6-month 10th percentile = squeeze


@dataclass(frozen=True)
class Parameter:
    """Documentation for one indicator, shown on the website's parameter guide."""

    key: str
    name: str
    group: str
    settings: str
    what: str
    formula: str
    how_to_read: str
    rule: str
    caveat: str = ""


PARAMETERS: tuple[Parameter, ...] = (
    Parameter("price_vs_sma200", "Price vs 200-day average", "trend", "SMA 200 sessions",
              "Where today's price sits relative to its average over roughly the last ten months.",
              "close / SMA200 - 1, where SMA200 = mean of the last 200 closes",
              "Above the line is a long-term uptrend posture; below it, a long-term downtrend posture.",
              "above → positive; below → negative",
              "Moving averages lag: they confirm a trend after it has started."),
    Parameter("price_vs_sma20", "Price vs 20-day average", "trend", "SMA 20 sessions",
              "The short-term (about one month) version of the same idea.",
              "close / SMA20 - 1",
              "Flips often; useful for seeing the latest swing, not the big picture.",
              "above → positive; below → negative"),
    Parameter("ma_cross", "50/200-day cross regime", "trend", "SMA 50 vs SMA 200",
              "Whether the medium-term average is above the long-term one. The moment it crosses up is "
              "nicknamed a 'golden cross', crossing down a 'death cross'.",
              "sign(SMA50 - SMA200); sessions since the last sign change",
              "Golden-cross regime = medium-term strength outweighs the long-term average.",
              "SMA50 > SMA200 → positive; below → negative",
              "A slow signal: crosses happen well after prices turn."),
    Parameter("sma50_slope", "Direction of the 50-day average", "trend", "SMA 50, 10-session change",
              "Is the medium-term average itself rising, falling or flat?",
              "SMA50(t) / SMA50(t-10) - 1",
              "Rising slope = the trend is advancing; flat = no clear direction.",
              f"> +{SLOPE_FLAT:.1%} → positive; < -{SLOPE_FLAT:.1%} → negative; otherwise flat (neutral)"),
    Parameter("adx", "ADX trend strength", "trend", "Wilder ADX 14 with +DI / -DI",
              "How strong the trend is, regardless of direction. +DI and -DI show which side is winning.",
              "ADX = Wilder-smoothed 100·|+DI − −DI| / (+DI + −DI); DI = smoothed directional move / ATR",
              "Below 20: no meaningful trend (choppy). Above 25: a strong trend. Direction comes from the DI pair.",
              f"ADX < {ADX_WEAK:.0f} → neutral; otherwise +DI > −DI → positive, else negative",
              "ADX can be high in a strong fall as well as a strong rise."),
    Parameter("swing_structure", "Swing structure", "trend", "Pivots confirmed by 5 sessions each side, last 60",
              "The pattern of recent peaks and troughs.",
              "Compare the last two confirmed swing highs and swing lows",
              "Higher highs and higher lows = an up-staircase; lower highs and lower lows = a down-staircase.",
              "HH+HL → positive; LH+LL → negative; mixed → neutral"),
    Parameter("rsi14", "RSI (14)", "momentum", "Wilder RSI, 14 sessions",
              "Momentum gauge from 0 to 100 comparing the size of recent up-days with recent down-days.",
              "RSI = 100 − 100 / (1 + avg gain / avg loss), Wilder-smoothed over 14 sessions",
              "Above 50, gains have outweighed losses. Above 70 the move is stretched ('overbought'); "
              "below 30, stretched to the downside ('oversold').",
              f"> {RSI_BAND[1]:.0f} → positive; < {RSI_BAND[0]:.0f} → negative; between → neutral",
              "Overbought is not a sell signal: strong uptrends can stay above 70 for weeks."),
    Parameter("macd", "MACD", "momentum", "EMA 12 − EMA 26, signal EMA 9",
              "The gap between a fast and a slow exponential average, compared with its own average (signal).",
              "MACD = EMA12 − EMA26; signal = EMA9(MACD); histogram = MACD − signal",
              "Histogram above zero = short-term momentum is accelerating relative to the trend.",
              "histogram > 0 → positive; < 0 → negative"),
    Parameter("stochastic", "Stochastic oscillator", "momentum", "%K 14, %D 3",
              "Where today's close sits within the last 14 sessions' high-low range (0–100).",
              "%K = 100·(close − lowest low) / (highest high − lowest low); %D = 3-session mean of %K",
              "Near 100 = closing near the top of the recent range; %K crossing above %D = turning up.",
              "%K > %D → positive; %K < %D → negative",
              "Very sensitive; flips frequently in sideways markets."),
    Parameter("roc20", "20-day rate of change", "momentum", "20 sessions",
              "Plain price change over about one month.",
              "close / close 20 sessions ago − 1",
              "The simplest momentum measure.",
              f"> +{ROC_BAND:.0%} → positive; < −{ROC_BAND:.0%} → negative; otherwise neutral"),
    Parameter("atr_pct", "ATR (% of price)", "volatility", "Wilder ATR 14",
              "Average daily trading range, including overnight gaps, as a share of the price.",
              "ATR14 / close; true range = max(high − low, |high − prev close|, |low − prev close|)",
              "Bigger = wider daily swings. Compared with the stock's own last year.",
              "descriptive only (no tone); state = low / normal / high vs its own 1-year terciles"),
    Parameter("vol_regime", "Volatility regime", "volatility", "20-session realised volatility, annualised",
              "How turbulent the last month was compared with the stock's own last three years.",
              "std(daily log returns, 20) × √245",
              "High regime = unusually turbulent for this stock; low = unusually calm.",
              "descriptive only; low / normal / high by terciles of its own 3-year history"),
    Parameter("bollinger", "Bollinger Bands", "volatility", "SMA 20 ± 2 standard deviations",
              "A channel around the 20-day average whose width breathes with volatility.",
              "upper/lower = SMA20 ± 2σ(20); %B = (close − lower)/(upper − lower)",
              "%B near 1 = pressing the upper band; near 0 = the lower band. Very narrow bands ('squeeze') "
              "mark unusually quiet periods.",
              f"%B > {BB_NEAR[1]} → positive; < {BB_NEAR[0]} → negative; otherwise neutral; squeeze = "
              f"bandwidth at/below its 6-month {SQUEEZE_PCT:.0%} percentile",
              "A squeeze says a big move is more likely, not which way."),
    Parameter("rel_volume", "Relative volume", "volume", "today vs previous 20-session average",
              "Is today's trading busier or quieter than usual?",
              "volume / mean(volume of the previous 20 sessions)",
              f"Above {RELVOL_HIGH:.0f}× = unusually busy (news, events); below {RELVOL_LOW}× = quiet.",
              "descriptive only (no tone)"),
    Parameter("obv", "On-balance volume (OBV)", "volume", "20-session change of OBV vs price",
              "Running total that adds volume on up-days and subtracts it on down-days.",
              "OBV(t) = OBV(t−1) ± volume(t) by the sign of the daily change",
              "When OBV and price move together, volume confirms the move; when they disagree, it does not.",
              "both up → positive; both down → negative; disagreement → neutral (divergence)"),
    Parameter("updown_volume", "Up-day vs down-day volume", "volume", "last 20 sessions",
              "Was more shares traded on rising days or on falling days?",
              "sum(volume on up-days) / sum(volume on down-days)",
              "Above 1 = buying days were busier.",
              f"> {UPDOWN_VOL_BAND[1]} → positive; < {UPDOWN_VOL_BAND[0]} → negative; otherwise neutral"),
    Parameter("excess_return", "Return vs KSE-100 (60 days)", "relative", "60 sessions",
              "Did the stock beat or lag the market index over about three months?",
              "stock return(60) − KSE-100 return(60)",
              "Positive = outperformed the market.",
              f"> +{EXCESS_BAND:.0%} → positive; < −{EXCESS_BAND:.0%} → negative; otherwise neutral"),
    Parameter("rs_line", "Relative-strength line", "relative", "stock ÷ KSE-100, vs its 50-session average",
              "The ratio of the stock to the index; rising means the stock is gaining on the market.",
              "RS = close / KSE-100 close; compared with SMA50(RS)",
              "RS above its own average = recent leadership.",
              "RS > SMA50(RS) → positive; below → negative"),
    Parameter("beta", "Beta to KSE-100", "relative", "120 sessions of daily log returns",
              "How strongly the stock tends to move with the market.",
              "cov(stock, index) / var(index)",
              "1 = moves like the market; 1.5 = tends to move 50% more; 0.5 = half as much.",
              f"descriptive only; < {BETA_BAND[0]} defensive, > {BETA_BAND[1]} aggressive"),
    Parameter("drawdown", "Drawdown from peak", "levels", "all stored history",
              "How far the price is below its highest close.",
              "close / running maximum close − 1",
              "−30% means the price would need to rise ~43% to regain its peak.",
              f"descriptive only; > {DRAWDOWN_BANDS[0]:.0%} near peak, < {DRAWDOWN_BANDS[1]:.0%} deep drawdown"),
    Parameter("range_52w", "52-week range position", "levels", "last 245 sessions",
              "Where the price sits between its 1-year low (0%) and high (100%).",
              "(close − 52w low) / (52w high − 52w low)",
              "Near 100% = near yearly highs; near 0% = near yearly lows.",
              f"> {POS52_BAND[1]:.0%} → positive; < {POS52_BAND[0]:.0%} → negative; otherwise neutral"),
    Parameter("zones", "Support & resistance zones", "levels", "swing pivots of the last 250 sessions, ±1 ATR",
              "Price areas where the stock repeatedly turned before.",
              "Cluster confirmed swing highs/lows within 1 ATR; zone = cluster range ± 0.25 ATR",
              "Support below the price, resistance above. More touches = a more established level.",
              "descriptive only; nearest zone on each side is reported",
              "Past turning points, not predicted ones."),
)
PARAMETER_INDEX = {p.key: p for p in PARAMETERS}


@dataclass
class Reading:
    key: str
    name: str
    group: str
    value: float | None
    display: str
    state: str
    tone: int | None  # +1 / 0 / -1 for directional readings; None = descriptive only
    headline: str
    rule: str
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------------
# helpers


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _r(x: Any, nd: int = 4) -> float | None:
    v = _num(x)
    return None if v is None else round(v, nd)


def _pct(x: float | None, nd: int = 1, signed: bool = True) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.{nd}f}%" if signed else f"{x * 100:.{nd}f}%"


def _tone3(value: float | None, lo: float, hi: float) -> int | None:
    if value is None:
        return None
    return 1 if value > hi else -1 if value < lo else 0


def _missing(key: str, why: str = "not enough history") -> Reading:
    p = PARAMETER_INDEX[key]
    return Reading(key, p.name, p.group, None, "n/a", "insufficient_data", None, f"{p.name}: {why}.", p.rule)


# ---------------------------------------------------------------------------------------------------
# frame


def compute_frame(pf: PriceFrame, bench: PriceFrame) -> pd.DataFrame:
    """All indicator series used by the engine and the charts (causal; adjusted prices)."""
    x = compute_indicator_frame(pf, bench)
    # A few provider bars have a high/low that does not contain the open/close (flagged as
    # provider_anomaly at ingestion). Range-based indicators need a consistent bar, so the range is
    # widened to contain the body; stored bars are untouched.
    x["high"] = x[["high", "open", "close"]].max(axis=1)
    x["low"] = x[["low", "open", "close"]].min(axis=1)
    h, lo, c, v = x["high"], x["low"], x["close"], x["volume"]
    x["atr14"] = ind.atr(h, lo, c, 14)
    x = x.join(ind.bollinger(c)).join(ind.stochastic(h, lo, c)).join(ind.adx(h, lo, c))
    x["obv"] = ind.obv(c, v)
    x["roc20"] = ind.roc(c, 20)
    x["high_52w"] = h.rolling(245, min_periods=60).max()
    x["low_52w"] = lo.rolling(245, min_periods=60).min()
    if pf.symbol != bench.symbol:
        bc = bench.frame["adj_close"].reindex(x.index)
        x["rs"] = c / bc
        x["rs_sma50"] = x["rs"].rolling(50, min_periods=50).mean()
        x["bench_close"] = bc
    return x


# ---------------------------------------------------------------------------------------------------
# readings


def _trend(x: pd.DataFrame) -> list[Reading]:
    last, c = x.iloc[-1], float(x["close"].iloc[-1])
    out: list[Reading] = []
    for key, col in (("price_vs_sma200", "sma200"), ("price_vs_sma20", "sma20")):
        p, ma = PARAMETER_INDEX[key], _num(last[col])
        if ma is None:
            out.append(_missing(key))
            continue
        gap = c / ma - 1
        above = gap > 0
        out.append(Reading(key, p.name, "trend", _r(gap), _pct(gap), "above" if above else "below",
                           1 if above else -1,
                           f"Price is {abs(gap):.1%} {'above' if above else 'below'} its "
                           f"{col.replace('sma', '')}-day average ({ma:,.2f}).", p.rule, {"average": _r(ma, 2)}))

    p = PARAMETER_INDEX["ma_cross"]
    diff = (x["sma50"] - x["sma200"]).dropna()
    if diff.empty:
        out.append(_missing("ma_cross"))
    else:
        sign = np.sign(diff)
        changes = np.flatnonzero(sign.to_numpy()[1:] != sign.to_numpy()[:-1])
        since = int(len(sign) - 1 - (changes[-1] + 1)) if len(changes) else None
        golden = bool(diff.iloc[-1] > 0)
        when = f" for {since} sessions" if since is not None else " throughout the stored history"
        out.append(Reading("ma_cross", p.name, "trend", _r(diff.iloc[-1] / x["sma200"].iloc[-1]),
                           "golden" if golden else "death", "golden_cross_regime" if golden else "death_cross_regime",
                           1 if golden else -1,
                           f"The 50-day average has been {'above' if golden else 'below'} the 200-day{when}.",
                           p.rule, {"sessions_since_cross": since}))

    p = PARAMETER_INDEX["sma50_slope"]
    if len(x) > 60 and _num(x["sma50"].iloc[-11]) and _num(last["sma50"]):
        slope = float(last["sma50"] / x["sma50"].iloc[-11] - 1)
        state = "rising" if slope > SLOPE_FLAT else "falling" if slope < -SLOPE_FLAT else "flat"
        out.append(Reading("sma50_slope", p.name, "trend", _r(slope), _pct(slope, 2), state,
                           {"rising": 1, "falling": -1, "flat": 0}[state],
                           f"The 50-day average is {state} ({_pct(slope, 2)} over 10 sessions).", p.rule))
    else:
        out.append(_missing("sma50_slope"))

    p = PARAMETER_INDEX["adx"]
    a, pdi, mdi = _num(last["adx"]), _num(last["plus_di"]), _num(last["minus_di"])
    if a is None or pdi is None or mdi is None:
        out.append(_missing("adx"))
    else:
        strength = "strong_trend" if a >= ADX_STRONG else "no_clear_trend" if a < ADX_WEAK else "developing_trend"
        side = "buyers (+DI)" if pdi > mdi else "sellers (−DI)"
        tone = 0 if a < ADX_WEAK else (1 if pdi > mdi else -1)
        text = (f"ADX {a:.0f}: no clear trend — the market is choppy." if a < ADX_WEAK else
                f"ADX {a:.0f}: a {'strong' if a >= ADX_STRONG else 'developing'} trend, led by {side}.")
        out.append(Reading("adx", p.name, "trend", _r(a, 1), f"{a:.0f}", strength, tone, text, p.rule,
                           {"plus_di": _r(pdi, 1), "minus_di": _r(mdi, 1)}))

    p = PARAMETER_INDEX["swing_structure"]
    s = ind.swing_structure(x["high"], x["low"])
    if s == "insufficient_pivots":
        out.append(_missing("swing_structure", "too few confirmed swing points"))
    else:
        label = {"higher_highs_higher_lows": "higher highs and higher lows",
                 "lower_highs_lower_lows": "lower highs and lower lows", "mixed": "a mixed pattern"}[s]
        short = {"higher_highs_higher_lows": "HH · HL", "lower_highs_lower_lows": "LH · LL", "mixed": "mixed"}[s]
        tone = {"higher_highs_higher_lows": 1, "lower_highs_lower_lows": -1, "mixed": 0}[s]
        out.append(Reading("swing_structure", p.name, "trend", None, short, s, tone,
                           f"Recent swings show {label}.", p.rule))
    return out


def _momentum(x: pd.DataFrame) -> list[Reading]:
    last = x.iloc[-1]
    out: list[Reading] = []
    p = PARAMETER_INDEX["rsi14"]
    r = _num(last["rsi14"])
    if r is None:
        out.append(_missing("rsi14"))
    else:
        zone = "overbought" if r >= RSI_EXTREMES[1] else "oversold" if r <= RSI_EXTREMES[0] else (
            "bullish" if r > RSI_BAND[1] else "bearish" if r < RSI_BAND[0] else "neutral")
        words = {"overbought": "stretched to the upside (overbought zone)",
                 "oversold": "stretched to the downside (oversold zone)",
                 "bullish": "gains have outweighed losses", "bearish": "losses have outweighed gains",
                 "neutral": "gains and losses are roughly balanced"}[zone]
        out.append(Reading("rsi14", p.name, "momentum", _r(r, 1), f"{r:.0f}", zone,
                           _tone3(r, *RSI_BAND), f"RSI {r:.0f}: {words}.", p.rule))

    p = PARAMETER_INDEX["macd"]
    hist = x["hist"].dropna()
    if hist.empty:
        out.append(_missing("macd"))
    else:
        hv = float(hist.iloc[-1])
        rising = len(hist) > 3 and hv > float(hist.iloc[-4])
        above = hv > 0
        state = ("above_signal" if above else "below_signal") + ("_rising" if rising else "_falling")
        out.append(Reading("macd", p.name, "momentum", _r(hv), f"{hv:+.2f}", state, 1 if above else -1,
                           f"MACD is {'above' if above else 'below'} its signal line and the gap is "
                           f"{'widening' if rising == above else 'narrowing'}.", p.rule,
                           {"macd": _r(last["macd"]), "signal": _r(last["signal"])}))

    p = PARAMETER_INDEX["stochastic"]
    k, d = _num(last["stoch_k"]), _num(last["stoch_d"])
    if k is None or d is None:
        out.append(_missing("stochastic"))
    else:
        zone = "overbought" if k >= STOCH_EXTREMES[1] else "oversold" if k <= STOCH_EXTREMES[0] else "mid_range"
        out.append(Reading("stochastic", p.name, "momentum", _r(k, 1), f"{k:.0f}", zone,
                           1 if k > d else -1 if k < d else 0,
                           f"Close is at {k:.0f}% of its 14-session range; %K is {'above' if k > d else 'below'} %D.",
                           p.rule, {"d": _r(d, 1)}))

    p = PARAMETER_INDEX["roc20"]
    roc = _num(last["roc20"])
    if roc is None:
        out.append(_missing("roc20"))
    else:
        out.append(Reading("roc20", p.name, "momentum", _r(roc), _pct(roc), "up" if roc > 0 else "down",
                           _tone3(roc, -ROC_BAND, ROC_BAND), f"Price changed {_pct(roc)} over 20 sessions.", p.rule))
    return out


def _volatility(x: pd.DataFrame) -> list[Reading]:
    last = x.iloc[-1]
    out: list[Reading] = []
    p = PARAMETER_INDEX["atr_pct"]
    atr_pct_series = (x["atr14"] / x["close"]).dropna()
    if atr_pct_series.empty:
        out.append(_missing("atr_pct"))
    else:
        v = float(atr_pct_series.iloc[-1])
        hist = atr_pct_series.iloc[-245:]
        lo_q, hi_q = hist.quantile([1 / 3, 2 / 3])
        state = "low" if v <= lo_q else "high" if v >= hi_q else "normal"
        out.append(Reading("atr_pct", p.name, "volatility", _r(v), _pct(v, 2, signed=False), state, None,
                           f"A typical day spans about {_pct(v, 1, signed=False)} ({float(last['atr14']):,.2f}) — "
                           f"{state} by its own standard over the last year.", p.rule, {"atr": _r(last["atr14"], 4)}))

    p = PARAMETER_INDEX["vol_regime"]
    vol_hist = x["vol20"].iloc[-750:].dropna()
    v20 = _num(last["vol20"])
    if v20 is None or len(vol_hist) < 100:
        out.append(_missing("vol_regime"))
    else:
        lo_q, hi_q = vol_hist.quantile([1 / 3, 2 / 3])
        state = "low" if v20 <= lo_q else "high" if v20 >= hi_q else "normal"
        out.append(Reading("vol_regime", p.name, "volatility", _r(v20), _pct(v20, 0, signed=False), state, None,
                           f"Last month's volatility ({_pct(v20, 0, signed=False)} annualised) is {state} versus "
                           f"its own 3-year history.", p.rule))

    p = PARAMETER_INDEX["bollinger"]
    pb, bw = _num(last["bb_pct_b"]), _num(last["bb_bandwidth"])
    if pb is None or bw is None:
        out.append(_missing("bollinger"))
    else:
        bw_hist = x["bb_bandwidth"].iloc[-126:].dropna()
        squeeze = bool(len(bw_hist) > 60 and bw <= bw_hist.quantile(SQUEEZE_PCT))
        where = ("above the upper band" if pb > 1 else "below the lower band" if pb < 0 else
                 "near the upper band" if pb > BB_NEAR[1] else "near the lower band" if pb < BB_NEAR[0] else
                 "inside the bands")
        state = "squeeze" if squeeze else where.replace(" ", "_")
        out.append(Reading("bollinger", p.name, "volatility", _r(pb, 3), f"{pb * 100:.0f}%B", state,
                           _tone3(pb, *BB_NEAR),
                           f"Price is {where} (%B {pb:.2f})" + ("; bands are unusually narrow (squeeze)." if squeeze
                                                               else "."), p.rule,
                           {"upper": _r(last["bb_upper"], 2), "lower": _r(last["bb_lower"], 2),
                            "bandwidth": _r(bw), "squeeze": squeeze}))
    return out


def _volume(x: pd.DataFrame, is_index: bool) -> list[Reading]:
    last = x.iloc[-1]
    out: list[Reading] = []
    p = PARAMETER_INDEX["rel_volume"]
    rv = _num(last["rel_volume20"])
    if rv is None:
        out.append(_missing("rel_volume"))
    else:
        state = "unusually_high" if rv >= RELVOL_HIGH else "quiet" if rv <= RELVOL_LOW else "normal"
        out.append(Reading("rel_volume", p.name, "volume", _r(rv, 2), f"{rv:.2f}×", state, None,
                           f"Today's volume is {rv:.1f}× its 20-session average"
                           + (" — unusually busy." if state == "unusually_high" else
                              " — a quiet session." if state == "quiet" else "."), p.rule,
                           {"note": "index volume is the aggregate traded volume of constituents"} if is_index else {}))

    p = PARAMETER_INDEX["obv"]
    if len(x) > 21:
        d_obv = float(x["obv"].iloc[-1] - x["obv"].iloc[-21])
        d_px = float(x["close"].iloc[-1] / x["close"].iloc[-21] - 1)
        if d_obv > 0 and d_px > 0:
            state, tone = "confirming_rise", 1
            text = "Volume is confirming the rise (OBV and price both up over 20 sessions)."
        elif d_obv < 0 and d_px < 0:
            state, tone = "confirming_fall", -1
            text = "Volume is confirming the fall (OBV and price both down over 20 sessions)."
        else:
            state, tone = "divergence", 0
            text = (f"Divergence: price {'rose' if d_px > 0 else 'fell'} while OBV "
                    f"{'fell' if d_obv < 0 else 'rose'} over 20 sessions.")
        out.append(Reading("obv", p.name, "volume", _r(d_obv, 0), f"{d_obv:+,.0f}", state, tone, text, p.rule,
                           {"price_change_20": _r(d_px)}))
    else:
        out.append(_missing("obv"))

    p = PARAMETER_INDEX["updown_volume"]
    w = x.iloc[-20:]
    chg = x["close"].diff().iloc[-20:]
    up_v, down_v = float(w["volume"][chg > 0].sum()), float(w["volume"][chg < 0].sum())
    if len(x) < 21 or down_v <= 0:
        out.append(_missing("updown_volume"))
    else:
        ratio = up_v / down_v
        out.append(Reading("updown_volume", p.name, "volume", _r(ratio, 2), f"{ratio:.2f}×",
                           "buying_heavier" if ratio > 1 else "selling_heavier", _tone3(ratio, *UPDOWN_VOL_BAND),
                           f"Over 20 sessions, up-days traded {ratio:.2f}× the volume of down-days.", p.rule))
    return out


def _relative(x: pd.DataFrame) -> list[Reading]:
    last = x.iloc[-1]
    out: list[Reading] = []
    p = PARAMETER_INDEX["excess_return"]
    ex = _num(last.get("excess_ret_60"))
    if ex is None:
        out.append(_missing("excess_return"))
    else:
        out.append(Reading("excess_return", p.name, "relative", _r(ex), _pct(ex),
                           "outperforming" if ex > 0 else "underperforming", _tone3(ex, -EXCESS_BAND, EXCESS_BAND),
                           f"Over 60 sessions the stock {'beat' if ex > 0 else 'lagged'} the KSE-100 by "
                           f"{abs(ex):.1%}.", p.rule))
    p = PARAMETER_INDEX["rs_line"]
    rs, rs_ma = _num(last.get("rs")), _num(last.get("rs_sma50"))
    if rs is None or rs_ma is None:
        out.append(_missing("rs_line"))
    else:
        above = rs > rs_ma
        out.append(Reading("rs_line", p.name, "relative", _r(rs / rs_ma - 1), _pct(rs / rs_ma - 1),
                           "rising_vs_market" if above else "falling_vs_market", 1 if above else -1,
                           f"The relative-strength line is {'above' if above else 'below'} its 50-session average.",
                           p.rule))
    p = PARAMETER_INDEX["beta"]
    b, corr = _num(last.get("beta")), _num(last.get("corr"))
    if b is None:
        out.append(_missing("beta"))
    else:
        state = "defensive" if b < BETA_BAND[0] else "aggressive" if b > BETA_BAND[1] else "market_like"
        out.append(Reading("beta", p.name, "relative", _r(b, 2), f"{b:.2f}", state, None,
                           f"Beta {b:.2f}: a 1% market move has typically come with a {b:.1f}% move here"
                           + (f" (correlation {corr:.2f})." if corr is not None else "."), p.rule,
                           {"correlation": _r(corr, 2)}))
    return out


def _levels(x: pd.DataFrame) -> tuple[list[Reading], dict[str, Any]]:
    last, c = x.iloc[-1], float(x["close"].iloc[-1])
    out: list[Reading] = []
    p = PARAMETER_INDEX["drawdown"]
    dd = _num(last["drawdown"])
    if dd is None:
        out.append(_missing("drawdown"))
    else:
        state = "near_peak" if dd > DRAWDOWN_BANDS[0] else "deep_drawdown" if dd < DRAWDOWN_BANDS[1] else "pullback"
        need = (1 / (1 + dd) - 1) if dd > -1 else None
        out.append(Reading("drawdown", p.name, "levels", _r(dd), _pct(dd), state, None,
                           f"Price is {abs(dd):.1%} below its highest close"
                           + (f"; it would need +{need:.0%} to regain it." if need and dd < -0.005 else "."),
                           p.rule, {"needed_to_recover": _r(need)}))
    p = PARAMETER_INDEX["range_52w"]
    hi, lo = _num(last["high_52w"]), _num(last["low_52w"])
    if hi is None or lo is None or hi <= lo:
        out.append(_missing("range_52w"))
    else:
        pos = (c - lo) / (hi - lo)
        out.append(Reading("range_52w", p.name, "levels", _r(pos, 3), f"{pos * 100:.0f}%",
                           "near_high" if pos > POS52_BAND[1] else "near_low" if pos < POS52_BAND[0] else "mid_range",
                           _tone3(pos, *POS52_BAND),
                           f"Price sits at {pos:.0%} of its 52-week range ({lo:,.2f} – {hi:,.2f}).", p.rule,
                           {"high_52w": _r(hi, 2), "low_52w": _r(lo, 2)}))
    atr_v = _num(last["atr14"])
    zones = ind.support_resistance_zones(x["high"], x["low"], x["close"], atr_v if atr_v else float("nan"))
    p = PARAMETER_INDEX["zones"]
    sup = zones["support"][0] if zones["support"] else None
    res = zones["resistance"][0] if zones["resistance"] else None
    parts = []
    if sup:
        parts.append(f"nearest support {sup['low']:,.2f}–{sup['high']:,.2f} ({sup['distance_pct']:+.1f}%)")
    if res:
        parts.append(f"nearest resistance {res['low']:,.2f}–{res['high']:,.2f} ({res['distance_pct']:+.1f}%)")
    out.append(Reading("zones", p.name, "levels", None, f"{len(zones['support'])}S / {len(zones['resistance'])}R",
                       "found" if parts else "none_found", None,
                       ("Zones: " + "; ".join(parts) + ".") if parts else "No established zones near the price.",
                       p.rule))
    return out, zones


# ---------------------------------------------------------------------------------------------------
# public API


def balance(readings: list[Reading]) -> dict[str, Any]:
    directional = [r for r in readings if r.tone is not None]
    pos = sum(r.tone == 1 for r in directional)
    neg = sum(r.tone == -1 for r in directional)
    neu = sum(r.tone == 0 for r in directional)
    n = len(directional)
    score = (pos - neg) / n if n else None
    if score is None:
        label = "insufficient_data"
    elif score >= 0.5:
        label = "strongly_positive"
    elif score >= 0.2:
        label = "leaning_positive"
    elif score > -0.2:
        label = "mixed"
    elif score > -0.5:
        label = "leaning_negative"
    else:
        label = "strongly_negative"
    by_group = {}
    for g in GROUPS:
        gr = [r for r in directional if r.group == g]
        if gr:
            by_group[g] = {"positive": sum(r.tone == 1 for r in gr), "neutral": sum(r.tone == 0 for r in gr),
                           "negative": sum(r.tone == -1 for r in gr),
                           "score": round(sum(r.tone for r in gr) / len(gr), 3)}
    return {"positive": pos, "neutral": neu, "negative": neg, "directional": n,
            "score": None if score is None else round(score, 3), "label": label, "by_group": by_group}


def _story(readings: dict[str, Reading], bal: dict[str, Any], symbol: str) -> list[str]:
    lines = []
    labels = {"strongly_positive": "strongly positive", "leaning_positive": "leaning positive", "mixed": "mixed",
              "leaning_negative": "leaning negative", "strongly_negative": "strongly negative"}
    if bal["score"] is not None:
        lines.append(f"{symbol}'s indicators are {labels[bal['label']]}: {bal['positive']} positive, "
                     f"{bal['neutral']} neutral and {bal['negative']} negative out of {bal['directional']}.")
    for key in ("price_vs_sma200", "adx", "rsi14", "vol_regime", "obv", "excess_return", "drawdown"):
        r = readings.get(key)
        if r is not None and r.state != "insufficient_data":
            lines.append(r.headline)
    return lines


def evaluate(pf: PriceFrame, bench: PriceFrame, *, frame: pd.DataFrame | None = None) -> dict[str, Any]:
    """Full engine output for the latest stored session of `pf`."""
    if len(pf.frame) < MIN_HISTORY:
        return {"symbol": pf.symbol, "engine_version": ENGINE_VERSION, "status": "insufficient_history",
                "rows": len(pf.frame), "min_rows": MIN_HISTORY}
    x = frame if frame is not None else compute_frame(pf, bench)
    readings = _trend(x) + _momentum(x) + _volatility(x) + _volume(x, pf.is_index)
    if pf.symbol != BENCHMARK and "rs" in x:
        readings += _relative(x)
    lvl, zones = _levels(x)
    readings += lvl
    bal = balance(readings)
    by_key = {r.key: r for r in readings}
    last = x.iloc[-1]
    c = x["close"]
    returns = {f"{n}d": _r(c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n else None for n in (1, 5, 20, 60, 250)}
    return {
        "symbol": pf.symbol, "engine_version": ENGINE_VERSION, "status": "ok", "is_index": pf.is_index,
        "session_date": x.index[-1].date().isoformat(), "rows": len(x), "data_version": pf.data_version,
        "price": {"close": _r(last["close"], 2), "raw_close": _r(last["raw_close"], 2),
                  "change_1d": _r(c.iloc[-1] - c.iloc[-2], 2) if len(c) > 1 else None, "returns": returns,
                  "basis": "split/bonus-adjusted closes (cash dividends not adjusted)"},
        "readings": [asdict(r) for r in readings],
        "balance": bal,
        "story": _story(by_key, bal, pf.symbol),
        "zones": zones,
        "caveats": [
            "Readings describe what prices and volume did; they are not predictions or recommendations.",
            "In this project's walk-forward tests, models using these indicators did not forecast returns better "
            "than assuming no change.",
            "Thresholds are fixed conventions listed in the parameter guide, not optimised trading rules.",
        ],
    }


def series(x: pd.DataFrame, limit: int = SERIES_DEFAULT_LIMIT) -> dict[str, Any]:
    """Chart-ready columns (last `limit` sessions). NaN -> None; times as YYYY-MM-DD."""
    w = x.iloc[-limit:]
    cols = ["open", "high", "low", "close", "volume", "sma20", "sma50", "sma200", "bb_upper", "bb_mid", "bb_lower",
            "rsi14", "macd", "signal", "hist", "stoch_k", "stoch_d", "adx", "plus_di", "minus_di", "obv", "atr14",
            "drawdown", "vol20", "rs", "rs_sma50", "bench_close"]
    out: dict[str, Any] = {"time": [d.date().isoformat() for d in w.index]}
    for col in cols:
        if col in w:
            out[col] = [_r(v, 6) for v in w[col].to_numpy()]
    return out


def parameter_guide() -> list[dict[str, Any]]:
    return [asdict(p) | {"group_label": GROUPS[p.group]} for p in PARAMETERS]
