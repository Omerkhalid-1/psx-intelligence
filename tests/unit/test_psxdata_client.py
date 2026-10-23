"""psxdata-api adapter: error mapping, retries, envelope validation, record normalisation.
All HTTP is served by FakeAdapter; nothing leaves the process."""

import json
from datetime import date
from decimal import Decimal

import pytest
import requests

from app.services.ingestion.providers.base import (
    ProviderNotFound,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
    ProviderUpstreamDataError,
)
from app.services.ingestion.providers.psxdata_api import PsxDataApiClient
from app.services.ingestion.providers.psxdata_api import normalize_daily_bars as _normalize
from tests.fakes import INDICES, bar, bars_body, err_body, fake_session, ok_body, quote_body

TODAY = date(2026, 10, 13)


def normalize(symbol, rows):
    """Feed rows through the same JSON->Decimal parsing the client applies on the wire."""
    return _normalize(symbol, json.loads(json.dumps(rows), parse_float=Decimal), today=TODAY)


def client(*outcomes, max_attempts=3):
    session, adapter = fake_session(*outcomes)
    sleeps: list[float] = []
    c = PsxDataApiClient(base_url="http://psxdata-api.invalid", timeout_seconds=30, max_attempts=max_attempts,
                         session=session, sleep=sleeps.append)
    return c, adapter, sleeps


# -- happy paths --------------------------------------------------------------------------------------


def test_daily_bars_mapping_order_and_params():
    c, adapter, _ = client((200, bars_body(bar("2026-10-12", 113.99, open_=113.9, high=115.5, low=113.9,
                                               volume=1078982), bar("2026-10-13", 113.6), cached=True)))
    result = c.fetch_daily_bars("SYS", start=date(2026, 10, 1))
    assert adapter.requests[0].url == "http://psxdata-api.invalid/stocks/SYS/historical?start=2026-10-01"
    assert result.received == 2 and result.rejected == []
    first, second = result.observations
    assert (first.session_date, second.session_date) == (date(2026, 10, 12), date(2026, 10, 13))  # oldest first
    assert first.close == Decimal("113.99") and first.volume == 1078982 and first.quality_flags == ()
    assert result.provider_cached is True
    assert result.metadata["http_status"] == 200 and result.metadata["attempts"] == 1


def test_quote_mapping_keeps_nulls_as_none():
    c, adapter, _ = client((200, quote_body("SYS", 113.6, change_pct=-0.34)))
    result = c.fetch_quote("SYS")
    q = result.observations[0]
    assert adapter.requests[0].url.endswith("/stocks/SYS/quote")
    assert q.price == Decimal("113.6") and q.change_pct == Decimal("-0.34")
    assert q.ldcp is None and q.change is None and q.market_cap is None
    assert q.pe_ratio == Decimal("10.5")


def test_quote_for_a_different_symbol_is_rejected():
    c, _, _ = client((200, quote_body("FFC", 500)))
    result = c.fetch_quote("SYS")
    assert result.observations == [] and "expected 'SYS'" in result.rejected[0].reason


def test_index_names():
    c, _, _ = client((200, INDICES))
    assert c.fetch_index_names() == ["KSE100", "KSE30", "KMI30"]


# -- error mapping and retries ------------------------------------------------------------------------


def test_429_respects_retry_after_then_succeeds():
    c, adapter, sleeps = client((429, err_body(429, "rate_limited"), {"Retry-After": "3"}), (200, INDICES))
    assert c.fetch_index_names()
    assert sleeps == [3.0] and len(adapter.requests) == 2


def test_persistent_429_is_bounded():
    c, adapter, sleeps = client((429, err_body(429, "rate_limited")))
    with pytest.raises(ProviderRateLimited):
        c.fetch_index_names()
    assert len(adapter.requests) == 3 and sleeps == [2.0, 4.0]


def test_503_psx_unavailable_retries_then_raises():
    c, adapter, _ = client((503, err_body(503, "psx_unavailable", "PSX site unreachable")))
    with pytest.raises(ProviderUnavailable, match="psx_unavailable"):
        c.fetch_daily_bars("SYS")
    assert len(adapter.requests) == 3


