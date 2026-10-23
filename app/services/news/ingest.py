"""News discovery, relevance, clustering, scoring and daily aggregation.

Timing / leakage rules (see docs/news_sentiment.md):
  * live article (seen by our poller): available_at = first_seen_at (provably available then)
  * backfilled, exact publication time: available_at = published_at
  * backfilled, date-only publication: available_at = 23:59:59 PKT of that date (next session at best)
  * unknown publication time: available_at = first_seen_at (never earlier than we saw it)
A forecast with origin session t uses only articles with available_at <= t 18:00 PKT (the time the
daily pipeline runs, after the 15:30 close).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.extensions import db
from app.models.market_data import DataSource, IngestionRun, IngestionStatus
from app.models.research import NewsArticle, NewsMention, NewsSentimentDaily
from app.services.news.google_news import PROVIDER_ID, GoogleNewsRss, NewsProviderError, RawArticle, domain_of
from app.services.news.relevance import (
    MARKET,
    RELEVANCE_THRESHOLD,
    NewsConfig,
    event_category,
    normalize_title,
    relevance,
    title_similarity,
)
from app.services.news.sentiment import SentimentModel, unsupported_language
from app.utils.logging import redact
from app.utils.time import PSX_TZ, utcnow

log = logging.getLogger(__name__)

DATASET = "news"
CLUSTER_SIMILARITY = 0.6
CLUSTER_WINDOW = timedelta(days=3)
FORECAST_CUTOFF_PSX = time(18, 0)
AGG_WINDOW_DAYS = 7
RECENCY_HALF_LIFE_DAYS = 2.0
AGG_METHODS = ("dedup_mean_v1", "weighted_v1")


def ensure_news_source() -> DataSource:
    source = db.session.get(DataSource, PROVIDER_ID)
    if source is None:
        source = DataSource(source_id=PROVIDER_ID, data_quality_status="unknown")
        db.session.add(source)
    source.source_name = "Google News RSS (discovery only: headlines, publisher, date)"
    source.dataset_type = "news_headlines"
    source.access_method = "public_rss"
    source.terms_reference = "Google News RSS; private research use only, not cleared for redistribution"
    source.license_status = "personal_research_use"
    source.enabled = True
    source.refresh_frequency = "daily before forecasts"
    return source


def available_at(raw: RawArticle, first_seen: datetime, backfilled: bool) -> datetime:
    if raw.published_at is None:
        return first_seen
    if not backfilled:
        return max(first_seen, raw.published_at)
    if raw.published_precision == "date_only":
        local_day = raw.published_at.astimezone(PSX_TZ).date()
        return datetime.combine(local_day, time(23, 59, 59), tzinfo=PSX_TZ)
    return raw.published_at


@dataclass
class DiscoveryStats:
    requests: int = 0
    failed_requests: int = 0
    items: int = 0
    new_articles: int = 0
    duplicates: int = 0
    mentions: int = 0


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        out.append((cur, min(nxt, end + timedelta(days=1))))
        cur = nxt
    return out


def discover(news_cfg: NewsConfig, *, provider: GoogleNewsRss | None = None, symbols: tuple[str, ...] | None = None,
             backfill_from: date | None = None, recent_days: int = 14, job_run_id: uuid.UUID | None = None
             ) -> IngestionRun:
    """Search configured queries; store new articles and their per-entity mentions (unscored)."""
    provider = provider or GoogleNewsRss(timeout=news_cfg.provider.get("timeout_seconds", 20),
                                         min_interval=news_cfg.provider.get("min_request_interval_seconds", 3.0))
    entities = [e for s, e in news_cfg.entities.items() if symbols is None or s in symbols or s == MARKET]
    today = utcnow().astimezone(PSX_TZ).date()
    windows = month_windows(backfill_from, today) if backfill_from else [(today - timedelta(days=recent_days), None)]
    backfilled = backfill_from is not None
    ensure_news_source()
    run = IngestionRun(id=uuid.uuid4(), source_id=PROVIDER_ID, dataset=DATASET, job_run_id=job_run_id,
                       status=IngestionStatus.RUNNING, started_at=utcnow(),
                       request_metadata={"mode": "backfill" if backfilled else "recent",
                                         "from": windows[0][0].isoformat(), "windows": len(windows),
                                         "entities": [e.symbol for e in entities]})
    db.session.add(run)
    db.session.commit()

    stats, errors = DiscoveryStats(), []
    for entity in entities:
        # Backfills use only each entity's primary query to keep request volume modest.
        for query in (entity.queries[:1] if backfilled else entity.queries):
            for after, before in windows:
                stats.requests += 1
                try:
                    items = provider.search(query, after=after, before=before)
                except NewsProviderError as exc:
                    stats.failed_requests += 1
                    errors.append(f"{query} [{after}]: {exc}")
                    if exc.transient and stats.failed_requests >= 5:
                        errors.append("aborted after repeated transient failures")
                        _cluster_unclustered()  # articles stored before the abort still need clusters
                        db.session.commit()
                        return _finish(run.id, stats, errors, aborted=True)
                    continue
                stats.items += len(items)
                _store_items(items, query, backfilled, news_cfg, stats)
                db.session.commit()
    _cluster_unclustered()
    db.session.commit()
    return _finish(run.id, stats, errors, aborted=False)


def _store_items(items: list[RawArticle], query: str, backfilled: bool, news_cfg: NewsConfig,
                 stats: DiscoveryStats) -> None:
    now = utcnow()
    for raw in items:
        url_hash = hashlib.sha256(raw.link.encode()).hexdigest()
        title_norm = normalize_title(raw.title)
        publisher_domain = domain_of(raw.publisher_url)
        existing = db.session.scalar(select(NewsArticle).where(
            (NewsArticle.url_hash == url_hash)
            | ((NewsArticle.title_normalized == title_norm) & (NewsArticle.publisher_domain == publisher_domain))))
        if existing is not None:
            stats.duplicates += 1
            article = existing
        else:
            article = NewsArticle(provider=PROVIDER_ID, url=raw.link, url_hash=url_hash, title=raw.title[:1000],
                                  title_normalized=title_norm, publisher=raw.publisher,
                                  publisher_domain=publisher_domain,
                                  published_at=raw.published_at, published_precision=raw.published_precision,
                                  first_seen_at=now, available_at=available_at(raw, now, backfilled),
                                  backfilled=backfilled, language="ur" if unsupported_language(raw.title) else "en",
                                  query=query)
            db.session.add(article)
            db.session.flush()
            stats.new_articles += 1
        for symbol, entity in news_cfg.entities.items():
            rel, reason = relevance(article.title, entity, article.publisher_domain, news_cfg.pk_context)
            if rel <= 0:
                continue
            mention = db.session.scalar(select(NewsMention).where(NewsMention.article_id == article.id,
                                                                  NewsMention.symbol == symbol))
            if mention is None:
                db.session.add(NewsMention(article_id=article.id, symbol=symbol, relevance=rel, relevance_reason=reason,
                                           event_category=event_category(article.title, news_cfg.event_keywords)))
                stats.mentions += 1


def _cluster_unclustered() -> None:
    """Assign cluster_id: near-identical titles within CLUSTER_WINDOW share the earliest article's id.
    Covers every unclustered article, so articles left by an earlier interrupted run are picked up."""
    new = db.session.scalars(
        select(NewsArticle).where(NewsArticle.cluster_id.is_(None)).order_by(NewsArticle.published_at)).all()
    for art in new:
        if art.published_at is None:
            art.cluster_id = art.id
            continue
        nearby = db.session.scalars(select(NewsArticle).where(
            NewsArticle.id != art.id, NewsArticle.published_at.between(art.published_at - CLUSTER_WINDOW,
                                                                      art.published_at + CLUSTER_WINDOW))).all()
        match = [n for n in nearby if title_similarity(art.title_normalized, n.title_normalized) >= CLUSTER_SIMILARITY]
        art.cluster_id = min([art.id] + [n.cluster_id or n.id for n in match])


def _finish(run_id: uuid.UUID, stats: DiscoveryStats, errors: list[str], *, aborted: bool) -> IngestionRun:
    db.session.rollback()
    run = db.session.get(IngestionRun, run_id)
    source = db.session.get(DataSource, PROVIDER_ID)
    run.records_received, run.records_accepted, run.records_stored = stats.items, stats.items, stats.new_articles
    run.records_filtered = stats.duplicates
    run.request_metadata = {**run.request_metadata, "stats": stats.__dict__, "errors": errors[:50]}
    ok_requests = stats.requests - stats.failed_requests
    if ok_requests == 0 or aborted:
        run.status, run.error_category = IngestionStatus.FAILED, "provider_unavailable"
    elif errors:
        run.status, run.error_category = IngestionStatus.PARTIALLY_SUCCEEDED, "partial"
    else:
        run.status, run.error_category = IngestionStatus.SUCCEEDED, None
    run.error_summary = redact("; ".join(errors[:5]))[:2000] or None
    run.finished_at = utcnow()
    if run.status != IngestionStatus.FAILED:
        source.last_successful_sync = run.finished_at
    source.last_attempted_sync = run.started_at
    source.last_error = run.error_summary
    source.data_quality_status = {"succeeded": "ok", "partially_succeeded": "degraded"}.get(run.status.value, "failing")
    db.session.commit()
    log.info("news discovery finished", extra={"event_type": "news_discovery", "status": run.status.value,
                                               "details": stats.__dict__})
    return run


def index_stored_articles(news_cfg: NewsConfig, symbols: tuple[str, ...] | None = None) -> dict[str, int]:
    """Create mentions for entities added after articles were stored (e.g. a newly tracked symbol), so
    existing coverage is not lost. Existing mentions are left alone (see recompute_relevance)."""
    targets = {s: e for s, e in news_cfg.entities.items() if symbols is None or s in symbols}
    have = set(db.session.execute(select(NewsMention.article_id, NewsMention.symbol)).all())
    created: dict[str, int] = {}
    for article in db.session.scalars(select(NewsArticle)).all():
        for symbol, entity in targets.items():
            if (article.id, symbol) in have:
                continue
            rel, reason = relevance(article.title, entity, article.publisher_domain, news_cfg.pk_context)
            if rel <= 0:
                continue
            db.session.add(NewsMention(article_id=article.id, symbol=symbol, relevance=rel, relevance_reason=reason,
                                       event_category=event_category(article.title, news_cfg.event_keywords)))
            created[symbol] = created.get(symbol, 0) + 1
    db.session.commit()
    return created


def recompute_relevance(news_cfg: NewsConfig) -> dict[str, int]:
    """Re-apply the current relevance rules to every stored mention (after a rule change). Scores of
    mentions that fall below the threshold are kept for audit but no longer aggregated; mentions that
    become relevant are left unscored for score_pending()."""
    changed = 0
    for mention, article in db.session.execute(select(NewsMention, NewsArticle).join(
            NewsArticle, NewsArticle.id == NewsMention.article_id)).all():
        entity = news_cfg.entities.get(mention.symbol)
        if entity is None:
            continue
        rel, reason = relevance(article.title, entity, article.publisher_domain, news_cfg.pk_context)
        if float(mention.relevance) != rel or mention.relevance_reason != reason:
            mention.relevance, mention.relevance_reason = rel, reason
            changed += 1
    db.session.commit()
    return {"changed": changed}


def score_pending(model: SentimentModel, *, batch: int = 64) -> int:
    """Score relevant, unscored mentions with `model`. Unsupported languages get no score."""
    pending = db.session.execute(
        select(NewsMention, NewsArticle).join(NewsArticle, NewsArticle.id == NewsMention.article_id)
        .where(NewsMention.relevance >= RELEVANCE_THRESHOLD, NewsMention.sentiment_model.is_(None))).all()
    done = 0
    for i in range(0, len(pending), batch):
        chunk = pending[i:i + batch]
        english = [(m, a) for m, a in chunk if a.language == "en"]
        results = model.score([a.title for _, a in english])
        now = utcnow()
        for (mention, _), res in zip(english, results, strict=True):
            mention.sentiment_label, mention.sentiment_score = res.label, res.score
            mention.sentiment_confidence, mention.sentiment_probs = res.confidence, res.probs or None
            mention.sentiment_model, mention.explanation, mention.scored_at = model.model_id, res.explanation[:500], now
        for mention, article in chunk:
            if article.language != "en":
                mention.sentiment_model, mention.explanation = model.model_id, "unsupported language: not scored"
        db.session.commit()
        done += len(english)
    return done


# --------------------------------------------------------------------------------------------------
# Daily aggregation


def session_cutoff(session_date: date) -> datetime:
    return datetime.combine(session_date, FORECAST_CUTOFF_PSX, tzinfo=PSX_TZ)


def mention_frame(symbol: str, model_id: str) -> pd.DataFrame:
    rows = db.session.execute(
        select(NewsArticle.id, NewsArticle.cluster_id, NewsArticle.available_at, NewsMention.relevance,
               NewsMention.sentiment_score, NewsMention.sentiment_confidence, NewsMention.event_category)
        .join(NewsMention, NewsMention.article_id == NewsArticle.id)
        .where(NewsMention.symbol == symbol, NewsMention.relevance >= RELEVANCE_THRESHOLD,
               NewsMention.sentiment_model == model_id, NewsMention.sentiment_score.is_not(None))).all()
    df = pd.DataFrame(rows, columns=["article_id", "cluster_id", "available_at", "relevance", "score", "confidence",
                                     "event"])
    if not df.empty:
        # An unclustered article is its own event (never silently dropped by the groupby).
        df["cluster_id"] = df["cluster_id"].fillna(df["article_id"])
        for col in ("relevance", "score", "confidence"):
            df[col] = df[col].astype(float)
        df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    return df


def aggregate_for_session(df: pd.DataFrame, session_date: date, method: str,
                          window_days: int = AGG_WINDOW_DAYS) -> dict[str, Any]:
    cutoff = pd.Timestamp(session_cutoff(session_date)).tz_convert("UTC")
    win = df[(df["available_at"] <= cutoff) & (df["available_at"] > cutoff - pd.Timedelta(days=window_days))] \
        if not df.empty else df
    if win.empty:
        return {"n_articles": 0, "n_events": 0, "score": None, "label": "insufficient", "evidence": []}
    events = win.groupby("cluster_id").agg(score=("score", "mean"), relevance=("relevance", "max"),
                                           confidence=("confidence", "mean"), available_at=("available_at", "min"))
    if method == "dedup_mean_v1":
        score = float(events["score"].mean())
    elif method == "weighted_v1":
        age_days = (cutoff - events["available_at"]).dt.total_seconds() / 86400
        decay = np.power(0.5, age_days / RECENCY_HALF_LIFE_DAYS)
        w = events["relevance"] * decay * events["confidence"].clip(lower=0.2)
        score = float((w * events["score"]).sum() / w.sum()) if w.sum() > 0 else float(events["score"].mean())
    else:
        raise ValueError(method)
    label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    if (events["score"] > 0.3).any() and (events["score"] < -0.3).any() and abs(score) <= 0.15:
        label = "mixed"
    return {"n_articles": int(len(win)), "n_events": int(len(events)), "score": round(score, 5), "label": label,
            "evidence": [int(i) for i in win["article_id"].tolist()[:50]]}


def rebuild_daily_sentiment(symbols: tuple[str, ...], sessions: pd.DatetimeIndex, model_id: str,
                            coverage_start: date) -> int:
    """(Re)compute aggregates for sessions on/after coverage_start. Before it there is no news
    coverage at all, which is different from 'insufficient evidence' and is not stored."""
    written = 0
    days = [d.date() for d in sessions if d.date() >= coverage_start]
    for symbol in symbols:
        df = mention_frame(symbol, model_id)
        existing = {(r.session_date, r.method): r for r in db.session.scalars(
            select(NewsSentimentDaily).where(NewsSentimentDaily.symbol == symbol))}
        for method in AGG_METHODS:
            for d in days:
                agg = aggregate_for_session(df, d, method)
                row = existing.get((d, method))
                if row is None:
                    row = NewsSentimentDaily(symbol=symbol, session_date=d, method=method, window_days=AGG_WINDOW_DAYS)
                    db.session.add(row)
                row.n_articles, row.n_events, row.score = agg["n_articles"], agg["n_events"], agg["score"]
                row.label, row.evidence_article_ids = agg["label"], agg["evidence"]
                written += 1
        db.session.commit()
    return written


def sentiment_series(symbol: str, method: str) -> pd.DataFrame:
    rows = db.session.execute(select(NewsSentimentDaily.session_date, NewsSentimentDaily.score,
                                     NewsSentimentDaily.n_events)
                              .where(NewsSentimentDaily.symbol == symbol, NewsSentimentDaily.method == method)
                              .order_by(NewsSentimentDaily.session_date)).all()
    df = pd.DataFrame(rows, columns=["session_date", "score", "n_events"])
    if df.empty:
        return df
    df["session_date"] = pd.to_datetime(df["session_date"])
    df["score"] = df["score"].astype(float)
    return df.set_index("session_date")


def coverage_start(config: Mapping[str, Any]) -> date:
    return date.fromisoformat(config["NEWS_COVERAGE_START"])
