"""Data-quality checks over stored market data.

Findings are upserted into `data_quality_issues` keyed by (check, symbol, session_date): reruns do
not duplicate them, and findings that disappear are marked resolved (never deleted), so every
judgement about the data stays auditable.

Severity:
  error   - must not be used as-is (e.g. unexplained corporate-action discontinuity)
  warning - suspicious but possibly genuine (flagged bar, move beyond the daily limit, stale data)
  info    - expected irregularities (no trades on a session, revised bar)
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import select

from app.extensions import db
from app.models.research import CorporateAction, DataQualityIssue
from app.services.research.prices import PriceFrame, load_universe, observed_calendar
from app.utils.time import utcnow

log = logging.getLogger(__name__)

EXTREME_MOVE = 0.105  # beyond PSX's widest daily limit (10%) plus slack, after adjustment
VOLUME_SPIKE_MULTIPLE = 10.0


@dataclass(frozen=True)
class Finding:
    check_name: str
    symbol: str
    session_date: date | None
    severity: str
    message: str
    details: dict[str, Any]


def _d(ts: pd.Timestamp) -> date:
    return ts.date()


def check_frame(pf: PriceFrame, calendar: pd.DatetimeIndex) -> list[Finding]:
    df, s = pf.frame, pf.symbol
    out: list[Finding] = []
    if df.empty:
        return [Finding("no_data", s, None, "error", "no daily bars stored", {})]

    in_range = calendar[(calendar >= df.index[0]) & (calendar <= df.index[-1])]
    for ts in in_range.difference(df.index):
        out.append(Finding("missing_session", s, _d(ts), "info",
                           "no bar on an exchange session (no trades, suspension, or data gap)", {}))
    for ts, row in df[df["quality_flags"].map(bool)].iterrows():
        out.append(Finding("flagged_bar", s, _d(ts), "warning", f"bar flagged: {', '.join(row.quality_flags)}",
                           {"flags": row.quality_flags}))
    for ts in df.index[df["revision"] > 1]:
        out.append(Finding("revised_bar", s, _d(ts), "info", "bar was revised by the source", {}))
    for ts, row in df[df["suspected_gap"] & ~df["unexplained_gap"]].iterrows():
        out.append(Finding(
            "suspected_discontinuity", s, _d(ts), "warning",
            f"overnight gap {row.gap:+.1%} ({row.gap_vs_market:+.1%} vs KSE100) within the daily limit and no "
            "recorded corporate action: a genuine limit move or an unrecorded small bonus; review",
            {"gap": round(row.gap, 4), "gap_vs_market": round(row.gap_vs_market, 4), "return": round(row.ret, 4)}))
    for ts, row in df[df["unexplained_gap"]].iterrows():
        out.append(Finding(
            "unexplained_discontinuity", s, _d(ts), "error",
            f"overnight gap {row.gap:+.1%} ({row.gap_vs_market:+.1%} vs KSE100) with no recorded corporate action; "
            "returns around this date are excluded from modelling until it is explained",
            {"gap": round(row.gap, 4), "gap_vs_market": round(row.gap_vs_market, 4)}))
    if not pf.is_index:
        extreme = df[(df["ret"].abs() > EXTREME_MOVE) & ~df["action_on_date"] & ~df["unexplained_gap"]]
        for ts, row in extreme.iterrows():
            out.append(Finding("extreme_move", s, _d(ts), "warning",
                               f"adjusted daily return {row.ret:+.1%} exceeds the PSX daily limit",
                               {"return": round(row.ret, 4)}))
    median_vol = df["adj_volume"].rolling(60, min_periods=20).median().shift(1)
    spikes = df[(df["adj_volume"] > VOLUME_SPIKE_MULTIPLE * median_vol) & (median_vol > 0)]
    for ts, row in spikes.iterrows():
        out.append(Finding("volume_spike", s, _d(ts), "info",
                           f"volume {row.adj_volume / median_vol[ts]:.0f}x the 60-session median", {}))
    return out


def check_recorded_actions() -> list[Finding]:
    out = []
    for a in db.session.scalars(select(CorporateAction)):
        if a.price_check == "inconsistent":
            out.append(Finding("corporate_action_inconsistent", a.symbol, a.ex_date, "error",
                               f"recorded {a.action_type} does not match prices: {a.price_check_detail}", {}))
        elif a.status != "verified":
            out.append(Finding("corporate_action_unverified", a.symbol, a.ex_date, "warning",
                               f"recorded {a.action_type} is not verified against a cited source", {}))
    return out


def run_quality_checks(config: Mapping[str, Any], frames: dict[str, PriceFrame] | None = None) -> dict[str, Any]:
    symbols = tuple(config["PSXDATA_SYMBOLS"])
    frames = frames or load_universe(symbols)
    calendar = observed_calendar(frames)
    findings = check_recorded_actions()
    for symbol in symbols:
        findings.extend(check_frame(frames[symbol], calendar))
    return persist_findings(findings, scope=set(symbols))


def persist_findings(findings: list[Finding], scope: set[str]) -> dict[str, Any]:
    now = utcnow()
    open_issues = {(i.check_name, i.symbol, i.session_date): i for i in db.session.scalars(
        select(DataQualityIssue).where(DataQualityIssue.resolved_at.is_(None), DataQualityIssue.symbol.in_(scope)))}
    seen = set()
    created = 0
    for f in findings:
        key = (f.check_name, f.symbol, f.session_date)
        seen.add(key)
        issue = open_issues.get(key) or db.session.scalar(select(DataQualityIssue).where(
            DataQualityIssue.check_name == f.check_name, DataQualityIssue.symbol == f.symbol,
            DataQualityIssue.session_date.is_(None) if f.session_date is None
            else DataQualityIssue.session_date == f.session_date))
        if issue is None:
            issue = DataQualityIssue(check_name=f.check_name, symbol=f.symbol, session_date=f.session_date,
                                     severity=f.severity, message=f.message, details=f.details,
                                     last_detected_at=now)
            db.session.add(issue)
            created += 1
        else:
            issue.severity, issue.message, issue.details = f.severity, f.message, f.details
            issue.last_detected_at, issue.resolved_at = now, None
    resolved = 0
    for key, issue in open_issues.items():
        if key not in seen:
            issue.resolved_at = now
            resolved += 1
    db.session.commit()
    counts: dict[str, int] = {}
    for f in findings:
        counts[f"{f.severity}:{f.check_name}"] = counts.get(f"{f.severity}:{f.check_name}", 0) + 1
    summary = {"findings": len(findings), "created": created, "resolved": resolved, "by_check": counts}
    log.info("data quality checks complete", extra={"event_type": "data_quality_checked", "details": summary})
    return summary
