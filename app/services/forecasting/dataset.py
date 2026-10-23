"""Pooled modelling panel: one row per (symbol, origin session) with point-in-time features and
forward-return targets. Pooling the five instruments gives ~5x the training rows of a single
symbol; all features are scale-free so instruments are comparable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.services.research.features import (
    FEATURE_SET_NEWS,
    FEATURE_SET_TECH,
    FEATURE_SETS,
    build_features,
    build_targets,
)
from app.services.research.prices import BENCHMARK, PriceFrame, load_universe, observed_calendar

SENTIMENT_METHOD_DEFAULT = "dedup_mean_v1"


@dataclass
class Panel:
    data: pd.DataFrame  # columns: symbol, date, cal_pos, <features>, y_h, target_date_h, close, usable
    feature_set: str
    features: list[str]
    horizons: tuple[int, ...]
    calendar: pd.DatetimeIndex
    data_version: str


def build_panel(config: Mapping[str, Any], *, feature_set: str = FEATURE_SET_TECH,
                horizons: tuple[int, ...] = (1, 5, 20), frames: dict[str, PriceFrame] | None = None,
                sentiment_method: str = SENTIMENT_METHOD_DEFAULT) -> Panel:
    symbols = tuple(config["MODEL_SYMBOLS"])
    frames = frames or load_universe(symbols)
    calendar = observed_calendar(frames)
    bench = frames[BENCHMARK]
    parts = []
    for symbol in symbols:
        pf = frames[symbol]
        if pf.frame.empty:
            continue
        sentiment = market_sentiment = None
        if feature_set == FEATURE_SET_NEWS:
            from app.services.news.ingest import sentiment_series

            sentiment = sentiment_series("MARKET" if symbol == BENCHMARK else symbol, sentiment_method)
            market_sentiment = sentiment_series("MARKET", sentiment_method).rename(
                columns={"score": "score", "n_events": "n_events"})
        feats = build_features(pf, bench, sentiment, market_sentiment)
        targets = build_targets(pf, calendar, horizons)
        df = feats.join(targets)
        df["symbol"] = symbol
        parts.append(df)
    data = pd.concat(parts).rename_axis("date").reset_index()
    cal_pos = pd.Series(np.arange(len(calendar)), index=calendar)
    data["cal_pos"] = cal_pos.reindex(data["date"]).to_numpy()
    if feature_set == FEATURE_SET_NEWS:
        # Rows without news coverage are not "insufficient evidence"; they are unusable for model B.
        coverage = pd.Timestamp(config["NEWS_COVERAGE_START"]) + pd.Timedelta(days=7)
        data.loc[data["date"] < coverage, "usable"] = False
    version = "|".join(f"{s}:{frames[s].data_version}" for s in symbols if s in frames)
    return Panel(data.sort_values(["date", "symbol"]).reset_index(drop=True), feature_set, FEATURE_SETS[feature_set],
                 horizons, calendar, version)
