"""Model registry: training production versions, promotion, rejection and rollback.

Lifecycle: candidate -> approved (promotion) -> retired (superseded or rolled back); a candidate that
fails its acceptance criteria becomes rejected. Rows are never deleted, artifacts are content-hashed,
and every transition is recorded in `model_events`. A partial unique index guarantees at most one
approved version per (model_key, horizon).

Acceptance criteria for promoting a candidate (all must hold; see docs/model_governance.md):
  1. trained only on labels that had matured before the training cutoff;
  2. it re-validates on the most recent RECENT_WINDOW origins in a walk-forward that excludes
     them from training (validate-then-refit): MSE no worse than (1 + MAX_DEGRADATION) x the
     currently approved spec's MSE on the same rows, and not significantly worse than zero;
  3. sanity: finite predictions, |prediction| below SANITY_VOL_MULTIPLE x the origin's 20d vol;
  4. at least MIN_TRAIN_ROWS training rows.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sqlalchemy import func, select

from app.extensions import db
from app.models.forecasting import BacktestPrediction, Experiment, ModelEvent, ModelVersion
from app.services.forecasting.backtest import BacktestConfig, code_version, run_walk_forward
from app.services.forecasting.dataset import Panel, build_panel
from app.services.forecasting.metrics import diebold_mariano
from app.services.forecasting.models import ReturnModel, candidate_models, model_hyperparameters
from app.services.research.features import FEATURE_SET_TECH, FEATURE_SETS
from app.utils.time import utcnow

log = logging.getLogger(__name__)

PRODUCTION_KEYS = ("zero", "hist_mean", "ridge_tech", "hgb_tech", "rf_tech", "base_rate", "logit_tech")
MIN_TRAIN_ROWS = 1500
RECENT_WINDOW = 250
MAX_DEGRADATION = 0.02
SANITY_VOL_MULTIPLE = 3.0
INTERVAL_POOL = 500
INTERVAL_MIN = 150


class PromotionRefused(Exception):
    pass


def _spec(key: str) -> ReturnModel:
    for m in candidate_models(FEATURE_SETS[FEATURE_SET_TECH], "tech"):
        if m.key == key:
            return m
    raise KeyError(key)


def approved(model_key: str, horizon: int) -> ModelVersion | None:
    return db.session.scalar(select(ModelVersion).where(ModelVersion.model_key == model_key,
                                                        ModelVersion.horizon == horizon,
                                                        ModelVersion.status == "approved"))


def load_artifact(mv: ModelVersion) -> ReturnModel:
    path = Path(mv.artifact_path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != mv.artifact_sha256:
        raise RuntimeError(f"artifact hash mismatch for {mv.model_key} v{mv.version}: refusing to load")
    return joblib.load(path)


def _event(event_type: str, decision: str, reason: str, *, mv: ModelVersion | None = None, horizon: int | None = None,
           evidence: dict[str, Any] | None = None, actor: str = "system", session: date | None = None) -> None:
    db.session.add(ModelEvent(event_type=event_type, decision=decision, reason=reason,
                              model_version_id=mv.id if mv else None, horizon=horizon or (mv.horizon if mv else None),
                              evidence=evidence or {}, actor=actor, session_date=session))


def latest_experiment(name: str) -> Experiment | None:
    return db.session.scalar(select(Experiment).where(Experiment.name == name, Experiment.status == "completed")
                             .order_by(Experiment.finished_at.desc()).limit(1))


def residual_pool(model_key: str, horizon: int, panel: Panel) -> dict[str, Any]:
    """Vol-scaled residual quantiles for live intervals, from the latest E1 out-of-sample predictions
    of this model (most recent INTERVAL_POOL matured rows)."""
    exp = latest_experiment("tech_walkforward_v1")
    key = "ens_equal_tech" if model_key == "ensemble" else model_key
    if exp is None:
        return {"status": "insufficient_evidence", "reason": "no walk-forward experiment"}
    rows = db.session.execute(select(BacktestPrediction.symbol, BacktestPrediction.origin_date,
                                     BacktestPrediction.predicted_return, BacktestPrediction.actual_return)
                              .where(BacktestPrediction.experiment_id == exp.id, BacktestPrediction.model_key == key,
                                     BacktestPrediction.horizon == horizon)
                              .order_by(BacktestPrediction.origin_date.desc()).limit(INTERVAL_POOL)).all()
    if len(rows) < INTERVAL_MIN:
        return {"status": "insufficient_evidence", "n": len(rows)}
    df = pd.DataFrame(rows, columns=["symbol", "date", "pred", "actual"])
    df["date"] = pd.to_datetime(df["date"])
    vol = panel.data.set_index(["date", "symbol"])["vol20"]
    scale = vol.reindex(pd.MultiIndex.from_frame(df[["date", "symbol"]])).to_numpy()
    resid = (df["actual"] - df["pred"]).to_numpy() / scale
    resid = resid[np.isfinite(resid)]
    holdout = next((r for r in exp.results.get("by_model", []) if r["model"] == key and r["horizon"] == horizon
                    and r["split"] == "holdout"), {})
    return {"method": "vol_scaled", "level": 0.90, "q_lo": float(np.quantile(resid, 0.05)),
            "q_hi": float(np.quantile(resid, 0.95)), "q_median": float(np.quantile(resid, 0.5)), "n": int(len(resid)),
            "source_experiment": str(exp.id), "status": holdout.get("interval_status", "insufficient_evidence"),
            "holdout_coverage_90": holdout.get("coverage_90")}


def train_candidates(config: Mapping[str, Any], horizons: tuple[int, ...], *, keys: tuple[str, ...] = PRODUCTION_KEYS,
                     actor: str = "system", panel: Panel | None = None) -> list[ModelVersion]:
    panel = panel or build_panel(config, feature_set=FEATURE_SET_TECH, horizons=horizons)
    artifacts = Path(config["ARTIFACTS_DIR"]) / "models"
    artifacts.mkdir(parents=True, exist_ok=True)
    data, last_pos = panel.data, int(panel.data["cal_pos"].max())
    out = []
    for h in horizons:
        y = f"y_{h}"
        train = data[data["usable"] & data[y].notna() & (data["cal_pos"] <= last_pos - h)].dropna(subset=panel.features)
        if len(train) < MIN_TRAIN_ROWS:
            _event("training", "skipped", f"only {len(train)} matured training rows for h={h}", horizon=h, actor=actor)
            continue
        for key in keys:
            model = _spec(key)
            model.fit(train, train[y], train["symbol"])
            version = (db.session.scalar(select(func.max(ModelVersion.version)).where(
                ModelVersion.model_key == key, ModelVersion.horizon == h)) or 0) + 1
            # Unique suffix: artifacts are never overwritten, even if the registry is rebuilt.
            path = artifacts / f"{key}_h{h}_v{version}_{uuid.uuid4().hex[:8]}.joblib"
            with path.open("xb") as fh:
                joblib.dump(model, fh)
            mv = ModelVersion(model_key=key, horizon=h, version=version, model_type=type(model).__name__,
                              feature_set_version=panel.feature_set, features=list(getattr(model, "features", [])),
                              hyperparameters=model_hyperparameters(model),
                              train_start=train["date"].min().date(), train_end=train["date"].max().date(),
                              n_train=int(len(train)), data_version=_short(panel.data_version),
                              code_version=code_version(), artifact_path=str(path),
                              artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              interval_calibration=residual_pool(key, h, panel) if model.kind == "return" else {},
                              status="candidate")
            db.session.add(mv)
            db.session.flush()
            _event("training", "applied", f"trained {key} h={h} v{version} on {len(train)} rows", mv=mv, actor=actor,
                   evidence={"train_start": str(mv.train_start), "train_end": str(mv.train_end)})
            out.append(mv)
    db.session.commit()
    return out


def validate_candidate(mv: ModelVersion, config: Mapping[str, Any], panel: Panel) -> dict[str, Any]:
    """Recent-window walk-forward of the candidate's spec vs the approved spec and zero, on identical
    rows that were excluded from training at each refit (the final artifact is then the refit)."""
    h = mv.horizon
    window_start = panel.calendar[-(RECENT_WINDOW + h + 1)].date()
    cfg = BacktestConfig(name="promotion_check", hypothesis="", eval_start=window_start,
                         holdout_start=date(2100, 1, 1), horizons=(h,), refit_every=63, min_train_rows=MIN_TRAIN_ROWS)
    spec = _spec(mv.model_key)
    if spec.kind == "direction":
        return _validate_direction(mv, spec, panel, cfg)
    specs = [spec, _spec("zero")]
    preds = run_walk_forward(panel, specs, cfg)
    if preds.empty:
        return {"passed": False, "reason": "no recent out-of-sample predictions"}
    cand = preds[preds["model_key"] == mv.model_key].set_index(["date", "symbol"])
    zero = preds[preds["model_key"] == "zero"].set_index(["date", "symbol"])
    se_c, se_z = (cand["pred"] - cand["actual"]) ** 2, zero["actual"] ** 2
    dm_stat, dm_p = diebold_mariano(se_c, se_z.reindex(se_c.index), h)
    vol = panel.data.set_index(["date", "symbol"])["vol20"].reindex(cand.index)
    sane = bool(np.isfinite(cand["pred"]).all() and (cand["pred"].abs() < SANITY_VOL_MULTIPLE * vol).all())
    evidence = {"window_start": str(window_start), "n": int(len(cand)), "mse": float(se_c.mean()),
                "mse_zero": float(se_z.mean()), "dm_stat_vs_zero": _f(dm_stat), "dm_p_vs_zero": _f(dm_p),
                "sanity_ok": sane, "n_train": mv.n_train}
    current = approved(mv.model_key, h)
    evidence["approved_version"] = current.version if current else None
    reasons = []
    if mv.n_train < MIN_TRAIN_ROWS:
        reasons.append("too few training rows")
    if not sane:
        reasons.append("predictions failed sanity bounds")
    if mv.model_key != "zero" and dm_stat == dm_stat and dm_stat > 0 and (dm_p or 1) < 0.05:
        reasons.append("significantly worse than the no-change forecast on the recent window")
    evidence["passed"] = not reasons
    evidence["reason"] = "; ".join(reasons) or "meets acceptance criteria"
    return evidence


def _validate_direction(mv: ModelVersion, spec: ReturnModel, panel: Panel, cfg: BacktestConfig) -> dict[str, Any]:
    """Direction models output probabilities: sanity = finite and within [0, 1]; the recent-window
    Brier score vs the base rate is recorded. Whether probabilities are SHOWN as calibrated is decided
    separately from walk-forward holdout calibration (metrics.probability_status), not here."""
    from app.services.forecasting.metrics import classification_metrics

    preds = run_walk_forward(panel, [spec], cfg)
    if preds.empty:
        return {"passed": False, "reason": "no recent out-of-sample predictions"}
    p = preds["pred"].to_numpy()
    sane = bool(np.isfinite(p).all() and ((p >= 0) & (p <= 1)).all())
    m = classification_metrics(preds.rename(columns={"pred": "prob"})[["prob", "actual"]], mv.horizon)
    reasons = [] if sane else ["probabilities outside [0, 1] or not finite"]
    if mv.n_train < MIN_TRAIN_ROWS:
        reasons.append("too few training rows")
    return {"window_start": str(cfg.eval_start), "n": m.get("n"), "brier": m.get("brier"),
            "brier_climatology": m.get("brier_climatology"), "ece": m.get("ece"), "sanity_ok": sane,
            "n_train": mv.n_train, "passed": not reasons, "reason": "; ".join(reasons) or "meets acceptance criteria"}


def promote(mv: ModelVersion, *, actor: str, evidence: dict[str, Any], force_reason: str | None = None) -> None:
    if mv.status != "candidate":
        raise PromotionRefused(f"version is {mv.status}, not a candidate")
    if not evidence.get("passed") and not force_reason:
        raise PromotionRefused(f"acceptance criteria not met: {evidence.get('reason')}")
    current = approved(mv.model_key, mv.horizon)
    now = utcnow()
    if current is not None:
        current.status, current.retired_at = "retired", now
        db.session.flush()
        _event("retirement", "applied", f"superseded by v{mv.version}", mv=current, actor=actor)
    mv.status, mv.approved_at, mv.evaluation = "approved", now, evidence
    _event("promotion", "promoted", force_reason or evidence.get("reason", ""), mv=mv, actor=actor, evidence=evidence)
    db.session.commit()


def reject(mv: ModelVersion, *, actor: str, evidence: dict[str, Any]) -> None:
    mv.status, mv.evaluation = "rejected", evidence
    _event("promotion", "rejected", evidence.get("reason", ""), mv=mv, actor=actor, evidence=evidence)
    db.session.commit()


def rollback(model_key: str, horizon: int, *, actor: str, reason: str) -> ModelVersion:
    """Re-approve the most recently retired version; the current approved version is retired.
    Historical forecasts keep pointing at the versions that produced them."""
    current = approved(model_key, horizon)
    previous = db.session.scalar(select(ModelVersion).where(
        ModelVersion.model_key == model_key, ModelVersion.horizon == horizon, ModelVersion.status == "retired",
        ModelVersion.approved_at.is_not(None)).order_by(ModelVersion.retired_at.desc()).limit(1))
    if previous is None:
        raise PromotionRefused("no previously approved version to roll back to")
    load_artifact(previous)  # verify the artifact still exists and matches its hash
    now = utcnow()
    if current is not None:
        current.status, current.retired_at = "retired", now
        db.session.flush()
    previous.status, previous.approved_at, previous.retired_at = "approved", now, None
    _event("rollback", "applied", reason, mv=previous, actor=actor,
           evidence={"from_version": current.version if current else None, "to_version": previous.version})
    db.session.commit()
    return previous


def set_primary(horizon: int, model_key: str, *, actor: str, evidence: dict[str, Any]) -> None:
    """Record which model's forecast is shown as the primary estimate for a horizon."""
    _event("primary_selected", "applied", f"primary model for h={horizon}: {model_key}", horizon=horizon,
           actor=actor, evidence={"model_key": model_key, **evidence})
    db.session.commit()


def primary_model(horizon: int) -> tuple[str, dict[str, Any]]:
    ev = db.session.scalar(select(ModelEvent).where(ModelEvent.event_type == "primary_selected",
                                                    ModelEvent.horizon == horizon)
                           .order_by(ModelEvent.occurred_at.desc()).limit(1))
    if ev is None:
        return "zero", {"reason": "no selection recorded; no-change baseline by default"}
    return ev.evidence.get("model_key", "zero"), ev.evidence


def _short(v: str) -> str:
    return v if len(v) <= 64 else hashlib.sha256(v.encode()).hexdigest()


def _f(x: float) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 5)
