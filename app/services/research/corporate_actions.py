"""Load curated corporate actions (config/corporate_actions.yaml) and verify them against prices."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from app.extensions import db
from app.models.research import CorporateAction
from app.services.audit import record_audit
from app.services.research.prices import check_action_consistency, load_raw_bars

log = logging.getLogger(__name__)

# cash_dividend: an ex-dividend price drop recorded so the quality checks treat it as explained. It never
# adjusts prices (multiplier 1): price history is a price-return series by design.
VALID_TYPES = {"bonus", "split", "consolidation", "cash_dividend"}
VALID_STATUS = {"verified", "unverified"}


class CorporateActionFileError(ValueError):
    pass


def parse_actions_file(path: Path) -> list[dict[str, Any]]:
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise CorporateActionFileError(f"cannot read {path}: {exc}") from exc
    entries = (doc or {}).get("actions") or []
    parsed, seen = [], set()
    for i, e in enumerate(entries):
        where = f"actions[{i}]"
        try:
            symbol = str(e["symbol"]).strip().upper()
            ex_date = e["ex_date"] if isinstance(e["ex_date"], date) else date.fromisoformat(str(e["ex_date"]))
            action_type = str(e["action_type"])
            multiplier = Decimal(str(e["share_multiplier"]))
            dividend = Decimal(str(e["cash_dividend_per_share"])) if e.get("cash_dividend_per_share") else None
            status = str(e.get("status", "unverified"))
            sources = [str(s) for s in e.get("sources") or []]
            evidence = str(e["evidence"]).strip()
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise CorporateActionFileError(f"{where}: invalid or missing field ({exc})") from exc
        if action_type not in VALID_TYPES:
            raise CorporateActionFileError(f"{where}: action_type must be one of {sorted(VALID_TYPES)}")
        if status not in VALID_STATUS:
            raise CorporateActionFileError(f"{where}: status must be one of {sorted(VALID_STATUS)}")
        if multiplier <= 0 or (action_type in {"bonus", "split"} and multiplier <= 1):
            raise CorporateActionFileError(f"{where}: share_multiplier must be > 1 for {action_type}")
        if action_type == "consolidation" and multiplier >= 1:
            raise CorporateActionFileError(f"{where}: consolidation multiplier must be < 1")
        if action_type == "cash_dividend" and (multiplier != 1 or not dividend or dividend <= 0):
            raise CorporateActionFileError(f"{where}: cash_dividend needs share_multiplier 1 and a positive "
                                           "cash_dividend_per_share")
        if status == "verified" and not sources:
            raise CorporateActionFileError(f"{where}: a verified action must cite at least one source")
        key = (symbol, ex_date, action_type)
        if key in seen:
            raise CorporateActionFileError(f"{where}: duplicate {key}")
        seen.add(key)
        parsed.append({"symbol": symbol, "ex_date": ex_date, "action_type": action_type,
                       "share_multiplier": multiplier, "cash_dividend_per_share": dividend, "status": status,
                       "evidence": evidence, "sources": sources})
    return parsed


def sync_corporate_actions(path: Path, *, actor: str) -> dict[str, Any]:
    """Upsert the file into the database (the file is the source of truth) and check each action
    against the stored prices. Actions removed from the file are deleted, with an audit record."""
    entries = parse_actions_file(path)
    existing = {(a.symbol, a.ex_date, a.action_type): a for a in db.session.scalars(select(CorporateAction))}
    wanted = {(e["symbol"], e["ex_date"], e["action_type"]): e for e in entries}
    created = updated = removed = 0
    bars_cache: dict[str, Any] = {}
    for key, entry in wanted.items():
        action = existing.get(key)
        if action is None:
            action = CorporateAction(**entry)
            db.session.add(action)
            created += 1
        else:
            for field, value in entry.items():
                setattr(action, field, value)
            updated += 1
        bars = bars_cache.setdefault(entry["symbol"], load_raw_bars(entry["symbol"]))
        action.price_check, action.price_check_detail = (
            check_action_consistency(bars, action) if not bars.empty else ("no_data", "no bars stored"))
    for key, action in existing.items():
        if key not in wanted:
            db.session.delete(action)
            removed += 1
    summary = {"created": created, "updated": updated, "removed": removed, "file": str(path)}
    record_audit(actor=actor, action="corporate_actions.synced", entity_type="corporate_actions", details=summary)
    db.session.commit()
    inconsistent = db.session.scalars(
        select(CorporateAction).where(CorporateAction.price_check == "inconsistent")).all()
    summary["inconsistent"] = [f"{a.symbol} {a.ex_date}: {a.price_check_detail}" for a in inconsistent]
    log.info("corporate actions synced", extra={"event_type": "corporate_actions_synced", "details": summary})
    return summary
