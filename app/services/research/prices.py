"""Price frames: raw observed bars + split/bonus adjustment + corporate-action diagnostics.

Raw bars in `daily_bars` are never modified. Adjustment is applied at read time:

    adj_price(t) = raw_price(t) / prod(share_multiplier of actions with ex_date > t)
    adj_volume(t) = raw_volume(t) * prod(...)

so every price is expressed in today's share units and returns across bonus issues and splits
are economically comparable. Cash dividends are NOT adjusted (targets are price returns).

Discontinuity detection (never used to adjust, only to flag), on dates with no recorded action:
  * suspected_gap: the stock's open gaps down more than 7% vs the previous close while KSE100's
    own gap was at least 6 points smaller. A small bonus issue and a limit-down day look alike,
    so this is a WARNING for human review; the data is still used.
  * unexplained_gap: additionally the close-to-close return is beyond any PSX daily price limit
    (|ret| > 10.5%), which ordinary trading cannot produce. This is an ERROR: rows within
    UNEXPLAINED_EXCLUSION_SESSIONS after it, and labels spanning it, are excluded from modelling.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.extensions import db
from app.models.market_data import DailyBar, Instrument
from app.models.research import CorporateAction
from app.services.ingestion.providers.psxdata_api import SOURCE_ID

BENCHMARK = "KSE100"
GAP_THRESHOLD = -0.07
MARKET_MOVE_TOLERANCE = 0.06  # stock gap must exceed the market's own gap by this much
BEYOND_LIMIT = 0.105  # no ordinary PSX session can move more than the 10% limit (+ slack)
CONSISTENCY_TOLERANCE = 0.105  # adjusted gap on an ex_date must be within the daily limit (+ slack)
UNEXPLAINED_EXCLUSION_SESSIONS = 60  # rows within this many sessions after an unexplained gap


@dataclass(frozen=True)
class PriceFrame:
    symbol: str
    is_index: bool
    frame: pd.DataFrame  # indexed by session_date (Timestamp); see load_price_frame for columns
    data_version: str

    @property
    def latest_session(self) -> date | None:
        return None if self.frame.empty else self.frame.index[-1].date()


def load_raw_bars(symbol: str) -> pd.DataFrame:
    rows = db.session.execute(
        select(DailyBar.session_date, DailyBar.open, DailyBar.high, DailyBar.low, DailyBar.close, DailyBar.volume,
               DailyBar.quality_flags, DailyBar.revision)
        .where(DailyBar.source_id == SOURCE_ID, DailyBar.symbol == symbol, DailyBar.is_current)
        .order_by(DailyBar.session_date)).all()
    df = pd.DataFrame(rows, columns=["session_date", "open", "high", "low", "close", "volume", "quality_flags",
                                     "revision"])
    if df.empty:
        return df
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["session_date"] = pd.to_datetime(df["session_date"])
    return df.set_index("session_date")


def load_actions(symbol: str) -> list[CorporateAction]:
    return list(db.session.scalars(select(CorporateAction).where(CorporateAction.symbol == symbol)
                                   .order_by(CorporateAction.ex_date)))


def adjustment_factors(index: pd.DatetimeIndex, actions: list[CorporateAction]) -> pd.Series:
    """Cumulative share multiplier applying to each date (actions with ex_date strictly later)."""
    factor = pd.Series(1.0, index=index)
    for action in actions:
        factor[index < pd.Timestamp(action.ex_date)] *= float(action.share_multiplier)
    return factor


def load_price_frame(symbol: str, benchmark: pd.DataFrame | None = None) -> PriceFrame:
    """Columns: raw open/high/low/close/volume, adj_* versions, adj_factor, ret (adjusted simple
    daily return), quality_flags, gap, gap_vs_market, action_on_date, suspected_gap,
    unexplained_gap, near_unexplained_gap."""
    raw = load_raw_bars(symbol)
    kind = db.session.scalar(select(Instrument.instrument_kind).where(Instrument.symbol == symbol))
    is_index = kind == "index" or symbol == BENCHMARK
    if raw.empty:
        return PriceFrame(symbol, is_index, raw, "empty")
    return price_frame_from_raw(symbol, raw, load_actions(symbol), is_index, benchmark)


def price_frame_from_raw(symbol: str, raw: pd.DataFrame, actions: list, is_index: bool,  # noqa: ANN001
                         benchmark: pd.DataFrame | None = None) -> PriceFrame:
    """Pure construction of a PriceFrame from raw bars and recorded actions (no database access)."""
    df = raw.copy()
    df["adj_factor"] = adjustment_factors(df.index, actions)
    for col in ("open", "high", "low", "close"):
        df[f"adj_{col}"] = df[col] / df["adj_factor"]
    df["adj_volume"] = df["volume"] * df["adj_factor"]
    df["ret"] = df["adj_close"].pct_change()
    df["gap"] = df["open"] / df["close"].shift(1) - 1  # raw overnight gap
    ex_dates = {pd.Timestamp(a.ex_date) for a in actions}
    df["action_on_date"] = df.index.isin(list(ex_dates))
    if is_index:
        df["gap_vs_market"] = 0.0
        df["suspected_gap"] = False
        df["unexplained_gap"] = False
    else:
        market_gap = (benchmark["open"] / benchmark["close"].shift(1) - 1) if benchmark is not None else None
        df["gap_vs_market"] = df["gap"] - (market_gap.reindex(df.index).fillna(0.0) if market_gap is not None else 0.0)
        suspicious = (df["gap"] < GAP_THRESHOLD) & (df["gap_vs_market"] < -MARKET_MOVE_TOLERANCE)
        df["suspected_gap"] = suspicious & ~df["action_on_date"]
        df["unexplained_gap"] = df["suspected_gap"] & (df["ret"].abs() > BEYOND_LIMIT)
    df["near_unexplained_gap"] = _near(df["unexplained_gap"], UNEXPLAINED_EXCLUSION_SESSIONS)
    return PriceFrame(symbol, is_index, df, _data_version(raw, actions))


def _near(events: pd.Series, sessions: int) -> pd.Series:
    """True for rows within `sessions` rows after (and including) any event."""
    if not events.any():
        return pd.Series(False, index=events.index)
    counter = events.astype(int).rolling(sessions, min_periods=1).max()
    return counter.astype(bool)


def _data_version(raw: pd.DataFrame, actions: list[CorporateAction]) -> str:
    h = hashlib.sha256()
    h.update(pd.util.hash_pandas_object(raw[["open", "high", "low", "close", "volume"]], index=True).values.tobytes())
    for a in actions:
        h.update(f"{a.ex_date}|{a.action_type}|{a.share_multiplier}".encode())
    return h.hexdigest()[:16]


def load_universe(symbols: tuple[str, ...]) -> dict[str, PriceFrame]:
    """Load all symbols with the benchmark used for discontinuity checks and relative features."""
    bench_raw = load_raw_bars(BENCHMARK)
    frames = {}
    for symbol in symbols:
        frames[symbol] = load_price_frame(symbol, bench_raw if not bench_raw.empty else None)
    if BENCHMARK not in frames and not bench_raw.empty:
        frames[BENCHMARK] = load_price_frame(BENCHMARK)
    return frames


def observed_calendar(frames: dict[str, PriceFrame]) -> pd.DatetimeIndex:
    """Exchange sessions as observed in the benchmark's bars (the index prints every session)."""
    bench = frames.get(BENCHMARK)
    if bench is None or bench.frame.empty:
        return pd.DatetimeIndex([])
    return bench.frame.index


