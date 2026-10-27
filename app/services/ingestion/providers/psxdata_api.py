"""psxdata-api adapter (https://github.com/mtauha/psxdata-api, pinned v0.5.0 / psxdata 1.2.0).

psxdata-api is a self-hosted FastAPI service wrapping the `psxdata` library, which reads the
PSX Data Portal (dps.psx.com.pk). Contract as read from its source at v0.5.0 and verified
live on 2026-10-13:

  Success: {"data": ..., "meta": {"timestamp": ISO, "cached": bool[, "count": int]}}
  Failure: {"error": {"status": int, "code": str, "message": str}}
    429 rate_limited (60 req/min per client) - 502 upstream_data_error / proxy_unreachable
    503 psx_unavailable - 404 not_found - 400/422 bad request
  GET /stocks/{symbol}/historical?start=&end=  -> [{date, open, high, low, close, volume, is_anomaly}]
      newest first; unadjusted; indices (e.g. KSE100) work here too
  GET /stocks/{symbol}/quote -> {symbol, price, change, change_pct, ldcp, pe_ratio, ...}
      404 for indices; no exchange timestamp
  GET /indices -> [index names]

Provider field names stop here: everything returned is a provider-neutral observation.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from app.services.ingestion.providers.base import (
    DailyBarObservation,
    FetchResult,
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
    ProviderUpstreamDataError,
    QuoteObservation,
    RejectedRecord,
)
from app.utils.time import PSX_TZ, psx_local_date, utcnow

log = logging.getLogger(__name__)

SOURCE_ID = "psxdata_api"

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")
CONNECT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_RETRY_AFTER_SECONDS = 60.0
MAX_ABS_VALUE = Decimal("1e13")
MAX_ABS_PERCENT = Decimal("99999")
# Match the numeric(…, 6) / numeric(…, 2) storage scales so a re-fetch of an unchanged value
# compares equal to what was stored (no spurious revisions from rounding).
def time_of_day(raw: str) -> dt_time:
    return dt_time.fromisoformat(raw.strip())


# A listed instrument symbol: letters/digits only (futures carry a "-MON" suffix and are excluded).
LISTING_SYMBOL = re.compile(r"[A-Z0-9]{1,12}")
SIX_DP = Decimal("0.000001")
TWO_DP = Decimal("0.01")

# Module-level so tests can substitute transport and clock without touching global libraries.
_session_factory: Callable[[], requests.Session] = requests.Session
_sleep: Callable[[float], None] = time.sleep


class PsxDataApiClient:
    source_id = SOURCE_ID

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_attempts: int = 3,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] | None = None,
        backoff_base_seconds: float = 2.0,
        session_final_after: dt_time | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Before this PSX-local time, today's bar is an in-progress session: it is skipped (and picked up by
        # the next sync's overlap window once final). None = accept today's bar at any time.
        self._final_after = session_final_after
        self._clock = clock or utcnow
        self._timeout = (CONNECT_TIMEOUT_SECONDS, timeout_seconds)
        self._max_attempts = max_attempts
        self._session = session or _session_factory()
        self._sleep = sleep or (lambda seconds: _sleep(seconds))
        self._backoff_base = backoff_base_seconds

    @classmethod
    def from_config(cls, config: Mapping[str, Any], **kwargs: Any) -> PsxDataApiClient:
        kwargs.setdefault("session_final_after", time_of_day(config.get("PSXDATA_SESSION_FINAL_TIME") or "17:00"))
        return cls(base_url=config["PSXDATA_API_URL"], timeout_seconds=config["PSXDATA_TIMEOUT_SECONDS"],
                   max_attempts=config["PSXDATA_MAX_ATTEMPTS"], **kwargs)

    def final_through(self) -> date:
        """Latest session date whose bar is final (yesterday while today's session may still be trading)."""
        now = self._clock()
        today = psx_local_date(now)
        if self._final_after is not None and now.astimezone(PSX_TZ).time() < self._final_after:
            return today - timedelta(days=1)
        return today

    def __repr__(self) -> str:
        return f"PsxDataApiClient(base_url={self._base_url!r})"

    # -- public -----------------------------------------------------------------------------

    def fetch_daily_bars(self, symbol: str, start: date | None = None, end: date | None = None) -> FetchResult:
        params = {k: v.isoformat() for k, v in (("start", start), ("end", end)) if v is not None}
        body, meta = self._get(f"/stocks/{symbol}/historical", params)
        rows = body.get("data")
        if not isinstance(rows, list):
            raise ProviderResponseError(f"`data` is {type(rows).__name__}, expected a list", metadata=meta)
        final = self.final_through()
        in_progress = [r for r in rows if isinstance(r, dict) and str(r.get("date", ""))[:10] > final.isoformat()
                       and str(r.get("date", ""))[:10] <= psx_local_date(self._clock()).isoformat()]
        if in_progress:
            rows = [r for r in rows if r not in in_progress]
            meta["in_progress_session_skipped"] = [str(r.get("date"))[:10] for r in in_progress]
        observations, rejected = normalize_daily_bars(symbol, rows, today=psx_local_date(self._clock()))
        return FetchResult(observations=observations, rejected=rejected, received=len(rows),
                           provider_cached=_cached(body), metadata=meta)

    def fetch_quote(self, symbol: str) -> FetchResult:
        body, meta = self._get(f"/stocks/{symbol}/quote")
        item = body.get("data")
        try:
            obs = _normalize_quote(symbol, item)
        except _Reject as exc:
            return FetchResult(observations=[], rejected=[RejectedRecord(0, symbol, exc.reason, _truncate(item))],
                               received=1, provider_cached=_cached(body), metadata=meta)
        return FetchResult(observations=[obs], received=1, provider_cached=_cached(body), metadata=meta)

    def fetch_index_names(self) -> list[str]:
        body, meta = self._get("/indices")
        names = body.get("data")
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ProviderResponseError("/indices `data` is not a list of strings", metadata=meta)
        return [n.strip().upper() for n in names]

    def fetch_listing(self) -> dict[str, str]:
        """Listed PSX instruments {symbol: company name} from /eligible-scrips (all of its tables).

        Futures contracts (symbols with a '-' month suffix) are skipped; the first name seen wins.
        """
        body, meta = self._get("/eligible-scrips")
        tables = body.get("data")
        if not isinstance(tables, dict):
            raise ProviderResponseError("/eligible-scrips `data` is not an object", metadata=meta)
        out: dict[str, str] = {}
        for rows in tables.values():
            for row in rows if isinstance(rows, list) else []:
                sym = str(row.get("symbol") or "").strip().upper() if isinstance(row, dict) else ""
                name = str(row.get("name") or "").strip() if isinstance(row, dict) else ""
                if sym and name and LISTING_SYMBOL.fullmatch(sym):
                    out.setdefault(sym, name[:200])
        if not out:
            raise ProviderResponseError("/eligible-scrips returned no symbols", metadata=meta)
        return out

    def health(self) -> bool:
        body, _ = self._get("/health", attempts=1)
        return isinstance(body.get("data"), dict) and body["data"].get("status") == "ok"

    # -- transport ----------------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, str] | None = None, *,
             attempts: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        max_attempts = attempts or self._max_attempts
        meta: dict[str, Any] = {"endpoint": path, "params": params or {}, "attempts": 0}
        last_error: ProviderError | None = None

        for attempt in range(1, max_attempts + 1):
            meta["attempts"] = attempt
            retry_after: float | None = None
            started = time.perf_counter()
            try:
                resp = self._session.get(f"{self._base_url}{path}", params=params, timeout=self._timeout,
                                         headers={"Accept": "application/json"}, allow_redirects=False)
            except requests.Timeout:
                last_error = ProviderUnavailable(f"psxdata-api timed out after {self._timeout[1]}s",
                                                 metadata=dict(meta))
            except requests.ConnectionError:
                last_error = ProviderUnavailable(f"psxdata-api unreachable at {self._base_url}", metadata=dict(meta))
            else:
                meta.update(http_status=resp.status_code, elapsed_ms=int((time.perf_counter() - started) * 1000),
                            response_bytes=len(resp.content))
                try:
                    return self._interpret(resp, meta), meta
                except (ProviderRateLimited, ProviderUnavailable) as exc:
                    last_error = exc
                    retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))

            if attempt == max_attempts:
                break
            delay = retry_after if retry_after is not None else self._backoff_base * (2 ** (attempt - 1))
            if delay > MAX_RETRY_AFTER_SECONDS:
                break
            log.warning("psxdata-api request failed; retrying", extra={
                "source_id": SOURCE_ID, "event_type": "provider_retry", "error_category": last_error.category,
                "details": {"endpoint": path, "attempt": attempt, "delay_seconds": delay}})
            self._sleep(delay)

        assert last_error is not None
        last_error.metadata = dict(meta)
        raise last_error

    @staticmethod
    def _interpret(resp: requests.Response, meta: dict[str, Any]) -> dict[str, Any]:
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise ProviderResponseError("response exceeds size limit", metadata=dict(meta))
        body = _parse_json(resp.content)
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        meta["provider_error_code"] = code
        detail = f"HTTP {resp.status_code}" + (f" {code}: {message}" if code else "")

        if resp.status_code == 429:
            raise ProviderRateLimited(f"rate limited by psxdata-api ({detail})", metadata=dict(meta))
        if resp.status_code in (503, 504) or (resp.status_code == 502 and code == "proxy_unreachable"):
            raise ProviderUnavailable(f"PSX or psxdata-api unavailable ({detail})", metadata=dict(meta))
        if resp.status_code == 502:
            raise ProviderUpstreamDataError(f"psxdata-api rejected PSX data ({detail})", metadata=dict(meta))
        if resp.status_code == 404:
            raise ProviderNotFound(f"not found ({detail})", metadata=dict(meta))
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"psxdata-api server error ({detail})", metadata=dict(meta))
        if resp.status_code != 200:
            raise ProviderResponseError(f"unexpected response ({detail})", metadata=dict(meta))
        if body is _INVALID_JSON or body is None:
            raise ProviderResponseError("response is not valid JSON", metadata=dict(meta))
        if not isinstance(body, dict) or "data" not in body:
            raise ProviderResponseError("response has no `data` field", metadata=dict(meta))
        meta["provider_cached"] = _cached(body)
        return body


