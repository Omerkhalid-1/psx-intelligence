"""Opt-in contract test against a running psxdata-api service. Never part of the normal run.

    services/psxdata-api/run.sh &
    RUN_EXTERNAL_TESTS=1 .venv/bin/pytest -m external -s

Fails loudly if psxdata-api's response format changes (the adapter would then reject data).
Makes 4 requests, writes nothing to any database. Run it after bumping the pinned version.
"""

import os
from datetime import date, timedelta

import pytest

from app.services.ingestion.providers.psxdata_api import PsxDataApiClient

pytestmark = pytest.mark.external

URL = os.environ.get("PSXDATA_API_URL", "http://127.0.0.1:8100")


@pytest.fixture(scope="module")
def client():
    c = PsxDataApiClient(base_url=URL, timeout_seconds=120, max_attempts=1)
    try:
        assert c.health()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"psxdata-api not reachable at {URL}: {exc}")
    return c


def test_indices_contract(client):
    names = client.fetch_index_names()
    assert "KSE100" in names


def test_daily_bars_contract(client):
    start = date.today() - timedelta(days=30)
    result = client.fetch_daily_bars("SYS", start=start)
    assert result.received > 0, "no bars returned for the last 30 days"
    assert not result.rejected, f"adapter rejected live rows: {[r.reason for r in result.rejected][:5]}"
    assert result.provider_cached is not None, "meta.cached missing"
    latest = result.observations[-1]
    print(f"\nSYS latest {latest.session_date} close={latest.close} vol={latest.volume} "
          f"cached={result.provider_cached} n={result.received}")


def test_quote_contract(client):
    result = client.fetch_quote("SYS")
    assert result.observations, f"quote rejected: {result.rejected}"
    print(f"\nSYS quote price={result.observations[0].price} change_pct={result.observations[0].change_pct}")
