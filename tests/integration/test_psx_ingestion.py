"""psxdata ingestion against PostgreSQL with mocked psxdata-api HTTP."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.market_data import (
    DailyBar,
    DataSource,
    IngestionRejection,
    IngestionRun,
    IngestionStatus,
    Instrument,
    QuoteSnapshot,
)
from app.models.operations import JobRun, JobStatus
from app.services.ingestion import psx
from app.services.ingestion.providers.base import DailyBarObservation, FetchResult
from app.utils.time import utcnow
from tests.fakes import INDICES, bar, bars_body, err_body, quote_body

pytestmark = pytest.mark.db

SYMBOLS = ("SYS", "FFC", "MEBL", "MARI", "KSE100")


def _hist(symbol):
    return f"/stocks/{symbol}/historical"


def _quote(symbol):
    return f"/stocks/{symbol}/quote"


def three_bars(base: float):
    return bars_body(bar("2026-10-09", base), bar("2026-10-12", base + 1), bar("2026-10-13", base + 2))


ALL_OK = {"/indices": (200, INDICES), **{_hist(s): (200, three_bars(100 + i * 10)) for i, s in enumerate(SYMBOLS)}}


def _count(db_session, model, *where):
    return db_session.scalar(select(func.count()).select_from(model).where(*where))


def _bars(app, **kw):
    return psx.run_daily_bar_ingestion(app.config, **kw)


# -- daily bars ---------------------------------------------------------------------------------------


def test_backfill_stores_bars_and_classifies_instruments(app, db_session, routed_http):
    adapter = routed_http(ALL_OK)
    run = _bars(app)

    assert run.status == IngestionStatus.SUCCEEDED
    assert (run.records_received, run.records_accepted, run.records_stored) == (15, 15, 15)
    # First sync of each symbol is a backfill: no start parameter (all history).
    hist_urls = [r.url for r in adapter.requests if "/historical" in r.url]
    assert len(hist_urls) == 5 and all("start=" not in u for u in hist_urls)
    # Paced one second apart: index lookup + 5 symbols = 5 pauses before symbol requests.
    assert routed_http.state["paces"] == [1.0] * 5
    kinds = dict(db_session.execute(select(Instrument.symbol, Instrument.instrument_kind)).all())
    assert kinds == {"SYS": "stock", "FFC": "stock", "MEBL": "stock", "MARI": "stock", "KSE100": "index"}
    sys = db_session.scalars(select(DailyBar).where(DailyBar.symbol == "SYS").order_by(DailyBar.session_date)).all()
    assert [b.session_date for b in sys] == [date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)]
    assert sys[-1].close == Decimal("102") and sys[-1].revision == 1 and sys[-1].is_current
    per_symbol = run.request_metadata["symbols"]
    assert per_symbol["SYS"]["inserted"] == 3 and per_symbol["SYS"]["latest_session"] == "2026-10-13"
    source = db_session.get(DataSource, "psxdata_api")
    assert source.data_quality_status == "ok" and source.last_successful_sync is not None


def test_incremental_sync_uses_overlap_and_skips_unchanged(app, db_session, routed_http):
    routed_http(ALL_OK)
    _bars(app)
    adapter = routed_http(ALL_OK)
    run = _bars(app)
    assert run.status == IngestionStatus.SUCCEEDED and run.records_stored == 0
    assert run.request_metadata["symbols"]["SYS"]["unchanged"] == 3
    overlap = app.config["PSXDATA_INCREMENTAL_OVERLAP_DAYS"]
    expected_start = (date(2026, 10, 13) - timedelta(days=overlap)).isoformat()
    assert all(f"start={expected_start}" in r.url for r in adapter.requests if "/historical" in r.url)
    assert _count(db_session, DailyBar) == 15


def test_corrected_bar_creates_revision_and_keeps_history(app, db_session, routed_http):
    routed_http(ALL_OK)
    _bars(app, symbols=("SYS",))
    corrected = bars_body(bar("2026-10-09", 100), bar("2026-10-12", 101), bar("2026-10-13", 105.5))
    routed_http({"/indices": (200, INDICES), _hist("SYS"): (200, corrected)})
    run = _bars(app, symbols=("SYS",))
    assert run.request_metadata["symbols"]["SYS"]["revised"] == 1 and run.records_stored == 1
    versions = db_session.scalars(select(DailyBar).where(DailyBar.symbol == "SYS",
                                                         DailyBar.session_date == date(2026, 10, 13))
                                  .order_by(DailyBar.revision)).all()
    assert [(v.revision, v.is_current, v.close) for v in versions] == [
        (1, False, Decimal("102")), (2, True, Decimal("105.5"))]
    assert versions[0].superseded_at is not None


def test_partial_failure_keeps_other_symbols(app, db_session, routed_http):
    routes = dict(ALL_OK)
    routes[_hist("FFC")] = (502, err_body(502, "upstream_data_error", "PSX data failed validation"))
    routes[_hist("MEBL")] = (200, bars_body(bar("2026-10-13", 50), {"date": "2026-10-12", "close": "bad"}))
    routed_http(routes)
    run = _bars(app)
    assert run.status == IngestionStatus.PARTIALLY_SUCCEEDED
    meta = run.request_metadata["symbols"]
    assert meta["FFC"]["status"] == "failed" and meta["FFC"]["error_category"] == "upstream_data_error"
    assert meta["MEBL"]["rejected"] == 1
    assert "FFC: upstream_data_error" in run.error_summary and "1 record(s) rejected" in run.error_summary
    assert _count(db_session, DailyBar, DailyBar.symbol == "FFC") == 0
    assert _count(db_session, DailyBar, DailyBar.symbol == "SYS") == 3
    assert db_session.scalars(select(IngestionRejection.symbol)).all() == ["MEBL"]
    assert db_session.get(DataSource, "psxdata_api").data_quality_status == "degraded"


def test_service_outage_aborts_remaining_symbols(app, db_session, routed_http):
    routes = {"/indices": (200, INDICES), _hist("SYS"): (503, err_body(503, "psx_unavailable"))}
    adapter = routed_http(routes)
    run = _bars(app)
    # Skipped symbols carry no category, so the run reports the one real cause.
    assert run.status == IngestionStatus.FAILED and run.error_category == "provider_unavailable"
    meta = run.request_metadata["symbols"]
    assert meta["SYS"]["error_category"] == "provider_unavailable"
    assert {meta[s]["status"] for s in ("FFC", "MEBL", "MARI", "KSE100")} == {"skipped"}
    assert len([r for r in adapter.requests if "/historical" in r.url]) == 3  # SYS retried, no others called
    assert db_session.get(DataSource, "psxdata_api").data_quality_status == "failing"


def test_index_lookup_failure_does_not_block_bars(app, db_session, routed_http):
    routes = dict(ALL_OK)
    routes["/indices"] = (500, b"boom")
    routed_http(routes)
    run = _bars(app, symbols=("SYS",))
    assert run.status == IngestionStatus.SUCCEEDED
    assert run.request_metadata["index_lookup"].startswith("failed")
    assert _count(db_session, Instrument) == 0  # unclassified rather than guessed


class _BadProvider:
    source_id = "psxdata_api"

    def fetch_index_names(self):
        return []

    def fetch_daily_bars(self, symbol, start=None, end=None):
        good = DailyBarObservation(symbol, date(2026, 10, 12), *(Decimal("10"),) * 4, volume=1)
        bad = DailyBarObservation(symbol, date(2026, 10, 13), Decimal("10"), Decimal("9"), Decimal("11"),
                                  Decimal("10"), volume=1)  # high < low: the database refuses it
        return FetchResult(observations=[good, bad], received=2)

    def fetch_quote(self, symbol):  # pragma: no cover - unused
        raise NotImplementedError


def test_database_error_rolls_back_only_that_symbol(app, db_session):
    run = psx.run_daily_bar_ingestion(app.config, symbols=("SYS", "FFC"), provider=_BadProvider())
    assert run.status == IngestionStatus.FAILED
    assert {r["error_category"] for r in run.request_metadata["symbols"].values()} == {"database_error"}
    assert _count(db_session, DailyBar) == 0  # the valid 10-12 bar was rolled back with its symbol


def test_concurrent_run_refused_and_abandoned_run_taken_over(app, db_session, routed_http):
    routed_http(ALL_OK)
    psx.ensure_source(app.config)
    blocker = IngestionRun(source_id="psxdata_api", dataset=psx.DATASET_BARS, status=IngestionStatus.RUNNING,
                           started_at=utcnow())
    db_session.add(blocker)
    db_session.commit()
    with pytest.raises(psx.IngestionAlreadyRunning):
        _bars(app)
    blocker.started_at = utcnow() - timedelta(hours=2)
    db_session.commit()
    assert _bars(app, symbols=("SYS",)).status == IngestionStatus.SUCCEEDED
    db_session.expire_all()
    assert db_session.get(IngestionRun, blocker.id).error_category == "abandoned"


# -- quotes -------------------------------------------------------------------------------------------------


def _classify(db_session):
    from app.models.market_data import Instrument

    psx.ensure_source(__import__("flask").current_app.config)
    for s in SYMBOLS:
        db_session.add(Instrument(symbol=s, instrument_kind="index" if s == "KSE100" else "stock",
                                  source_id="psxdata_api"))
    db_session.commit()


QUOTES_OK = {_quote(s): (200, quote_body(s, 100 + i)) for i, s in enumerate(SYMBOLS[:4])}


def test_quotes_stored_and_indices_skipped(app, db_session, routed_http):
    _classify(db_session)
    adapter = routed_http(QUOTES_OK)
    run = psx.run_quote_ingestion(app.config)
    assert run.status == IngestionStatus.SUCCEEDED
    assert run.records_stored == 4 and run.records_filtered == 1
    assert not any("KSE100" in r.url for r in adapter.requests)
    assert run.request_metadata["symbols"]["KSE100"]["status"] == "not_applicable"
    q = db_session.scalar(select(QuoteSnapshot).where(QuoteSnapshot.symbol == "SYS"))
    assert q.price == Decimal("100") and q.change_pct == Decimal("0.5") and q.ldcp is None


def test_unchanged_quotes_are_not_restored(app, db_session, routed_http):
    _classify(db_session)
    routed_http(QUOTES_OK)
    psx.run_quote_ingestion(app.config)
    changed = dict(QUOTES_OK)
    changed[_quote("SYS")] = (200, quote_body("SYS", 101.25, change_pct=1.25))
    routed_http(changed)
    run = psx.run_quote_ingestion(app.config)
    assert run.records_stored == 1
    assert db_session.scalars(select(QuoteSnapshot.price).where(QuoteSnapshot.symbol == "SYS")
                              .order_by(QuoteSnapshot.retrieved_at)).all() == [Decimal("100"), Decimal("101.25")]


def test_quote_not_found_is_a_coverage_note_not_a_failure(app, db_session, routed_http):
    """psxdata-api has no quote for some listed symbols (not in its screener feed): recorded, not failed."""
    _classify(db_session)
    routes = dict(QUOTES_OK)
    routes[_quote("MARI")] = (404, err_body(404, "not_found", "MARI not found"))
    routed_http(routes)
    run = psx.run_quote_ingestion(app.config)
    assert run.status == IngestionStatus.SUCCEEDED
    assert "no live quote available for: MARI" in run.error_summary
    assert run.request_metadata["symbols"]["MARI"]["status"] == "not_available"


def test_quote_failure_other_than_not_found_is_still_partial(app, db_session, routed_http):
    _classify(db_session)
    routes = dict(QUOTES_OK)
    routes[_quote("MARI")] = (502, err_body(502, "upstream_parse_error", "bad PSX data"))
    routed_http(routes)
    run = psx.run_quote_ingestion(app.config)
    assert run.status == IngestionStatus.PARTIALLY_SUCCEEDED and "MARI" in run.error_summary


# -- jobs, scheduler, CLI --------------------------------------------------------------------------------------


def _job(db_session, job_id):
    db_session.expire_all()
    return db_session.get(JobRun, job_id)


def test_bars_job_success_and_subset(app, db_session, routed_http):
    from app.services.orchestration import jobs

    routed_http(ALL_OK)
    result = jobs.submit_job("ingestion.psx_daily_bars", parameters={"symbols": ["SYS"]}, requested_by="test")
    job = _job(db_session, result.job.id)
    assert job.status == JobStatus.SUCCEEDED and job.result_summary["stored"] == 3
    assert db_session.scalar(select(IngestionRun)).job_run_id == job.id


@pytest.mark.parametrize("outcome,category", [
    ((503, err_body(503, "psx_unavailable")), "transient"),
    ((502, err_body(502, "upstream_data_error")), "permanent"),
])
def test_failed_sync_marks_job_failed(app, db_session, routed_http, outcome, category):
    from app.services.orchestration import jobs

    routed_http({"/indices": (200, INDICES), _hist("SYS"): outcome})
    result = jobs.submit_job("ingestion.psx_daily_bars", parameters={"symbols": ["SYS"]}, requested_by="test")
    job = _job(db_session, result.job.id)
    assert job.status == JobStatus.FAILED and job.error_category == category


def test_job_refuses_when_disabled(app, db_session, routed_http, monkeypatch):
    from app.services.orchestration import jobs

    adapter = routed_http(ALL_OK)
    monkeypatch.setitem(app.config, "PSXDATA_ENABLED", False)
    result = jobs.submit_job("ingestion.psx_daily_bars", requested_by="test")
    job = _job(db_session, result.job.id)
    assert job.status == JobStatus.FAILED and "PSXDATA_ENABLED is false" in job.error_message
    assert adapter.requests == []


def test_daily_scheduler_one_job_per_day(app, db_session, routed_http, monkeypatch):
    from app.tasks.ingestion_tasks import schedule_psx_daily_bars

    routed_http(ALL_OK)
    monkeypatch.setitem(app.config, "ENABLE_SCHEDULED_INGESTION", True)
    schedule_psx_daily_bars.apply()
    schedule_psx_daily_bars.apply()  # duplicate beat delivery
    assert _count(db_session, JobRun) == 1
    assert _count(db_session, DailyBar) == 15


@pytest.mark.parametrize("scheduled,inside", [(False, True), (True, False)])
def test_quote_scheduler_respects_flag_and_window(app, db_session, routed_http, monkeypatch, scheduled, inside):
    from app.tasks import ingestion_tasks

    adapter = routed_http(QUOTES_OK)
    monkeypatch.setitem(app.config, "ENABLE_SCHEDULED_INGESTION", scheduled)
    monkeypatch.setattr(ingestion_tasks, "within_window", lambda *_a, **_k: inside)
    ingestion_tasks.schedule_psx_quotes.apply()
    assert _count(db_session, JobRun) == 0 and adapter.requests == []


def test_quote_window_is_psx_weekdays():
    from datetime import datetime

    from app.services.market_data import within_window
    from app.utils.time import PSX_TZ

    assert within_window("09:15-17:00", datetime(2026, 10, 13, 10, 0, tzinfo=PSX_TZ))      # Tuesday
    assert not within_window("09:15-17:00", datetime(2026, 10, 13, 18, 0, tzinfo=PSX_TZ))  # after hours
    assert not within_window("09:15-17:00", datetime(2026, 10, 17, 10, 0, tzinfo=PSX_TZ))  # Saturday


def test_cli_sync(app, db_session, routed_http):
    routed_http(ALL_OK)
    result = app.test_cli_runner().invoke(args=["ingest-psx", "bars", "--symbols", "SYS,FFC", "--sync"])
    assert result.exit_code == 0, result.output
    assert '"status": "succeeded"' in result.output
    assert _count(db_session, DailyBar) == 6
    bad = app.test_cli_runner().invoke(args=["ingest-psx", "bars", "--symbols", "NOPE"])
    assert bad.exit_code != 0 and "not in PSXDATA_SYMBOLS" in bad.output
