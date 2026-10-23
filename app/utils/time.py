"""Time helpers. Internally all instants are timezone-aware UTC; PSX session dates are
exchange-local calendar dates (Asia/Karachi) and are stored as plain `date` values."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

PSX_TZ = ZoneInfo("Asia/Karachi")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Refusing to convert a naive datetime; attach its original timezone first")
    return value.astimezone(UTC)


def psx_local_date(instant: datetime) -> date:
    """The PSX calendar date on which a UTC instant falls."""
    return to_utc(instant).astimezone(PSX_TZ).date()
