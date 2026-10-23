"""Point-in-time feature construction and forecast targets.

Availability rule: a feature row dated t uses only bars with session_date <= t (all computations
are causal rolling windows), so it is available at t's close and may be used by a forecast whose
origin is t. News features use only articles whose `available_at` is <= t's close (see news
aggregation). Every feature is scale-free (ratios/returns), so the split/bonus adjustment of past
prices -- which rescales all earlier prices by a constant -- cannot leak information.

Targets: forward simple return on the adjusted close, y(t, h) = adj_close[t+h] / adj_close[t] - 1,
where t+h is the h-th later row of the symbol's own bar series. Targets are NaN (excluded) when
the window contains an unexplained discontinuity or the symbol skipped exchange sessions inside
the window (a suspension would otherwise silently lengthen the horizon).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.services.research import indicators as ind
from app.services.research.prices import PriceFrame

FEATURE_SET_TECH = "tech_v1"
FEATURE_SET_NEWS = "tech_news_v1"
WARMUP_SESSIONS = 200  # longest window (SMA200); rows before this are not used


@dataclass(frozen=True)
class FeatureDef:
    name: str
    description: str
    group: str


TECH_FEATURES: list[FeatureDef] = [
    FeatureDef("ret_1", "log return over the last session", "momentum"),
    FeatureDef("ret_5", "log return over the last 5 sessions", "momentum"),
    FeatureDef("ret_20", "log return over the last 20 sessions", "momentum"),
    FeatureDef("ret_60", "log return over the last 60 sessions", "momentum"),
    FeatureDef("dist_sma20", "close / SMA20 - 1", "trend"),
    FeatureDef("dist_sma50", "close / SMA50 - 1", "trend"),
    FeatureDef("dist_sma200", "close / SMA200 - 1", "trend"),
    FeatureDef("sma20_slope", "SMA20 / SMA20 five sessions earlier - 1", "trend"),
    FeatureDef("sma50_slope", "SMA50 / SMA50 ten sessions earlier - 1", "trend"),
    FeatureDef("rsi14", "Wilder RSI(14) / 100", "momentum"),
    FeatureDef("macd_hist", "MACD(12,26,9) histogram / close", "momentum"),
    FeatureDef("donchian20", "position of close in the prior 20-session high/low channel", "momentum"),
    FeatureDef("atr14_pct", "Wilder ATR(14) / close", "volatility"),
    FeatureDef("vol20", "annualised std of daily log returns, 20 sessions", "volatility"),
    FeatureDef("vol_ratio", "vol20 / vol60 (volatility regime change)", "volatility"),
    FeatureDef("drawdown_252", "close / 252-session high - 1", "risk"),
    FeatureDef("rel_volume20", "log(volume / prior 20-session mean volume)", "volume"),
    FeatureDef("excess_ret_20", "20-session log return minus KSE100's (0 for KSE100)", "relative"),
    FeatureDef("beta_120", "120-session beta to KSE100 daily log returns (1 for KSE100)", "relative"),
    FeatureDef("mkt_ret_20", "KSE100 20-session log return", "market"),
    FeatureDef("mkt_vol20", "KSE100 20-session annualised volatility", "market"),
    FeatureDef("mkt_dist_sma200", "KSE100 close / SMA200 - 1 (market regime)", "market"),
]
NEWS_FEATURES: list[FeatureDef] = [
    FeatureDef("sent_score_7d", "deduplicated mean headline sentiment over 7 days available by the close "
                                "(0 when insufficient)", "news"),
    FeatureDef("sent_count_7d", "log(1 + distinct relevant news events in 7 days)", "news"),
    FeatureDef("sent_insufficient", "1 when fewer than 1 relevant event in 7 days", "news"),
    FeatureDef("mkt_sent_score_7d", "market-wide (MARKET topic) 7-day sentiment (0 when insufficient)", "news"),
]
FEATURE_SETS: dict[str, list[str]] = {
    FEATURE_SET_TECH: [f.name for f in TECH_FEATURES],
    FEATURE_SET_NEWS: [f.name for f in TECH_FEATURES + NEWS_FEATURES],
}


def build_features(pf: PriceFrame, bench: PriceFrame, sentiment: pd.DataFrame | None = None,
                   market_sentiment: pd.DataFrame | None = None) -> pd.DataFrame:
    """Feature matrix indexed by session date for one symbol (all tech features, plus news
    features when sentiment frames are given). Also returns helper columns: close (raw),
    adj_close, usable (warm-up done and no unexplained discontinuity nearby)."""
    df = pf.frame
    c, h, lo, v = df["adj_close"], df["adj_high"], df["adj_low"], df["adj_volume"]
    lr = ind.log_returns(c)
    out = pd.DataFrame(index=df.index)
    for n in (1, 5, 20, 60):
        out[f"ret_{n}"] = lr.rolling(n, min_periods=n).sum()
    sma20, sma50, sma200 = ind.sma(c, 20), ind.sma(c, 50), ind.sma(c, 200)
    out["dist_sma20"], out["dist_sma50"], out["dist_sma200"] = c / sma20 - 1, c / sma50 - 1, c / sma200 - 1
    out["sma20_slope"], out["sma50_slope"] = sma20 / sma20.shift(5) - 1, sma50 / sma50.shift(10) - 1
    out["rsi14"] = ind.rsi(c, 14) / 100
    out["macd_hist"] = ind.macd(c)["hist"] / c
    out["donchian20"] = ind.donchian_position(h, lo, c, 20)
    out["atr14_pct"] = ind.atr(h, lo, c, 14) / c
    vol20, vol60 = ind.realized_vol(c, 20), ind.realized_vol(c, 60)
    out["vol20"], out["vol_ratio"] = vol20, vol20 / vol60
    out["drawdown_252"] = ind.drawdown(c, 252)
    out["rel_volume20"] = np.log(ind.relative_volume(v, 20).replace(0, np.nan))

    bc = bench.frame["adj_close"].reindex(df.index)
    blr = ind.log_returns(bench.frame["adj_close"]).reindex(df.index)
    out["mkt_ret_20"] = blr.rolling(20, min_periods=20).sum()
    out["mkt_vol20"] = ind.realized_vol(bench.frame["adj_close"], 20).reindex(df.index)
    out["mkt_dist_sma200"] = (bench.frame["adj_close"] / ind.sma(bench.frame["adj_close"], 200) - 1).reindex(df.index)
    if pf.symbol == bench.symbol:
        out["excess_ret_20"], out["beta_120"] = 0.0, 1.0
    else:
        out["excess_ret_20"] = out["ret_20"] - out["mkt_ret_20"]
        out["beta_120"] = ind.rolling_beta_corr(c, bc, 120)["beta"]

    if sentiment is not None:
        out = out.join(_news_features(sentiment, "sent"), how="left")
        out["sent_score_7d"] = out["sent_score_7d"].fillna(0.0)
        out["sent_count_7d"] = out["sent_count_7d"].fillna(0.0)
        out["sent_insufficient"] = out["sent_insufficient"].fillna(1.0)
        mkt = _news_features(market_sentiment if market_sentiment is not None else pd.DataFrame(), "mkt_sent")
        out = out.join(mkt[["mkt_sent_score_7d"]], how="left")
        out["mkt_sent_score_7d"] = out["mkt_sent_score_7d"].fillna(0.0)

    out = out.replace([np.inf, -np.inf], np.nan)
    out["close"], out["adj_close"] = df["close"], c
    position = np.arange(len(df))
    out["usable"] = (position >= WARMUP_SESSIONS) & ~df["near_unexplained_gap"].to_numpy()
    return out


def _news_features(daily: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """`daily` is indexed by session date with columns score (NaN = insufficient) and n_events."""
    if daily is None or daily.empty:
        return pd.DataFrame(columns=[f"{prefix}_score_7d", f"{prefix}_count_7d", f"{prefix}_insufficient"])
    out = pd.DataFrame(index=daily.index)
    out[f"{prefix}_score_7d"] = daily["score"].fillna(0.0)
    out[f"{prefix}_count_7d"] = np.log1p(daily["n_events"].astype(float))
    out[f"{prefix}_insufficient"] = (daily["n_events"] < 1).astype(float)
    return out


def build_targets(pf: PriceFrame, calendar: pd.DatetimeIndex, horizons: tuple[int, ...]) -> pd.DataFrame:
    """Forward adjusted simple returns per horizon, NaN where the window is not clean."""
    df = pf.frame
    c = df["adj_close"]
    out = pd.DataFrame(index=df.index)
    # Exchange sessions elapsed between consecutive bars of this symbol (1 = no gap).
    cal_pos = pd.Series(np.arange(len(calendar)), index=calendar)
    pos = cal_pos.reindex(df.index)
    unexplained = df["unexplained_gap"].astype(int)
    for h in horizons:
        fwd = c.shift(-h) / c - 1
        sessions_spanned = pos.shift(-h) - pos
        crosses_gap = unexplained[::-1].rolling(h, min_periods=1).max()[::-1].shift(-1).fillna(0).astype(bool)
        valid = (sessions_spanned == h) & ~crosses_gap
        out[f"y_{h}"] = fwd.where(valid)
        out[f"target_date_{h}"] = pd.Series(df.index, index=df.index).shift(-h)
    return out


def feature_docs() -> list[dict[str, str]]:
    return [{"name": f.name, "group": f.group, "description": f.description,
             "availability": "close of the origin session", "source": "daily_bars (adjusted)" if f.group != "news"
             else "news_sentiment_daily"} for f in TECH_FEATURES + NEWS_FEATURES]
