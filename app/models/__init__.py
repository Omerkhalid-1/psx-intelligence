"""ORM models. Importing this package registers every table on `db.metadata`
(required by Alembic autogenerate and by the test fixtures)."""

from app.models.forecasting import (
    BacktestPrediction,
    EnsembleWeights,
    Experiment,
    Forecast,
    ForecastOutcome,
    ModelEvent,
    ModelVersion,
    PipelineRun,
)
from app.models.market_data import (
    DailyBar,
    DataSource,
    IngestionRejection,
    IngestionRun,
    IngestionStatus,
    Instrument,
    QuoteSnapshot,
)
from app.models.operations import AuditEvent, JobRun, JobStatus
from app.models.research import (
    AnalysisSnapshot,
    CorporateAction,
    DataQualityIssue,
    FeatureSnapshot,
    NewsArticle,
    NewsMention,
    NewsSentimentDaily,
)

__all__ = [
    "AnalysisSnapshot",
    "AuditEvent",
    "BacktestPrediction",
    "CorporateAction",
    "DataQualityIssue",
    "EnsembleWeights",
    "Experiment",
    "FeatureSnapshot",
    "Forecast",
    "ForecastOutcome",
    "ModelEvent",
    "ModelVersion",
    "NewsArticle",
    "NewsMention",
    "NewsSentimentDaily",
    "PipelineRun",
    "DailyBar",
    "DataSource",
    "IngestionRejection",
    "IngestionRun",
    "IngestionStatus",
    "Instrument",
    "JobRun",
    "JobStatus",
    "QuoteSnapshot",
]