_INVALID_JSON = object()


def _parse_json(content: bytes) -> Any:
    if not content:
        return None
    try:
        # Decimal keeps prices exact; NaN/Infinity become non-finite Decimals that validation rejects.
        return json.loads(content, parse_float=Decimal, parse_constant=Decimal)
    except (ValueError, UnicodeDecodeError):
        return _INVALID_JSON


def _cached(body: dict[str, Any]) -> bool | None:
    meta = body.get("meta")
    value = meta.get("cached") if isinstance(meta, dict) else None
    return value if isinstance(value, bool) else None


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value) - utcnow()).total_seconds())
    except (TypeError, ValueError):
        return None


# -- normalisation ------------------------------------------------------------------------------


class _Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def normalize_daily_bars(symbol: str, rows: list[Any], *, today: date) -> tuple[list[DailyBarObservation],
                                                                                list[RejectedRecord]]:
    """Hard failures are rejected; plausible-but-suspect bars are kept with quality flags."""
    observations: list[DailyBarObservation] = []
    rejected: list[RejectedRecord] = []
    seen: set[date] = set()
    for index, row in enumerate(rows):
        try:
            bar = _normalize_bar(symbol, row, today)
        except _Reject as exc:
            rejected.append(RejectedRecord(index, symbol, exc.reason, _truncate(row)))
            continue
        if bar.session_date in seen:
            rejected.append(RejectedRecord(index, symbol, "duplicate session date", _truncate(row)))
            continue
        seen.add(bar.session_date)
        observations.append(bar)
    observations.sort(key=lambda b: b.session_date)
    return observations, rejected


