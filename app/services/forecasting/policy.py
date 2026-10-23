"""Retraining policy: decides each session whether candidate training is warranted.

Every new session is EVALUATED; training happens only when one of these holds:
  * scheduled: >= RETRAIN_EVERY_SESSIONS sessions since the last training of that horizon AND at
    least MIN_NEW_LABELS newly matured labels;
  * deterioration: rolling MSE of the approved primary-candidate models on the last
    DETERIORATION_WINDOW matured live forecasts exceeds DETERIORATION_RATIO x their walk-forward
    holdout MSE (with at least DETERIORATION_MIN outcomes);
  * regime change: KSE100 20-session volatility moves into a different tercile of its 3-year
    distribution than at the last training.
The decision (applied / skipped) and its evidence are recorded in model_events.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import numpy as np
from sqlalchemy import select

from app.extensions import db
from app.models.forecasting import ModelEvent
from app.services.forecasting import registry
from app.services.forecasting.dataset import Panel
from app.services.forecasting.live import live_performance

RETRAIN_EVERY_SESSIONS = 21
MIN_NEW_LABELS = 21
DETERIORATION_WINDOW = 60
DETERIORATION_MIN = 40
DETERIORATION_RATIO = 1.25


def _tercile(panel: Panel, d: date) -> int | None:
    k = panel.data[panel.data["symbol"] == "KSE100"].set_index("date")["mkt_vol20"].dropna()
    k = k[k.index <= np.datetime64(d)]
    if len(k) < 250:
        return None
    hist = k.iloc[-750:]
    lo, hi = hist.quantile([1 / 3, 2 / 3])
    v = k.iloc[-1]
    return 0 if v <= lo else 2 if v >= hi else 1


def decide(config: Mapping[str, Any], panel: Panel, session: date) -> dict[int, dict[str, Any]]:
    decisions = {}
    exp = registry.latest_experiment("tech_walkforward_v1")
    for h in config["FORECAST_HORIZONS"]:
        last_train = db.session.scalar(select(ModelEvent).where(ModelEvent.event_type == "training",
                                                                ModelEvent.decision == "applied",
                                                                ModelEvent.horizon == h)
                                       .order_by(ModelEvent.occurred_at.desc()).limit(1))
        reasons, evidence = [], {}
        if last_train is None:
            reasons.append("no trained model for this horizon")
        else:
            trained_on = last_train.occurred_at.date()
            sessions_since = int((panel.calendar > np.datetime64(trained_on)).sum())
            evidence["sessions_since_training"] = sessions_since
            if sessions_since >= max(RETRAIN_EVERY_SESSIONS, MIN_NEW_LABELS):
                reasons.append(f"{sessions_since} sessions since last training (>= {RETRAIN_EVERY_SESSIONS})")
            t_then, t_now = _tercile(panel, trained_on), _tercile(panel, session)
            evidence["vol_tercile_then_now"] = [t_then, t_now]
            if t_then is not None and t_now is not None and t_then != t_now:
                reasons.append(f"market volatility regime changed (tercile {t_then} -> {t_now})")
        perf = live_performance(h)
        if not perf.empty and exp is not None:
            for key in ("ridge_tech", "hgb_tech", "rf_tech"):
                recent = perf[perf["model_key"] == key].sort_values("target_date").tail(DETERIORATION_WINDOW)
                ref = next((r for r in exp.results["by_model"] if r["model"] == key and r["horizon"] == h
                            and r["split"] == "holdout"), None)
                if len(recent) >= DETERIORATION_MIN and ref and ref.get("rmse"):
                    ratio = float((recent["error"] ** 2).mean()) / ref["rmse"] ** 2
                    evidence[f"{key}_live_vs_backtest_mse"] = round(ratio, 3)
                    if ratio > DETERIORATION_RATIO:
                        reasons.append(f"{key} live MSE {ratio:.2f}x its walk-forward holdout MSE")
        decision = "applied" if reasons else "skipped"
        db.session.add(ModelEvent(event_type="retrain_check", decision=decision, horizon=h, session_date=session,
                                  reason="; ".join(reasons) or "no retraining condition met", evidence=evidence,
                                  actor="pipeline"))
        decisions[h] = {"retrain": bool(reasons), "reasons": reasons, "evidence": evidence}
    db.session.commit()
    return decisions
