"""Live forecasts (stored before outcomes exist), outcome evaluation and ensemble weights.

Price basis: targets are returns on split/bonus-adjusted closes. At the origin, no later corporate
action is known, so the adjusted and raw origin close coincide and implied price = raw origin close
x (1 + predicted return), in the share units trading at the origin. If an action occurs inside the
forecast window, the evaluated actual return is computed on the adjusted basis (comparable), while
`actual_price` is the raw target close (in post-action units); the dashboard flags such cases.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.extensions import db
from app.models.forecasting import EnsembleWeights, Forecast, ForecastOutcome, ModelVersion
from app.services.forecasting import registry
from app.services.forecasting.dataset import Panel, build_panel
from app.services.forecasting.metrics import probability_status
from app.services.research.features import FEATURE_SET_TECH
from app.services.research.prices import load_universe, observed_calendar

log = logging.getLogger(__name__)

RETURN_KEYS = ("zero", "hist_mean", "ridge_tech", "hgb_tech", "rf_tech")
ENSEMBLE_MEMBERS = ("hist_mean", "ridge_tech", "hgb_tech", "rf_tech")
ENSEMBLE_WINDOW = 250
ENSEMBLE_MIN_MATURED = 150
ENSEMBLE_SHRINK = 0.5


def generate_forecasts(config: Mapping[str, Any], *, pipeline_run_id: uuid.UUID | None = None,
                       panel: Panel | None = None) -> dict[str, Any]:
    horizons = tuple(config["FORECAST_HORIZONS"])
    panel = panel or build_panel(config, feature_set=FEATURE_SET_TECH, horizons=horizons)
    data = panel.data
    origin = data["date"].max()
    rows = data[data["date"] == origin].set_index("symbol")
    created, skipped, warnings = 0, 0, []
    exp = registry.latest_experiment("tech_walkforward_v1")
    for h in horizons:
        primary_key, primary_evidence = registry.primary_model(h)
        versions = {k: registry.approved(k, h) for k in (*RETURN_KEYS, "logit_tech")}
        missing = [k for k, v in versions.items() if v is None]
        if missing:
            warnings.append(f"h={h}: no approved version for {', '.join(missing)}")
        loaded = {k: registry.load_artifact(v) for k, v in versions.items() if v is not None}
        weights_row = ensemble_weights(origin.date(), h)
        ensemble_calib = registry.residual_pool("ensemble", h, panel)
        for symbol, row in rows.iterrows():
            if not row["usable"] or row[panel.features].isna().any():
                warnings.append(f"{symbol} h={h}: features unusable at {origin.date()} (warm-up or unexplained gap)")
                skipped += 1
                continue
            x = row.to_frame().T
            sym = pd.Series([symbol])
            preds = {k: float(m.predict(x, sym)[0]) for k, m in loaded.items() if m.kind == "return"}
            prob = float(loaded["logit_tech"].predict(x, sym)[0]) if "logit_tech" in loaded else None
            members = [k for k in ENSEMBLE_MEMBERS if k in preds]
            if len(members) >= 2:
                w = weights_row.weights if weights_row else {k: 1 / len(members) for k in members}
                preds["ensemble"] = float(sum(preds[k] * w.get(k, 0.0) for k in members) / sum(w.get(k, 0.0)
                                                                                               for k in members))
            for key, pred in preds.items():
                mv_ids = [str(versions[k].id) for k in (members if key == "ensemble" else [key]) if versions.get(k)]
                created += _store_forecast(
                    symbol=symbol, origin=origin.date(), horizon=h, model_key=key, pred=pred, row=row,
                    mv_ids=mv_ids, weights_id=weights_row.id if (key == "ensemble" and weights_row) else None,
                    calib=(versions[key].interval_calibration if versions.get(key) else ensemble_calib),
                    prob=prob, prob_status=_prob_status(exp, h), reliability=_reliability(exp, key, h, primary_key),
                    panel=panel, pipeline_run_id=pipeline_run_id)
    db.session.commit()
    summary = {"origin": origin.date().isoformat(), "created": created, "skipped": skipped, "warnings": warnings}
    log.info("forecasts generated", extra={"event_type": "forecasts_generated", "details": summary})
    return summary


def _store_forecast(*, symbol: str, origin: date, horizon: int, model_key: str, pred: float, row: pd.Series,
                    mv_ids: list[str], weights_id: int | None, calib: dict[str, Any], prob: float | None,
                    prob_status: str, reliability: dict[str, Any], panel: Panel,
                    pipeline_run_id: uuid.UUID | None) -> int:
    exists = db.session.scalar(select(Forecast.id).where(Forecast.symbol == symbol, Forecast.origin_date == origin,
                                                         Forecast.horizon == horizon, Forecast.model_key == model_key))
    if exists is not None:
        return 0  # forecasts are immutable; a rerun never replaces one
    vol = float(row["vol20"])
    lower = upper = median = None
    interval_status = calib.get("status", "insufficient_evidence") if calib else "insufficient_evidence"
    if calib and "q_lo" in calib and np.isfinite(vol):
        lower, upper = pred + calib["q_lo"] * vol, pred + calib["q_hi"] * vol
        median = pred + calib.get("q_median", 0.0) * vol
    ref = Decimal(str(round(float(row["close"]), 6)))
    warnings = []
    if interval_status != "calibrated":
        warnings.append(f"prediction interval {interval_status.replace('_', ' ')} in out-of-sample tests")
    if prob_status != "calibrated":
        warnings.append("direction probability not calibrated; shown for research only")
    db.session.add(Forecast(
        symbol=symbol, origin_date=origin, horizon=horizon, model_key=model_key, model_version_ids=mv_ids,
        ensemble_weights_id=weights_id, feature_set_version=panel.feature_set, data_version=_short(panel.data_version),
        reference_price=ref, predicted_return=pred, median_return=median, lower_return=lower, upper_return=upper,
        interval_level=0.90 if lower is not None else None, interval_status=interval_status,
        prob_up=prob, prob_status=prob_status, implied_price=ref * Decimal(str(round(1 + pred, 8))),
        reliability=reliability, warnings=warnings, pipeline_run_id=pipeline_run_id))
    return 1


def _prob_status(exp: Any, h: int) -> str:
    if exp is None:
        return "insufficient_evidence"
    row = next((r for r in exp.results.get("direction", []) if r["model"] == "logit_tech" and r["horizon"] == h
                and r["split"] == "holdout"), None)
    return probability_status(row) if row else "insufficient_evidence"


def _reliability(exp: Any, key: str, h: int, primary_key: str) -> dict[str, Any]:
    exp_key = "ens_equal_tech" if key == "ensemble" else key
    out: dict[str, Any] = {"is_primary": key == primary_key, "primary_model": primary_key}
    if exp is None:
        return out
    for split in ("validation", "holdout"):
        r = next((x for x in exp.results.get("by_model", []) if x["model"] == exp_key and x["horizon"] == h
                  and x["split"] == split), None)
        if r:
            out[split] = {k: r.get(k) for k in ("n", "n_eff", "mae", "rmse", "mse_skill_vs_zero", "dm_pvalue_vs_zero",
                                                "directional_accuracy", "coverage_90", "period")}
    out["experiment_id"] = str(exp.id)
    return out


def _short(v: str) -> str:
    import hashlib

    return v if len(v) <= 64 else hashlib.sha256(v.encode()).hexdigest()


# --------------------------------------------------------------------------------------------------
# Outcome evaluation


def evaluate_matured(config: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate forecasts whose target session now has a validated bar. Never before maturity."""
    pending = db.session.scalars(select(Forecast).outerjoin(ForecastOutcome, ForecastOutcome.forecast_id == Forecast.id)
                                 .where(ForecastOutcome.id.is_(None))).all()
    if not pending:
        return {"evaluated": 0, "invalid": 0, "pending": 0}
    frames = load_universe(tuple(config["MODEL_SYMBOLS"]))
    calendar = observed_calendar(frames)
    cal_pos = pd.Series(np.arange(len(calendar)), index=calendar)
    evaluated = invalid = still_pending = 0
    for f in pending:
        df = frames[f.symbol].frame
        origin = pd.Timestamp(f.origin_date)
        if origin not in df.index:
            _outcome(f, "invalid", "origin bar no longer present (data revision)")
            invalid += 1
            continue
        pos = df.index.get_loc(origin)
        if pos + f.horizon >= len(df):
            still_pending += 1
            continue
        target = df.index[pos + f.horizon]
        if cal_pos.get(target, -1) - cal_pos.get(origin, -1) != f.horizon:
            _outcome(f, "invalid", "symbol did not trade on every exchange session in the window", target.date())
            invalid += 1
            continue
        window = df.iloc[pos + 1:pos + f.horizon + 1]
        if window["unexplained_gap"].any():
            _outcome(f, "invalid", "unexplained price discontinuity in the window", target.date())
            invalid += 1
            continue
        actual = float(df["adj_close"].iloc[pos + f.horizon] / df["adj_close"].iloc[pos] - 1)
        note = "corporate action in window: actual on adjusted basis" if window["action_on_date"].any() else None
        err = f.predicted_return - actual
        direction = (None if f.predicted_return == 0 or actual == 0
                     else bool(np.sign(f.predicted_return) == np.sign(actual)))
        inside = None if f.lower_return is None else bool(f.lower_return <= actual <= f.upper_return)
        raw_target = Decimal(str(round(float(df["close"].iloc[pos + f.horizon]), 6)))
        _outcome(f, "evaluated", note, target.date(), actual, raw_target, err, direction, inside)
        evaluated += 1
    db.session.commit()
    return {"evaluated": evaluated, "invalid": invalid, "pending": still_pending}


