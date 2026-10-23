"""Pre-registered experiments (configurations fixed before results were seen).

  E1 tech_walkforward_v1   Model A candidates + baselines + ensembles on the full history.
  E2 news_vs_tech_v1       Model A vs Model B (A + sentiment) on IDENTICAL rows and periods
                           restricted to the news-coverage window; both trained the same way.
  E3 signal_studies_v1     Descriptive tests of indicator hypotheses (MA windows, RSI-only vs
                           trend+momentum) on validation and holdout periods.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from app.models.forecasting import Experiment
from app.services.forecasting.backtest import BacktestConfig, evaluate, persist_experiment, run_walk_forward
from app.services.forecasting.dataset import build_panel
from app.services.forecasting.models import candidate_models
from app.services.research.features import FEATURE_SET_NEWS, FEATURE_SET_TECH, FEATURE_SETS
from app.services.research.prices import load_universe

log = logging.getLogger(__name__)

E1 = BacktestConfig(
    name="tech_walkforward_v1",
    hypothesis="Price/volume/market features (Model A) forecast 1-, 5- and 20-session returns better than the "
               "no-change and historical-mean baselines out of sample, and adaptive ensemble weights beat equal "
               "weights.",
    eval_start=date(2019, 7, 1), holdout_start=date(2024, 1, 1), refit_every=42, min_train_rows=1500)

E2 = BacktestConfig(
    name="news_vs_tech_v1",
    hypothesis="Adding correctly timed headline sentiment (Model B) improves out-of-sample return forecasts over "
               "Model A trained on identical rows and periods.",
    eval_start=date(2024, 1, 1), holdout_start=date(2025, 7, 1), refit_every=21, min_train_rows=900,
    ensemble_suffix="news")


def run_e1(config: Mapping[str, Any], *, store_predictions: bool = True) -> Experiment:
    frames = load_universe(tuple(config["MODEL_SYMBOLS"]))
    panel = build_panel(config, feature_set=FEATURE_SET_TECH, horizons=E1.horizons, frames=frames)
    preds = run_walk_forward(panel, candidate_models(FEATURE_SETS[FEATURE_SET_TECH], "tech"), E1)
    results = evaluate(preds, E1)
    return persist_experiment(E1, panel, preds, results, _conclusion(results), store_predictions=store_predictions)


def run_e2(config: Mapping[str, Any], sentiment_method: str = "dedup_mean_v1") -> Experiment:
    frames = load_universe(tuple(config["MODEL_SYMBOLS"]))
    panel = build_panel(config, feature_set=FEATURE_SET_NEWS, horizons=E2.horizons, frames=frames,
                        sentiment_method=sentiment_method)
    tech = [m for m in candidate_models(FEATURE_SETS[FEATURE_SET_TECH], "tech") if m.key.startswith(("ridge", "hgb",
                                                                                                     "logit"))]
    news = [m for m in candidate_models(FEATURE_SETS[FEATURE_SET_NEWS], "news") if m.key not in ("base_rate",)]
    cfg = BacktestConfig(**{**E2.__dict__, "extra": {"sentiment_method": sentiment_method}})
    preds = run_walk_forward(panel, news + tech, cfg)  # news list includes zero + hist_mean baselines
    results = evaluate(preds, cfg)
    results["model_b_vs_a"] = _compare_ab(results)
    coverage = _sentiment_coverage(panel)
    results["sentiment_coverage"] = coverage
    conclusion = _conclusion(results) + " " + _ab_text(results["model_b_vs_a"], coverage)
    return persist_experiment(cfg, panel, preds, results, conclusion)


def _compare_ab(results: dict[str, Any]) -> list[dict[str, Any]]:
    rows = {(r["model"], r["horizon"], r["split"]): r for r in results["by_model"]}
    out = []
    for (model, h, split), r in rows.items():
        if not model.endswith("_news") or model.startswith("ens_"):
            continue
        twin = rows.get((model.replace("_news", "_tech"), h, split))
        if twin is None:
            continue
        out.append({"model_b": model, "model_a": twin["model"], "horizon": h, "split": split, "n": r["n"],
                    "rmse_b": r["rmse"], "rmse_a": twin["rmse"],
                    "mse_skill_b": r.get("mse_skill_vs_zero"), "mse_skill_a": twin.get("mse_skill_vs_zero"),
                    "b_better": (r["rmse"] or 1) < (twin["rmse"] or 0)})
    return out


def _sentiment_coverage(panel) -> dict[str, Any]:  # noqa: ANN001
    d = panel.data[panel.data["usable"]]
    if d.empty or "sent_insufficient" not in d:
        return {}
    return {"rows": int(len(d)), "share_with_news": round(float(1 - d["sent_insufficient"].mean()), 4),
            "by_symbol": {s: round(float(1 - g["sent_insufficient"].mean()), 4) for s, g in d.groupby("symbol")}}


def _ab_text(ab: list[dict[str, Any]], coverage: dict[str, Any]) -> str:
    val = [r for r in ab if r["split"] == "validation"]
    wins = sum(r["b_better"] for r in val)
    return (f"Model B beat its Model A twin on validation RMSE in {wins} of {len(val)} model/horizon pairs; "
            f"{coverage.get('share_with_news', 0):.0%} of rows had at least one relevant news event in the "
            "prior 7 days.")


def _conclusion(results: dict[str, Any]) -> str:
    parts = []
    for h, sel in sorted(results.get("selection", {}).items(), key=lambda kv: int(kv[0])):
        hold = next((r for r in results["by_model"] if r["split"] == "holdout" and r["horizon"] == int(h)
                     and r["model"] == sel["selected"]), None)
        hold_txt = (f"; holdout MSE skill {hold.get('mse_skill_vs_zero')}, DM p={hold.get('dm_pvalue_vs_zero')}"
                    if hold else "")
        parts.append(f"h={h}: selected {sel['selected']} ({sel['reason']}){hold_txt}; adaptive ensemble weights "
                     f"{'retained' if sel['adaptive_weights_retained'] else 'not retained'}.")
    return " ".join(parts)


# --------------------------------------------------------------------------------------------------
# E3: indicator hypothesis studies (descriptive, chronological)


def run_e3(config: Mapping[str, Any]) -> Experiment:
    frames = load_universe(tuple(config["MODEL_SYMBOLS"]))
    panel = build_panel(config, feature_set=FEATURE_SET_TECH, horizons=(5, 20), frames=frames)
    d = panel.data[panel.data["usable"]].copy()
    # Extra MA distances computed from adjusted closes (causal).
    for s, g in d.groupby("symbol"):
        c = frames[s].frame["adj_close"]
        for n in (20, 50, 100, 200):
            dist = (c / c.rolling(n, min_periods=n).mean() - 1).reindex(g["date"]).to_numpy()
            d.loc[g.index, f"above_sma{n}"] = dist > 0
    d["split"] = np.where(d["date"] < pd.Timestamp(E1.holdout_start), "validation", "holdout")
    d = d[d["date"] >= pd.Timestamp(E1.eval_start)]
    results: dict[str, Any] = {"ma_windows": [], "rsi_vs_trend": []}
    for h in (5, 20):
        y = f"y_{h}"
        dd = d.dropna(subset=[y])
        for split, g in dd.groupby("split"):
            for n in (20, 50, 100, 200):
                results["ma_windows"].append({"horizon": h, "split": split, "ma": n, **_signal_stats(g[y],
                                                                                                    g[f"above_sma{n}"])})
            rsi = g["rsi14"] * 100
            rsi_signal = pd.Series(np.where(rsi < 30, True, np.where(rsi > 70, False, np.nan)), index=g.index)
            trend_mom = (g["dist_sma50"] > 0) & (g["sma50_slope"] > 0) & (g["ret_20"] > 0)
            trend_down = (g["dist_sma50"] < 0) & (g["sma50_slope"] < 0) & (g["ret_20"] < 0)
            tm_signal = pd.Series(np.where(trend_mom, True, np.where(trend_down, False, np.nan)), index=g.index)
            results["rsi_vs_trend"].append({"horizon": h, "split": split, "signal": "rsi_30_70_mean_reversion",
                                            **_signal_stats(g[y], rsi_signal)})
            results["rsi_vs_trend"].append({"horizon": h, "split": split, "signal": "trend_and_momentum",
                                            **_signal_stats(g[y], tm_signal)})
    cfg = BacktestConfig(name="signal_studies_v1", hypothesis=(
        "Fixed indicator rules (price above SMA-n; RSI 30/70 mean reversion; trend+momentum agreement) are "
        "associated with different forward returns, and the best MA window is stable between validation and "
        "holdout."), eval_start=E1.eval_start, holdout_start=E1.holdout_start, horizons=(5, 20))
    conclusion = _e3_conclusion(results)
    return persist_experiment(cfg, panel, pd.DataFrame(), results, conclusion, store_predictions=False)


def _signal_stats(y: pd.Series, signal: pd.Series) -> dict[str, Any]:
    """Mean forward return when the signal is True vs False, Welch t-test (overlap makes p optimistic)."""
    s = signal.dropna().astype(bool)
    on, off = y[s.index][s], y[s.index][~s]
    if len(on) < 30 or len(off) < 30:
        return {"n_on": int(len(on)), "n_off": int(len(off)), "spread": None, "p_value": None}
    t = stats.ttest_ind(on, off, equal_var=False)
    return {"n_on": int(len(on)), "n_off": int(len(off)), "mean_on": round(float(on.mean()), 5),
            "mean_off": round(float(off.mean()), 5), "spread": round(float(on.mean() - off.mean()), 5),
            "hit_rate_on": round(float((on > 0).mean()), 4), "p_value": round(float(t.pvalue), 4)}


def _e3_conclusion(results: dict[str, Any]) -> str:
    lines = []
    for h in (5, 20):
        val = [r for r in results["ma_windows"] if r["horizon"] == h and r["split"] == "validation" and r["spread"]]
        hold = {r["ma"]: r for r in results["ma_windows"] if r["horizon"] == h and r["split"] == "holdout"}
        if val:
            best = max(val, key=lambda r: r["spread"])
            hb = hold.get(best["ma"], {})
            lines.append(f"h={h}: best validation MA window SMA{best['ma']} (spread {best['spread']:+.4f}); "
                         f"holdout spread {hb.get('spread')}.")
    lines.append("p-values are optimistic because overlapping windows and pooled symbols are not independent; "
                 "results are descriptive.")
    return " ".join(lines)