def _normalize_bar(symbol: str, row: Any, today: date) -> DailyBarObservation:
    if not isinstance(row, dict):
        raise _Reject("record is not an object")
    raw_date = row.get("date")
    if not isinstance(raw_date, str):
        raise _Reject("missing date")
    try:
        session_date = date.fromisoformat(raw_date[:10])
    except ValueError:
        raise _Reject(f"invalid date {raw_date[:20]!r}") from None
    if session_date > today:
        raise _Reject("session date is in the future")
    o, h, low, c = (_number(row, k, required=True) for k in ("open", "high", "low", "close"))
    for name, value in (("open", o), ("high", h), ("low", low), ("close", c)):
        if value <= 0:
            raise _Reject(f"{name} is not positive")
        if value >= MAX_ABS_VALUE:
            raise _Reject(f"{name} out of range")
    if h < low:
        raise _Reject("high is below low")
    volume = row.get("volume")
    if isinstance(volume, bool) or not isinstance(volume, int | Decimal):
        raise _Reject("volume is not a number")
    if isinstance(volume, Decimal) and (not volume.is_finite() or volume != volume.to_integral_value()):
        raise _Reject("volume is not a whole number")
    volume = int(volume)
    if volume < 0:
        raise _Reject("volume is negative")

    flags = []
    if row.get("is_anomaly") is True:
        flags.append("provider_anomaly")
    if not (low <= o <= h):
        flags.append("open_outside_range")
    if not (low <= c <= h):
        flags.append("close_outside_range")
    return DailyBarObservation(symbol=symbol, session_date=session_date, open=o.quantize(SIX_DP),
                               high=h.quantize(SIX_DP), low=low.quantize(SIX_DP), close=c.quantize(SIX_DP),
                               volume=volume, quality_flags=tuple(flags))