def _outcome(f: Forecast, status: str, reason: str | None, target: date | None = None, actual: float | None = None,
             price: Decimal | None = None, err: float | None = None, direction: bool | None = None,
             inside: bool | None = None) -> None:
    db.session.add(ForecastOutcome(forecast_id=f.id, status=status,
                                   invalid_reason=reason if status == "invalid" else None,
                                   note=reason if status == "evaluated" else None, target_date=target,
                                   actual_return=actual, actual_price=price, error=err, direction_correct=direction,
                                   in_interval=inside))


# --------------------------------------------------------------------------------------------------
# Ensemble weights


def ensemble_weights(session: date, horizon: int) -> EnsembleWeights | None:
    return db.session.scalar(select(EnsembleWeights).where(EnsembleWeights.session_date <= session,
                                                           EnsembleWeights.horizon == horizon)
                             .order_by(EnsembleWeights.session_date.desc(), EnsembleWeights.id.desc()).limit(1))


def update_ensemble_weights(session: date, horizons: tuple[int, ...], *, adaptive_retained: dict[int, bool]
                            ) -> dict[str, Any]:
    """Compute equal weights and (evidence permitting) shrunk inverse-MSE weights from LIVE matured
    forecasts. The adaptive weights are only put in force when the experiment retained them."""
    out = {}
    for h in horizons:
        rows = db.session.execute(
            select(Forecast.model_key, ForecastOutcome.error).join(ForecastOutcome, ForecastOutcome.forecast_id ==
                                                                   Forecast.id)
            .where(Forecast.horizon == h, Forecast.model_key.in_(ENSEMBLE_MEMBERS),
                   ForecastOutcome.status == "evaluated", ForecastOutcome.target_date <= session)
            .order_by(ForecastOutcome.target_date.desc()).limit(ENSEMBLE_WINDOW * len(ENSEMBLE_MEMBERS) * 5)).all()
        df = pd.DataFrame(rows, columns=["model", "error"])
        k = len(ENSEMBLE_MEMBERS)
        equal = {m: 1 / k for m in ENSEMBLE_MEMBERS}
        counts = df.groupby("model").size().reindex(ENSEMBLE_MEMBERS).fillna(0)
        method, weights, evidence = "equal", equal, {"matured_per_model": counts.astype(int).to_dict()}
        if adaptive_retained.get(h) and counts.min() >= ENSEMBLE_MIN_MATURED:
            mse = (df.assign(se=df["error"] ** 2).groupby("model")["se"].mean()).reindex(ENSEMBLE_MEMBERS)
            inv = 1 / mse.clip(lower=1e-12)
            weights = {m: float(ENSEMBLE_SHRINK / k + (1 - ENSEMBLE_SHRINK) * inv[m] / inv.sum())
                       for m in ENSEMBLE_MEMBERS}
            method, evidence["mse"] = "inverse_mse_shrunk", mse.round(8).to_dict()
        elif not adaptive_retained.get(h):
            evidence["reason"] = "adaptive weighting not retained by the walk-forward experiment; equal weights"
        else:
            evidence["reason"] = f"fewer than {ENSEMBLE_MIN_MATURED} matured live forecasts per model; equal weights"
        existing = db.session.scalar(select(EnsembleWeights).where(EnsembleWeights.session_date == session,
                                                                   EnsembleWeights.horizon == h,
                                                                   EnsembleWeights.method == method))
        if existing is None:
            db.session.add(EnsembleWeights(session_date=session, horizon=h, method=method, weights=weights,
                                           evidence=evidence))
        out[str(h)] = {"method": method, "weights": weights, **evidence}
    db.session.commit()
    return out


def live_performance(horizon: int | None = None) -> pd.DataFrame:
    q = select(Forecast.symbol, Forecast.horizon, Forecast.model_key, Forecast.origin_date, Forecast.predicted_return,
               ForecastOutcome.actual_return, ForecastOutcome.error, ForecastOutcome.direction_correct,
               ForecastOutcome.in_interval, ForecastOutcome.target_date).join(
        ForecastOutcome, ForecastOutcome.forecast_id == Forecast.id).where(ForecastOutcome.status == "evaluated")
    if horizon:
        q = q.where(Forecast.horizon == horizon)
    return pd.DataFrame(db.session.execute(q).all(), columns=["symbol", "horizon", "model_key", "origin_date", "pred",
                                                             "actual", "error", "direction_correct", "in_interval",
                                                             "target_date"])


def approved_versions() -> list[ModelVersion]:
    return list(db.session.scalars(select(ModelVersion).where(ModelVersion.status == "approved")
                                   .order_by(ModelVersion.horizon, ModelVersion.model_key)))
