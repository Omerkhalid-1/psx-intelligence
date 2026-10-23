"""Provider-neutral types. Code outside `providers/` depends only on these, never on a
provider's wire format, so a provider can be swapped without touching the ingestion
services, API or dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True)
class DailyBarObservation:
    symbol: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuoteObservation:
    symbol: str
    price: Decimal
    change: Decimal | None = None
    change_pct: Decimal | None = None  # percent units (-0.34 means -0.34%)
    ldcp: Decimal | None = None
    pe_ratio: Decimal | None = None
    dividend_yield: Decimal | None = None
    market_cap: Decimal | None = None
    volume_avg_30d: Decimal | None = None
    change_1y_pct: Decimal | None = None


@dataclass(frozen=True)
class RejectedRecord:
    index: int
    symbol: str | None
    reason: str
    raw: Any


@dataclass
class FetchResult:
    """One provider response: validated observations plus what was rejected and why."""

    observations: list[Any]
    rejected: list[RejectedRecord] = field(default_factory=list)
    received: int = 0
    provider_cached: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)  # safe request facts only


class ProviderError(Exception):
    """Base class. `transient` errors may succeed on a later attempt."""

    category = "provider_error"
    transient = False

    def __init__(self, message: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.metadata: dict[str, Any] = metadata or {}


class ProviderNotFound(ProviderError):
    """The provider has no data for the requested symbol."""

    category = "not_found"


class ProviderRateLimited(ProviderError):
    category = "rate_limited"
    transient = True


class ProviderUnavailable(ProviderError):
    """Service unreachable, timeouts, or the upstream site being down."""

    category = "provider_unavailable"
    transient = True


class ProviderUpstreamDataError(ProviderError):
    """The provider rejected the upstream data it scraped (failed its own validation)."""

    category = "upstream_data_error"


class ProviderResponseError(ProviderError):
    """The provider answered, but not with a usable payload (bad JSON, wrong shape)."""

    category = "invalid_response"


class MarketDataProvider(Protocol):
    source_id: str

    def fetch_daily_bars(self, symbol: str, start: date | None = None, end: date | None = None) -> FetchResult: ...

    def fetch_quote(self, symbol: str) -> FetchResult: ...

    def fetch_index_names(self) -> list[str]: ...