def _normalize_quote(symbol: str, item: Any) -> QuoteObservation:
    if not isinstance(item, dict):
        raise _Reject("quote is not an object")
    reported = item.get("symbol")
    if isinstance(reported, str) and SYMBOL_RE.match(reported.strip().upper()) and reported.strip().upper() != symbol:
        raise _Reject(f"quote is for {reported!r}, expected {symbol!r}")
    price = _number(item, "price", required=True)
    if price <= 0 or price >= MAX_ABS_VALUE:
        raise _Reject("price is not positive or out of range")
    values = {k: _number(item, k, required=False) for k in
              ("change", "change_pct", "ldcp", "pe_ratio", "dividend_yield", "market_cap", "volume_avg_30d",
               "change_1y_pct")}
    for k in ("change_pct", "change_1y_pct", "dividend_yield"):
        if values[k] is not None and abs(values[k]) >= MAX_ABS_PERCENT:
            raise _Reject(f"{k} out of range")
    scale = {"market_cap": TWO_DP, "volume_avg_30d": TWO_DP}
    values = {k: None if v is None else v.quantize(scale.get(k, SIX_DP)) for k, v in values.items()}
    return QuoteObservation(symbol=symbol, price=price.quantize(SIX_DP), **values)


def _number(item: dict[str, Any], key: str, *, required: bool) -> Decimal | None:
    value = item.get(key)
    if value is None:
        if required:
            raise _Reject(f"missing value for {key}")
        return None
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise _Reject(f"{key} is not a JSON number")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise _Reject(f"{key} is not a valid number") from None
    if not number.is_finite():
        raise _Reject(f"{key} is not finite")
    return number


def _truncate(item: Any) -> Any:
    text = json.dumps(item, default=str)
    return json.loads(text) if len(text) <= 1000 else {"truncated": text[:1000]}
