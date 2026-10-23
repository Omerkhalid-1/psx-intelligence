"""Shared fixtures.

Database-backed tests run against the PostgreSQL database in TEST_DATABASE_URL. The schema
is built by running the real Alembic migrations (so migration drift is caught), and every
table is truncated between tests. Tests that need the database are skipped with a clear
reason when TEST_DATABASE_URL is not set.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.config import TestingConfig

HAS_TEST_DB = bool(os.environ.get("TEST_DATABASE_URL"))


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "db" in item.keywords and not HAS_TEST_DB:
            item.add_marker(pytest.mark.skip(reason="TEST_DATABASE_URL not set"))


@pytest.fixture(scope="session")
def app():
    if not HAS_TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    from flask_migrate import upgrade

    from app import create_app

    application = create_app(TestingConfig())
    with application.app_context():
        from app.extensions import db

        # Start from an empty schema every session, then apply the real migrations.
        db.session.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        db.session.commit()
        upgrade()
    yield application


@pytest.fixture()
def db_session(app):
    from app.extensions import db

    ctx = app.app_context()
    ctx.push()
    yield db.session
    db.session.rollback()
    tables = [t.name for t in reversed(db.metadata.sorted_tables)]
    if tables:
        db.session.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        db.session.commit()
    db.session.remove()
    ctx.pop()


@pytest.fixture()
def client(app, db_session):
    return app.test_client()


@pytest.fixture()
def admin_headers(app):
    return {"Authorization": f"Bearer {app.config['ADMIN_API_TOKEN']}"}


@pytest.fixture()
def provider_http(monkeypatch):
    """Route every psxdata-api request in this test to a FakeAdapter. Retry backoff and request
    pacing are recorded instead of slept."""
    from app.services.ingestion import psx
    from app.services.ingestion.providers import psxdata_api
    from tests.fakes import FakeAdapter, fake_session

    state: dict = {"adapter": None, "sleeps": [], "paces": []}

    def install(*outcomes) -> FakeAdapter:
        session, adapter = fake_session(*outcomes)
        state["adapter"] = adapter
        monkeypatch.setattr(psxdata_api, "_session_factory", lambda: session)
        return adapter

    monkeypatch.setattr(psxdata_api, "_sleep", state["sleeps"].append)
    monkeypatch.setattr(psx, "_pace", state["paces"].append)
    install.state = state
    return install


@pytest.fixture()
def routed_http(provider_http, monkeypatch):
    """Like provider_http, but responds by URL path: routes = {path: outcome or [outcomes]}."""
    import requests

    from app.services.ingestion.providers import psxdata_api
    from tests.fakes import RoutedAdapter

    def install(routes: dict) -> RoutedAdapter:
        adapter = RoutedAdapter(routes)
        provider_http.state["adapter"] = adapter
        session = requests.Session()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        monkeypatch.setattr(psxdata_api, "_session_factory", lambda: session)
        return adapter

    install.state = provider_http.state
    return install


def pytest_runtest_setup(item):
    if "external" in item.keywords and os.environ.get("RUN_EXTERNAL_TESTS") != "1":
        pytest.skip("external test: set RUN_EXTERNAL_TESTS=1 (needs a running psxdata-api service) to run")