def check_action_consistency(frame: pd.DataFrame, action: CorporateAction) -> tuple[str, str]:
    """Is a recorded action consistent with the observed prices on its ex_date?"""
    ts = pd.Timestamp(action.ex_date)
    if ts not in frame.index:
        return "no_data", f"no bar on ex_date {action.ex_date}"
    pos = frame.index.get_loc(ts)
    if pos == 0:
        return "no_data", "no previous bar"
    prev_close = frame["close"].iloc[pos - 1]
    open_ = frame["open"].iloc[pos]
    # Ex-entitlement reference price = (prev close - cash dividend) / multiplier, so a consistent
    # entry leaves an ordinary-sized gap once both are accounted for.
    dividend = float(getattr(action, "cash_dividend_per_share", None) or 0)
    adjusted_gap = (open_ * float(action.share_multiplier) + dividend) / prev_close - 1
    detail = (f"prev close {prev_close:.2f}, open {open_:.2f}, multiplier {action.share_multiplier}"
              + (f", cash dividend {dividend:g}" if dividend else "") + f": adjusted gap {adjusted_gap:+.2%}")
    if abs(adjusted_gap) <= CONSISTENCY_TOLERANCE:
        return "consistent", detail
    return "inconsistent", detail + f" exceeds ±{CONSISTENCY_TOLERANCE:.1%}"


def as_decimal(value: float) -> Decimal:
    return Decimal(str(round(value, 6)))


def safe_float(value: float | None) -> float | None:
    if value is None or (isinstance(value, float) and (np.isnan(value) or np.isinf(value))):
        return None
    return float(value)
