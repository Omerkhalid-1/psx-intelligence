"""Pipeline orchestration: ingestion runs before the new-session check, and source refresh steps
(bars, news) still run when there is no new session. Step bodies are fakes."""

import pytest

from app.services import pipeline as pl

pytestmark = pytest.mark.db


def _fake_steps(order, *, new_session: bool, fail: set[str] = frozenset()):
    def make(name):
        def step(ctx):
            order.append(name)
            if name in fail:
                raise RuntimeError(f"{name} broke")
            if name == "market_session":
                ctx["session"] = None
                if not new_session:
                    raise pl.StepSkipped("no new session since 2026-10-13")
            return {"ok": True}
        return step

    return lambda config, skip_ingestion: {n: make(n) for n in pl.DEPENDENCIES}


def _statuses(run):
    return {s["step"]: s["status"] for s in run.steps}


def test_ingestion_runs_before_session_check(db_session, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(pl, "_steps", _fake_steps(order, new_session=True))
    run = pl.run_daily_pipeline({})
    assert order[:2] == ["market_data", "market_session"]
    assert run.status == "succeeded" and set(_statuses(run).values()) == {"succeeded"}


def test_no_new_session_still_refreshes_news(db_session, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(pl, "_steps", _fake_steps(order, new_session=False))
    run = pl.run_daily_pipeline({})
    assert order == ["market_data", "market_session", "news"]
    st = _statuses(run)
    assert st["features"] == "skipped" and st["forecasts"] == "skipped" and st["news"] == "succeeded"
    assert run.status == "skipped"


def test_ingestion_failure_blocks_session_work_but_not_news(db_session, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(pl, "_steps", _fake_steps(order, new_session=True, fail={"market_data"}))
    run = pl.run_daily_pipeline({})
    st = _statuses(run)
    assert st["market_data"] == "failed" and st["market_session"] == "blocked" and st["forecasts"] == "blocked"
    assert st["news"] == "succeeded" and run.status == "partial"


def test_news_failure_without_new_session_is_partial(db_session, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(pl, "_steps", _fake_steps(order, new_session=False, fail={"news"}))
    assert pl.run_daily_pipeline({}).status == "partial"
