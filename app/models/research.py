"""Research-layer tables: data quality, corporate actions and derived features.

Observed data (daily_bars) is never modified here. Adjustments live in `corporate_actions`
and are applied at read time; derived values carry the feature-set version that produced them.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.base import recorded_at_col, updated_at_col


class CorporateAction(db.Model):
    """Corporate actions from cited reference data: share-count changes (bonus, split, consolidation),
    which adjust prices, and large cash dividends (cash_dividend), recorded only to explain an
    ex-dividend price drop (never adjusted)."""

    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    action_type: Mapped[str] = mapped_column(String(16), nullable=False)  # bonus|split|consolidation|cash_dividend
    share_multiplier: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    cash_dividend_per_share: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))  # reference only
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # verified | unverified
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # Result of checking the entry against stored prices: consistent | inconsistent | no_data
    price_check: Mapped[str | None] = mapped_column(String(16))
    price_check_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = recorded_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        UniqueConstraint("symbol", "ex_date", "action_type"),
        CheckConstraint("share_multiplier > 0", name="multiplier_positive"),
        CheckConstraint("action_type IN ('bonus', 'split', 'consolidation', 'cash_dividend')",
                        name="action_type_valid"),
    )


class DataQualityIssue(db.Model):
    """One finding from a data-quality check. Upserted by (check, symbol, session_date) so reruns
    do not duplicate; `resolved_at` is set when a later run no longer finds the issue."""

    __tablename__ = "data_quality_issues"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    check_name: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    session_date: Mapped[date | None] = mapped_column(Date)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # info | warning | error
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    first_detected_at: Mapped[datetime] = recorded_at_col()
    last_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_dq_issue_key", "check_name", "symbol", "session_date", unique=True,
              postgresql_nulls_not_distinct=True),
        Index("ix_dq_open", "resolved_at", "severity"),
    )


class FeatureSnapshot(db.Model):
    """Point-in-time features for one symbol and session, computed only from data available at
    that session's close. Immutable per (symbol, session, feature_set_version, data_version)."""

    __tablename__ = "feature_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    feature_set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # Hash of the inputs (bars + corporate actions) so recomputation after a data revision is visible.
    data_version: Mapped[str] = mapped_column(String(64), nullable=False)
    values: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    adjustment_status: Mapped[str] = mapped_column(String(32), nullable=False)  # adjusted | unexplained_gap_nearby
    computed_at: Mapped[datetime] = recorded_at_col()

    __table_args__ = (
        UniqueConstraint("symbol", "session_date", "feature_set_version", "data_version"),
        Index("ix_feature_snapshots_lookup", "symbol", "feature_set_version", "session_date"),
    )


class AnalysisSnapshot(db.Model):
    """Persisted research summary (trend, momentum, S/R zones, caveats) for a symbol/session."""

    __tablename__ = "analysis_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    method_version: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    computed_at: Mapped[datetime] = recorded_at_col()

    __table_args__ = (UniqueConstraint("symbol", "session_date", "method_version"),)


class NewsArticle(db.Model):
    """A discovered headline. Deduplicated by URL and by normalised title per publisher."""

    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # e.g. google_news_rss
    url: Mapped[str] = mapped_column(Text, nullable=False)  # provider link (may be a redirect URL)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    title_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    publisher: Mapped[str | None] = mapped_column(String(200))
    publisher_domain: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "exact" when the source gives a time of day; "date_only" when it looks like a placeholder time.
    published_precision: Mapped[str] = mapped_column(String(16), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When a forecast may first use this article (conservative; see docs/news_sentiment.md).
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    backfilled: Mapped[bool] = mapped_column(nullable=False, default=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    cluster_id: Mapped[int | None] = mapped_column(BigInteger)  # earliest article of a near-duplicate group
    query: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_news_articles_published", "published_at"),
        Index("ix_news_articles_title_publisher", "title_normalized", "publisher_domain"),
    )


class NewsMention(db.Model):
    """Relevance, event category and sentiment of one article for one symbol (or MARKET)."""

    __tablename__ = "news_mentions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(BigInteger, db.ForeignKey("news_articles.id", ondelete="CASCADE"),
                                            nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)  # ticker or "MARKET"
    relevance: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)  # 0..1
    relevance_reason: Mapped[str] = mapped_column(Text, nullable=False)
    event_category: Mapped[str] = mapped_column(String(32), nullable=False)
    sentiment_label: Mapped[str | None] = mapped_column(String(16))  # positive|neutral|negative|mixed
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))  # -1..1
    sentiment_confidence: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))
    sentiment_probs: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    sentiment_model: Mapped[str | None] = mapped_column(String(100))
    explanation: Mapped[str | None] = mapped_column(Text)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("article_id", "symbol"),
        Index("ix_news_mentions_symbol", "symbol"),
        CheckConstraint("relevance >= 0 AND relevance <= 1", name="relevance_range"),
    )


class NewsSentimentDaily(db.Model):
    """Per-symbol, per-session sentiment aggregate using only articles available by that session."""

    __tablename__ = "news_sentiment_daily"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)  # aggregation method id
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    n_articles: Mapped[int] = mapped_column(Integer, nullable=False)
    n_events: Mapped[int] = mapped_column(Integer, nullable=False)  # after cluster dedup
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 5))  # None = insufficient evidence
    label: Mapped[str] = mapped_column(String(16), nullable=False)  # ... | insufficient
    evidence_article_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    computed_at: Mapped[datetime] = recorded_at_col()

    __table_args__ = (UniqueConstraint("symbol", "session_date", "method", "window_days"),)
