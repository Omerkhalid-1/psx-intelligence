"""News discovery with a fake provider: storage, relevance, clustering (also after an aborted run)
and daily aggregation. Headlines here are synthetic test data."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models.research import NewsArticle, NewsMention
from app.services.news import ingest
from app.services.news.google_news import NewsProviderError, RawArticle
from app.services.news.relevance import load_news_config

pytestmark = pytest.mark.db

CFG = load_news_config(Path("config/news.yaml"))
T0 = datetime(2025, 3, 3, 6, 0, tzinfo=UTC)


def art(title: str, i: int, hours: int = 0) -> RawArticle:
    return RawArticle(title=title, link=f"https://example.test/{i}", publisher="Example (synthetic)",
                      publisher_url="https://www.dawn.com", published_at=T0 + timedelta(hours=hours),
                      published_precision="exact")


class FakeProvider:
    """Returns `pages` in order, then raises a transient error for every further request."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def search(self, query, *, after=None, before=None):
        self.calls += 1
        if self.pages:
            return self.pages.pop(0)
        raise NewsProviderError("request failed: ConnectTimeout", transient=True)


def _mentions(symbol):
    return db_scalar(select(func.count()).select_from(NewsMention).where(NewsMention.symbol == symbol))


def db_scalar(q):
    from app.extensions import db

    return db.session.scalar(q)


def test_discover_stores_dedupes_and_clusters(db_session):
    page = [art("Meezan Bank posts record profit", 1), art("Meezan Bank posts record profit", 1),
            art("Meezan Bank posts record profits", 2, hours=2)]
    run = ingest.discover(CFG, provider=FakeProvider([page] + [[]] * 50), symbols=("MEBL",))
    assert run.records_stored == 2  # exact duplicate link stored once
    clusters = db_session.scalars(select(NewsArticle.cluster_id)).all()
    assert None not in clusters and len(set(clusters)) == 1  # near-identical titles form one event
    assert _mentions("MEBL") == 2


def test_aborted_run_still_clusters_stored_articles(db_session):
    page = [art("Meezan Bank posts record profit", 1), art("Fauji Fertilizer Company announces payout", 2)]
    run = ingest.discover(CFG, provider=FakeProvider([page]), symbols=("MEBL", "FFC"))
    assert run.status.value == "failed"
    assert "aborted" in str(run.request_metadata["errors"])
    assert db_session.scalar(select(func.count()).select_from(NewsArticle).where(NewsArticle.cluster_id.is_(None))) == 0


def test_unclustered_articles_are_still_counted_as_events(db_session):
    ingest.discover(CFG, provider=FakeProvider([[art("Meezan Bank posts record profit", 1)]] + [[]] * 50),
                    symbols=("MEBL",))
    for m in db_session.scalars(select(NewsMention)).all():
        m.sentiment_model, m.sentiment_score, m.sentiment_confidence = "test_model", 0.8, 0.9
    for a in db_session.scalars(select(NewsArticle)).all():
        a.cluster_id = None  # simulate rows left by an older interrupted run
    db_session.commit()
    df = ingest.mention_frame("MEBL", "test_model")
    # Live (non-backfill) articles become available when first seen, i.e. now.
    seen = df["available_at"].max().tz_convert("Asia/Karachi").date()
    agg = ingest.aggregate_for_session(df, seen + timedelta(days=1), "dedup_mean_v1")
    assert agg["n_events"] == 1 and agg["label"] == "positive"
