"""Forecast evaluation metrics.

Overlapping horizons: for h > 1, consecutive origins share h-1 sessions of their target window,
so errors are autocorrelated. `n_eff` (= n / h, per symbol) is reported next to n, and the
Diebold-Mariano test uses a Newey-West (HAC) variance with lag h-1.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score


def _r(x: float | None, nd: int = 5) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def regression_metrics(df: pd.DataFrame, horizon: int, baseline_sq_err: pd.Series | None = None) -> dict[str, Any]:
    """`df` columns: pred, actual, optional lower/upper. Index aligned with baseline_sq_err."""
    if df.empty:
        return {"n": 0}
    err = df["pred"] - df["actual"]
    out: dict[str, Any] = {
        "n": int(len(df)), "n_eff": int(len(df) / max(horizon, 1)),
        "mae": _r(err.abs().mean(), 6), "rmse": _r(math.sqrt((err ** 2).mean()), 6),
        "bias": _r(err.mean(), 6),
    }
    moving = df["actual"] != 0
    nonzero_pred = df["pred"] != 0
    if (moving & nonzero_pred).sum() > 0:
        hits = np.sign(df.loc[moving & nonzero_pred, "pred"]) == np.sign(df.loc[moving & nonzero_pred, "actual"])
        out["directional_accuracy"] = _r(hits.mean(), 4)
    else:
        out["directional_accuracy"] = None  # undefined for a constant-zero forecast
    out["actual_up_rate"] = _r((df["actual"] > 0).mean(), 4)
    if {"lower", "upper"} <= set(df.columns) and df["lower"].notna().any():
        iv = df.dropna(subset=["lower", "upper"])
        out["interval_n"] = int(len(iv))
        out["coverage_90"] = _r(((iv["actual"] >= iv["lower"]) & (iv["actual"] <= iv["upper"])).mean(), 4)
        out["width_90"] = _r((iv["upper"] - iv["lower"]).mean(), 6)
    if baseline_sq_err is not None:
        base = baseline_sq_err.reindex(df.index)
        base_mae = np.sqrt(base).mean()  # baseline is the zero forecast: |0 - actual|
        out["mae_skill_vs_zero"] = _r(1 - err.abs().mean() / base_mae, 4) if base_mae > 0 else None
        out["mse_skill_vs_zero"] = _r(1 - (err ** 2).mean() / base.mean(), 4) if base.mean() > 0 else None
        dm = diebold_mariano(err ** 2, base, horizon)
        out["dm_stat_vs_zero"], out["dm_pvalue_vs_zero"] = _r(dm[0], 3), _r(dm[1], 4)
    return out


def diebold_mariano(loss_a: pd.Series, loss_b: pd.Series, horizon: int) -> tuple[float, float]:
    """DM test of equal predictive accuracy (two-sided). Negative stat = model A has lower loss.
    Losses are averaged per origin date across symbols first, then HAC variance with lag h-1."""
    d = (loss_a - loss_b).dropna()
    if hasattr(d.index, "get_level_values") and d.index.nlevels > 1:
        d = d.groupby(level=0).mean()
    n = len(d)
    if n < 30:
        return float("nan"), float("nan")
    d = d.to_numpy(dtype=float)
    mean = d.mean()
    dc = d - mean
    lag = max(horizon - 1, 0)
    var = np.dot(dc, dc) / n
    for k in range(1, lag + 1):
        var += 2 * (1 - k / (lag + 1)) * np.dot(dc[k:], dc[:-k]) / n
    if var <= 0:
        return float("nan"), float("nan")
    stat = mean / math.sqrt(var / n)
    return stat, 2 * (1 - stats.norm.cdf(abs(stat)))


def classification_metrics(df: pd.DataFrame, horizon: int, bins: int = 10) -> dict[str, Any]:
    """`df` columns: prob, actual (return). Includes base-rate comparison and reliability bins."""
    df = df.dropna(subset=["prob"])
    df = df[df["actual"] != 0]
    if len(df) < 30:
        return {"n": int(len(df))}
    y = (df["actual"] > 0).astype(int).to_numpy()
    p = df["prob"].clip(1e-6, 1 - 1e-6).to_numpy()
    base = np.full_like(p, y.mean())
    out: dict[str, Any] = {"n": int(len(df)), "n_eff": int(len(df) / max(horizon, 1)),
                           "brier": _r(brier_score_loss(y, p)), "brier_climatology": _r(brier_score_loss(y, base)),
                           "log_loss": _r(log_loss(y, p, labels=[0, 1])),
                           "balanced_accuracy": _r(_balanced_accuracy(y, p >= 0.5), 4)}
    out["auc"] = _r(roc_auc_score(y, p), 4) if len(np.unique(y)) == 2 else None
    out["brier_skill"] = _r(1 - out["brier"] / out["brier_climatology"], 4) if out["brier_climatology"] else None
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    table, ece = [], 0.0
    for b in range(bins):
        mask = idx == b
        if mask.sum() == 0:
            continue
        mean_p, freq = float(p[mask].mean()), float(y[mask].mean())
        ece += mask.sum() / len(p) * abs(mean_p - freq)
        table.append({"bin": f"{edges[b]:.1f}-{edges[b + 1]:.1f}", "n": int(mask.sum()), "mean_prob": round(mean_p, 4),
                      "observed_freq": round(freq, 4)})
    out["ece"], out["reliability"] = _r(ece, 4), table
    return out


def _balanced_accuracy(y: np.ndarray, pred: np.ndarray) -> float:
    pos, neg = y == 1, y == 0
    tpr = (pred[pos] == 1).mean() if pos.any() else np.nan
    tnr = (pred[neg] == 0).mean() if neg.any() else np.nan
    return float(np.nanmean([tpr, tnr]))


def probability_status(m: dict[str, Any], *, max_ece: float = 0.03, min_n: int = 500) -> str:
    """'calibrated' only when there is enough out-of-sample evidence that probabilities are
    reliable AND better than the base rate; otherwise not shown as a calibrated probability."""
    if m.get("n", 0) < min_n or m.get("ece") is None:
        return "insufficient_evidence"
    if m["ece"] <= max_ece and (m.get("brier_skill") or 0) > 0:
        return "calibrated"
    return "not_calibrated"


def interval_status(m: dict[str, Any], *, target: float = 0.90, tolerance: float = 0.03, min_n: int = 300) -> str:
    cov = m.get("coverage_90")
    if cov is None or m.get("interval_n", 0) < min_n:
        return "insufficient_evidence"
    return "calibrated" if abs(cov - target) <= tolerance else ("too_narrow" if cov < target else "too_wide")


def strategy_simulation(df: pd.DataFrame, cost_per_side: float = 0.0015) -> dict[str, Any]:
    """Hypothetical long/flat daily strategy for h=1 predictions: hold a symbol for the next session
    when its predicted return exceeds the round-trip cost; equal-weight across held symbols.
    Costs: `cost_per_side` per unit traded (commission + taxes approximation). Ignores slippage,
    liquidity, capital-gains tax and execution at the close. Compared with equal-weight buy-and-hold."""
    if df.empty:
        return {}
    d = df.copy()
    d["pos"] = (d["pred"] > 2 * cost_per_side).astype(float)
    d = d.sort_values(["symbol", "date"])
    d["trade"] = d.groupby("symbol")["pos"].diff().abs().fillna(d["pos"])
    d["net"] = d["pos"] * d["actual"] - d["trade"] * cost_per_side
    daily = d.groupby("date").agg(net=("net", "mean"), bh=("actual", "mean"), exposure=("pos", "mean"),
                                  turnover=("trade", "mean"))

    def summary(r: pd.Series) -> dict[str, Any]:
        eq = (1 + r).cumprod()
        years = len(r) / 245
        return {"annual_return": _r(eq.iloc[-1] ** (1 / years) - 1, 4) if years > 0 else None,
                "annual_vol": _r(r.std() * math.sqrt(245), 4),
                "sharpe": _r(r.mean() / r.std() * math.sqrt(245), 3) if r.std() > 0 else None,
                "max_drawdown": _r((eq / eq.cummax() - 1).min(), 4)}

    return {"strategy": summary(daily["net"]), "buy_and_hold": summary(daily["bh"]),
            "avg_exposure": _r(daily["exposure"].mean(), 3), "avg_daily_turnover": _r(daily["turnover"].mean(), 3),
            "cost_per_side": cost_per_side, "days": int(len(daily)),
            "assumptions": "hypothetical; close-to-close execution, no slippage/liquidity limits/CGT"}
