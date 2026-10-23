"""Chronological walk-forward evaluation.

At every refit date R (every `refit_every` sessions):
  * training rows: origins t with cal_pos(t) + h <= cal_pos(R) - 1, i.e. the label matured at least
    one session before R (purge/embargo for overlapping horizons; no future information);
  * all preprocessing is fitted inside each model's pipeline on those rows only;
  * predictions are made for origins in [R, next R) and stored with their realised outcomes.
Prediction intervals (90%) use split-conformal residual quantiles from the model's own earlier
out-of-sample predictions whose targets had matured by the origin. Ensemble weights at origin t
use only errors of predictions that had matured by t. Nothing is shuffled.

Splits: origins before `holdout_start` are "validation" (used for model selection); origins on or
after it are "holdout", reported but never used to choose models, weights or thresholds.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import insert

from app.extensions import db
from app.models.forecasting import BacktestPrediction, Experiment
from app.services.forecasting import metrics as M
from app.services.forecasting.dataset import Panel
from app.services.forecasting.models import ReturnModel
from app.utils.time import utcnow

log = logging.getLogger(__name__)

ENSEMBLE_MEMBERS_SUFFIXES = ("hist_mean", "ridge_", "hgb_", "rf_")


@dataclass
class BacktestConfig:
    name: str
    hypothesis: str
    eval_start: date
    holdout_start: date
    horizons: tuple[int, ...] = (1, 5, 20)
    refit_every: int = 42
    min_train_rows: int = 1500
    interval_window: int = 500
    interval_min: int = 150
    interval_level: float = 0.90
    ensemble_window_sessions: int = 250
    ensemble_min_matured: int = 150
    ensemble_shrink: float = 0.5  # weight on equal weights (regularisation toward 1/k)
    ensemble_suffix: str = "tech"
    # Interval method: "absolute" (residual quantiles) or "vol_scaled" (quantiles of residual / vol20,
    # rescaled by the origin's vol20: adapts width to the current volatility regime).
    interval_method: str = "vol_scaled"
    # Adaptive weights must reduce validation RMSE by at least this fraction vs equal weights.
    min_adaptive_improvement: float = 0.005
    extra: dict[str, Any] = field(default_factory=dict)


def run_walk_forward(panel: Panel, models: list[ReturnModel], cfg: BacktestConfig) -> pd.DataFrame:
    rows_out = []
    data = panel.data
    for h in cfg.horizons:
        y, td = f"y_{h}", f"target_date_{h}"
        feats = sorted({f for m in models for f in getattr(m, "features", [])})
        usable = data[data["usable"] & data[y].notna()].dropna(subset=feats)
        eval_rows = usable[usable["date"] >= pd.Timestamp(cfg.eval_start)]
        if eval_rows.empty:
            continue
        eval_dates = np.sort(eval_rows["cal_pos"].unique())
        refits = eval_dates[::cfg.refit_every]
        for i, p_r in enumerate(refits):
            p_next = refits[i + 1] if i + 1 < len(refits) else np.inf
            train = usable[usable["cal_pos"] <= p_r - h - 1]
            test = eval_rows[(eval_rows["cal_pos"] >= p_r) & (eval_rows["cal_pos"] < p_next)]
            if len(train) < cfg.min_train_rows or test.empty:
                continue
            for model in models:
                model.fit(train, train[y], train["symbol"])
                pred = model.predict(test, test["symbol"])
                rows_out.append(pd.DataFrame({
                    "model_key": model.key, "kind": model.kind, "symbol": test["symbol"].to_numpy(),
                    "date": test["date"].to_numpy(), "target_date": test[td].to_numpy(), "horizon": h,
                    "pred": pred, "actual": test[y].to_numpy(), "mkt_vol20": test["mkt_vol20"].to_numpy(),
                    "vol20": test["vol20"].to_numpy(),
                    "train_end": data.loc[train.index, "date"].max(), "n_train": len(train)}))
        log.info("walk-forward horizon done", extra={"event_type": "backtest_progress",
                                                     "details": {"horizon": h, "refits": len(refits)}})
    if not rows_out:
        return pd.DataFrame()
    preds = pd.concat(rows_out, ignore_index=True)
    preds = _add_ensembles(preds, cfg)
    preds = _add_intervals(preds, cfg)
    preds["split"] = np.where(preds["date"] < pd.Timestamp(cfg.holdout_start), "validation", "holdout")
    return preds


def _members(keys: list[str], suffix: str) -> list[str]:
    return [k for k in keys if k == "hist_mean" or (k.endswith(f"_{suffix}") and not k.startswith("logit"))]


def _add_ensembles(preds: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    out = [preds]
    ret = preds[preds["kind"] == "return"]
    for h, g in ret.groupby("horizon"):
        members = _members(sorted(g["model_key"].unique()), cfg.ensemble_suffix)
        if len(members) < 2:
            continue
        wide = g.pivot_table(index=["date", "symbol"], columns="model_key", values="pred")[members].dropna()
        meta = g.drop_duplicates(["date", "symbol"]).set_index(["date", "symbol"])[
            ["target_date", "actual", "mkt_vol20", "vol20"]].reindex(wide.index)
        equal = wide.mean(axis=1)
        weights = _inverse_mse_weights(wide, meta, members, cfg)
        adaptive = (wide * weights).sum(axis=1)
        for key, series, w in ((f"ens_equal_{cfg.ensemble_suffix}", equal, None),
                               (f"ens_invmse_{cfg.ensemble_suffix}", adaptive, weights)):
            frame = meta.assign(pred=series.to_numpy()).reset_index()
            frame["model_key"], frame["kind"], frame["horizon"] = key, "return", h
            if w is not None:
                frame["weights"] = [dict(zip(members, map(float, row), strict=True)) for row in w.to_numpy()]
            out.append(frame)
    return pd.concat(out, ignore_index=True)


def _inverse_mse_weights(wide: pd.DataFrame, meta: pd.DataFrame, members: list[str],
                         cfg: BacktestConfig) -> pd.DataFrame:
    """Weights at each origin date from errors of predictions matured by that date (target_date <=
    origin), over the trailing window; shrunk toward equal weights; equal until enough evidence."""
    sq = wide.sub(meta["actual"], axis=0) ** 2
    sq["target_date"] = meta["target_date"].to_numpy()
    by_maturity = sq.groupby("target_date")[members].agg(["sum", "count"])
    dates = sorted(wide.index.get_level_values(0).unique())
    all_days = sorted(set(by_maturity.index) | set(dates))
    sums = by_maturity.xs("sum", axis=1, level=1).reindex(all_days).fillna(0.0)
    counts = by_maturity.xs("count", axis=1, level=1).reindex(all_days).fillna(0.0)
    roll_sum = sums.rolling(cfg.ensemble_window_sessions, min_periods=1).sum()
    roll_cnt = counts.rolling(cfg.ensemble_window_sessions, min_periods=1).sum()
    k = len(members)
    equal = np.full(k, 1.0 / k)
    w_by_date = {}
    for d in dates:
        n = roll_cnt.loc[d].min()
        if n < cfg.ensemble_min_matured:
            w_by_date[d] = equal
            continue
        mse = (roll_sum.loc[d] / roll_cnt.loc[d]).to_numpy()
        inv = 1 / np.maximum(mse, 1e-12)
        w_by_date[d] = cfg.ensemble_shrink * equal + (1 - cfg.ensemble_shrink) * inv / inv.sum()
    w = np.vstack([w_by_date[d] for d in wide.index.get_level_values(0)])
    return pd.DataFrame(w, index=wide.index, columns=members)


def _add_intervals(preds: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    """Adds lower/upper for cfg.interval_method and alt_lower/alt_upper for the other method, so
    both can be evaluated on identical predictions."""
    preds = preds.copy()
    other = "absolute" if cfg.interval_method == "vol_scaled" else "vol_scaled"
    for method, (lo_col, hi_col) in ((cfg.interval_method, ("lower", "upper")), (other, ("alt_lower", "alt_upper"))):
        lo, hi = conformal_bounds(preds, cfg, method)
        preds[lo_col], preds[hi_col] = lo, hi
    return preds


def conformal_bounds(preds: pd.DataFrame, cfg: BacktestConfig, method: str) -> tuple[np.ndarray, np.ndarray]:
    lower, upper = np.full(len(preds), np.nan), np.full(len(preds), np.nan)
    q_lo, q_hi = (1 - cfg.interval_level) / 2, 1 - (1 - cfg.interval_level) / 2
    scale_all = preds["vol20"].to_numpy() if method == "vol_scaled" else np.ones(len(preds))
    for _, g in preds[preds["kind"] == "return"].groupby(["model_key", "horizon"]):
        g = g.sort_values("date")
        pos = preds.index.get_indexer(g.index)
        scale = scale_all[pos]
        resid = (g["actual"] - g["pred"]).to_numpy() / scale
        target = g["target_date"].to_numpy()
        order = np.argsort(target, kind="stable")
        sorted_targets, sorted_resid = target[order], resid[order]
        lows, highs = np.full(len(g), np.nan), np.full(len(g), np.nan)
        for i, d in enumerate(g["date"].to_numpy()):
            n_matured = np.searchsorted(sorted_targets, d, side="right")
            if n_matured < cfg.interval_min:
                continue
            pool = sorted_resid[max(0, n_matured - cfg.interval_window):n_matured]
            lows[i], highs[i] = np.quantile(pool, q_lo), np.quantile(pool, q_hi)
        lower[pos] = g["pred"].to_numpy() + lows * scale
        upper[pos] = g["pred"].to_numpy() + highs * scale
    return lower, upper


def evaluate(preds: pd.DataFrame, cfg: BacktestConfig) -> dict[str, Any]:
    results: dict[str, Any] = {"by_model": [], "by_symbol": [], "by_regime": [], "direction": [], "strategy": []}
    if preds.empty:
        return results
    vol_median = preds.groupby("date")["mkt_vol20"].first().expanding().median()
    preds = preds.assign(regime=np.where(preds["mkt_vol20"] > preds["date"].map(vol_median), "high_vol", "low_vol"))
    for (h, split), g in preds.groupby(["horizon", "split"]):
        zero = g[g["model_key"] == "zero"].set_index(["date", "symbol"])
        base_sq = zero["actual"] ** 2
        for key, m in g[g["kind"] == "return"].groupby("model_key"):
            mi = m.set_index(["date", "symbol"])
            row = {"model": key, "horizon": int(h), "split": split, "period": _period(m),
                   **M.regression_metrics(mi[["pred", "actual", "lower", "upper"]], int(h), base_sq)}
            row["interval_status"] = M.interval_status(row)
            row["interval_method"] = cfg.interval_method
            if "alt_lower" in m:
                alt = M.regression_metrics(mi[["pred", "actual"]].assign(lower=mi["alt_lower"], upper=mi["alt_upper"]),
                                           int(h))
                row["alt_interval"] = {"method": "absolute" if cfg.interval_method == "vol_scaled" else "vol_scaled",
                                       "coverage_90": alt.get("coverage_90"), "width_90": alt.get("width_90")}
            results["by_model"].append(row)
            for sym, ms in mi.groupby(level="symbol"):
                results["by_symbol"].append({"model": key, "horizon": int(h), "split": split, "symbol": sym,
                                             **M.regression_metrics(ms[["pred", "actual"]], int(h),
                                                                    base_sq.reindex(ms.index))})
            for regime, mr in mi.groupby(m["regime"].to_numpy()):
                r = M.regression_metrics(mr[["pred", "actual"]], int(h), base_sq.reindex(mr.index))
                results["by_regime"].append({"model": key, "horizon": int(h), "split": split, "regime": regime,
                                             "n": r["n"], "mse_skill_vs_zero": r.get("mse_skill_vs_zero"),
                                             "directional_accuracy": r.get("directional_accuracy")})
            if int(h) == 1:
                results["strategy"].append({"model": key, "split": split,
                                            **M.strategy_simulation(m[["symbol", "date", "pred", "actual"]])})
        for key, m in g[g["kind"] == "direction"].groupby("model_key"):
            row = {"model": key, "horizon": int(h), "split": split, "period": _period(m),
                   **M.classification_metrics(m.rename(columns={"pred": "prob"})[["prob", "actual"]], int(h))}
            row["probability_status"] = M.probability_status(row)
            results["direction"].append(row)
    results["selection"] = select_models(results, cfg)
    return results


def _period(m: pd.DataFrame) -> str:
    return f"{pd.Timestamp(m['date'].min()).date()}..{pd.Timestamp(m['date'].max()).date()}"


def select_models(results: dict[str, Any], cfg: BacktestConfig, alpha: float = 0.10) -> dict[str, Any]:
    """Pre-registered rule, applied to VALIDATION results only: per horizon, pick the return model
    with the best MSE skill vs the zero forecast among learned models and ensembles, provided the
    Diebold-Mariano p-value vs zero is < alpha; otherwise retain the zero baseline. Adaptive
    ensemble weights are retained only if they beat equal weights on validation MSE."""
    out = {}
    val = [r for r in results["by_model"] if r["split"] == "validation"]
    for h in sorted({r["horizon"] for r in val}):
        cands = [r for r in val if r["horizon"] == h and r["model"] not in ("zero", "hist_mean")
                 and r.get("mse_skill_vs_zero") is not None]
        best = max(cands, key=lambda r: r["mse_skill_vs_zero"], default=None)
        eq = next((r for r in cands if r["model"].startswith("ens_equal")), None)
        ad = next((r for r in cands if r["model"].startswith("ens_invmse")), None)
        adaptive_kept = bool(eq and ad and eq["rmse"] and ad["rmse"]
                             and ad["rmse"] <= eq["rmse"] * (1 - cfg.min_adaptive_improvement))
        if best and best["mse_skill_vs_zero"] > 0 and (best.get("dm_pvalue_vs_zero") or 1) < alpha:
            chosen, reason = best["model"], (f"best validation MSE skill {best['mse_skill_vs_zero']:+.4f}, "
                                             f"DM p={best['dm_pvalue_vs_zero']}")
        else:
            chosen = "zero"
            reason = ("no model beat the no-change forecast with DM p < "
                      f"{alpha} on validation (best: {best['model'] if best else 'n/a'} "
                      f"skill {best['mse_skill_vs_zero'] if best else 'n/a'}, "
                      f"p={best.get('dm_pvalue_vs_zero') if best else 'n/a'})")
        out[str(h)] = {"selected": chosen, "reason": reason, "adaptive_weights_retained": adaptive_kept,
                       "adaptive_vs_equal_rmse": [ad["rmse"] if ad else None, eq["rmse"] if eq else None]}
    return out


def code_version() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def persist_experiment(cfg: BacktestConfig, panel: Panel, preds: pd.DataFrame, results: dict[str, Any],
                       conclusion: str, *, store_predictions: bool = True) -> Experiment:
    exp = Experiment(name=cfg.name, hypothesis=cfg.hypothesis, status="completed",
                     config={**{k: (v.isoformat() if isinstance(v, date) else v) for k, v in asdict(cfg).items()},
                             "feature_set": panel.feature_set, "features": panel.features},
                     data_version=panel.data_version[:64] if len(panel.data_version) <= 64 else
                     __import__("hashlib").sha256(panel.data_version.encode()).hexdigest(),
                     code_version=code_version(), results=_jsonable(results), conclusion=conclusion,
                     finished_at=utcnow())
    db.session.add(exp)
    db.session.flush()
    if store_predictions and not preds.empty:
        records = [{"experiment_id": exp.id, "model_key": r.model_key, "symbol": r.symbol,
                    "origin_date": pd.Timestamp(r.date).date(), "horizon": int(r.horizon), "split": r.split,
                    "predicted_return": float(r.pred), "lower": _nan(r.lower), "upper": _nan(r.upper),
                    "prob_up": float(r.pred) if r.kind == "direction" else None, "actual_return": float(r.actual)}
                   for r in preds.itertuples()]
        for i in range(0, len(records), 5000):
            db.session.execute(insert(BacktestPrediction), records[i:i + 5000])
    db.session.commit()
    return exp


def _nan(v: Any) -> float | None:
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.integer | np.floating):
        return None if isinstance(obj, np.floating) and not np.isfinite(obj) else obj.item()
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    if isinstance(obj, pd.Timestamp | date):
        return obj.isoformat()
    return obj