def test_502_upstream_data_error_is_not_retried():
    c, adapter, sleeps = client((502, err_body(502, "upstream_data_error", "PSX data failed validation")))
    with pytest.raises(ProviderUpstreamDataError, match="upstream_data_error"):
        c.fetch_daily_bars("SYS")
    assert len(adapter.requests) == 1 and sleeps == []


def test_502_proxy_unreachable_counts_as_unavailable():
    c, _, _ = client((502, err_body(502, "proxy_unreachable")))
    with pytest.raises(ProviderUnavailable):
        c.fetch_daily_bars("SYS")


def test_404_is_not_found():
    c, adapter, _ = client((404, err_body(404, "not_found", "KSE100 not found")))
    with pytest.raises(ProviderNotFound):
        c.fetch_quote("KSE100")
    assert len(adapter.requests) == 1


@pytest.mark.parametrize("exc", [requests.ConnectTimeout("t"), requests.ReadTimeout("t"),
                                 requests.ConnectionError("refused")])
def test_service_down_or_slow_is_unavailable(exc):
    c, adapter, _ = client(exc)
    with pytest.raises(ProviderUnavailable):
        c.fetch_index_names()
    assert len(adapter.requests) == 3


@pytest.mark.parametrize("outcome,match", [
    ((200, b"<html>"), "not valid JSON"),
    ((200, b""), "not valid JSON"),
    ((200, {"meta": {}}), "no `data` field"),
    ((200, ok_body({"date": "2026-10-13"})), "expected a list"),
    ((422, err_body(422, "validation_error")), "unexpected response"),
])
def test_unusable_responses_are_not_retried(outcome, match):
    c, adapter, _ = client(outcome)
    with pytest.raises(ProviderResponseError, match=match):
        c.fetch_daily_bars("SYS")
    assert len(adapter.requests) == 1


# -- bar validation ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("row,reason", [
    ("nope", "not an object"),
    ({**bar("2026-10-13", 10), "date": None}, "missing date"),
    ({**bar("2026-10-13", 10), "date": "13/10/2026"}, "invalid date"),
    (bar("2026-10-14", 10), "in the future"),
    (bar("2026-10-13", 0, open_=1, low=0.5, high=2), "close is not positive"),
    (bar("2026-10-13", -5), "is not positive"),
    ({**bar("2026-10-13", 10), "close": "10"}, "close is not a JSON number"),
    ({**bar("2026-10-13", 10), "close": None}, "missing value for close"),
    (bar("2026-10-13", 10, high=9, low=11), "high is below low"),
    (bar("2026-10-13", 10, volume=-1), "volume is negative"),
    ({**bar("2026-10-13", 10), "volume": 1.5}, "whole number"),
    ({**bar("2026-10-13", 10), "volume": True}, "volume is not a number"),
])
def test_invalid_bars_are_rejected(row, reason):
    observations, rejected = normalize("SYS", [row])
    assert observations == []
    assert reason in rejected[0].reason


def test_non_finite_values_are_rejected():
    body = b'{"data":[{"date":"2026-10-13","open":NaN,"high":2,"low":1,"close":1.5,"volume":1,"is_anomaly":false}],' \
           b'"meta":{"timestamp":"x","cached":false,"count":1}}'
    c, _, _ = client((200, body))
    result = c.fetch_daily_bars("SYS")
    assert result.observations == [] and "not finite" in result.rejected[0].reason


def test_suspect_bars_are_kept_with_flags():
    # Mirrors a real KSE100 bar (2026-10-12): close below the reported low, provider anomaly flag set.
    row = bar("2026-10-12", 165124.41, open_=166149.77, high=166988.32, low=165168.5, anomaly=True)
    observations, rejected = normalize("KSE100", [row])
    assert rejected == []
    assert observations[0].quality_flags == ("provider_anomaly", "close_outside_range")


def test_duplicate_dates_keep_first():
    observations, rejected = normalize("SYS", [bar("2026-10-13", 10), bar("2026-10-13", 11)])
    assert len(observations) == 1 and rejected[0].reason == "duplicate session date"


def test_values_are_quantised_to_storage_scale():
    observations, _ = normalize("SYS", [bar("2026-10-13", 10.1234567, open_=10, high=11, low=9)])
    assert observations[0].close == Decimal("10.123457")
