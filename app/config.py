"""Environment-specific configuration.

All settings come from environment variables (optionally loaded from `.env` by the
entrypoint). `validate()` is called by the application factory so a misconfigured
deployment fails at startup rather than on first use.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import date, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")


def _symbol_list(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name)
    raw = default if raw is None or raw.strip() == "" else raw
    symbols = []
    for part in raw.split(","):
        symbol = part.strip().upper()
        if not symbol:
            continue
        if not _SYMBOL_RE.match(symbol):
            raise ConfigError(f"{name} contains an invalid symbol: {part.strip()!r}")
        if symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)


def parse_clock(name: str, raw: str) -> time:
    try:
        return time.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be HH:MM, got {raw!r}") from exc


def parse_window(name: str, raw: str) -> tuple[time, time]:
    """Parse "HH:MM-HH:MM" (PSX local time)."""
    try:
        start_s, end_s = raw.split("-")
    except ValueError as exc:
        raise ConfigError(f"{name} must look like 09:15-17:00, got {raw!r}") from exc
    start, end = parse_clock(name, start_s), parse_clock(name, end_s)
    if start >= end:
        raise ConfigError(f"{name} start must be before its end")
    return start, end


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class BaseConfig:
    ENV_NAME = "base"
    TESTING = False
    DEBUG = False

    def __init__(self) -> None:
        self.SECRET_KEY = os.environ.get("SECRET_KEY", "")
        self.SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "")
        self.SQLALCHEMY_TRACK_MODIFICATIONS = False
        # Pin every connection to UTC so timestamptz values never come back in the server's local zone.
        self.SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "connect_args": {"options": "-c timezone=UTC"}}
        self.REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self.CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/1")
        self.CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
        self.CELERY_TASK_ALWAYS_EAGER = False
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
        self.LOG_FORMAT = os.environ.get("LOG_FORMAT", "json").lower()
        self.LOG_FILE = os.environ.get("LOG_FILE", "") or None
        self.ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "") or None
        self.ENABLE_SCHEDULED_INGESTION = _bool("ENABLE_SCHEDULED_INGESTION")
        self.ENABLE_MODEL_TRAINING = _bool("ENABLE_MODEL_TRAINING")
        self.ENABLE_MODEL_PROMOTION = _bool("ENABLE_MODEL_PROMOTION")
        self.MARKET_DATA_IMPORT_DIR = os.environ.get(
            "MARKET_DATA_IMPORT_DIR", str(PROJECT_ROOT / "data" / "imports")
        )
        self.STALE_AFTER_SESSIONS = _int("STALE_AFTER_SESSIONS", 2)
        self.WATCHLIST_CONFIG = os.environ.get("WATCHLIST_CONFIG", str(PROJECT_ROOT / "config" / "watchlist.yaml"))
        self.SOURCES_CONFIG = os.environ.get("SOURCES_CONFIG", str(PROJECT_ROOT / "config" / "sources.yaml"))
        self.API_MAX_PAGE_SIZE = 200
        self.API_DEFAULT_PAGE_SIZE = 50
        self.DEPENDENCY_CHECK_TIMEOUT_SECONDS = 1.0

        # psxdata-api: self-hosted REST wrapper around the psxdata library (see
        # docs/data_sources.md). Disabled by default; the service runs as a separate process.
        self.PSXDATA_ENABLED = _bool("PSXDATA_ENABLED")
        self.PSXDATA_API_URL = os.environ.get("PSXDATA_API_URL", "http://127.0.0.1:8100").rstrip("/")
        # A cache miss on /historical scrapes a symbol's full history, which can take a while.
        self.PSXDATA_TIMEOUT_SECONDS = _float("PSXDATA_TIMEOUT_SECONDS", 60.0)
        self.PSXDATA_MAX_ATTEMPTS = _int("PSXDATA_MAX_ATTEMPTS", 3)
        self.PSXDATA_SYMBOLS = _symbol_list("PSXDATA_SYMBOLS", "SYS,FFC,MEBL,MARI,KSE100")
        # Forecasting universe (subset of PSXDATA_SYMBOLS). Models were validated on these symbols only;
        # adding a symbol here requires re-running the experiments and model validation
        # (docs/model_governance.md). Every PSXDATA_SYMBOLS symbol still gets the analysis engine.
        # Unset -> the validated universe restricted to the data symbols; an explicit value is checked strictly.
        validated = ("SYS", "FFC", "MEBL", "MARI", "KSE100")
        self.MODEL_SYMBOLS = (_symbol_list("MODEL_SYMBOLS", ",".join(validated)) if os.environ.get("MODEL_SYMBOLS")
                              else tuple(s for s in validated if s in self.PSXDATA_SYMBOLS))
        # psxdata-api allows 60 requests/minute per client; one request per second stays under it.
        self.PSXDATA_REQUEST_SPACING_SECONDS = _float("PSXDATA_REQUEST_SPACING_SECONDS", 1.0)
        # Earliest session to load on a full backfill (YYYY-MM-DD); empty = all available history.
        self.PSXDATA_BACKFILL_START = os.environ.get("PSXDATA_BACKFILL_START", "").strip() or None
        # Incremental syncs re-fetch this many days before the latest stored bar to pick up corrections.
        self.PSXDATA_INCREMENTAL_OVERLAP_DAYS = _int("PSXDATA_INCREMENTAL_OVERLAP_DAYS", 10)
        # Scheduled daily-bar sync time (PSX local time, Mon-Fri), after the market closes.
        self.PSXDATA_DAILY_SYNC_TIME = os.environ.get("PSXDATA_DAILY_SYNC_TIME", "18:00")
        # PSX-local time after which today's daily bar is final. Earlier syncs skip today's in-progress bar.
        self.PSXDATA_SESSION_FINAL_TIME = os.environ.get("PSXDATA_SESSION_FINAL_TIME", "17:00")
        self.PSXDATA_QUOTE_POLL_INTERVAL_MINUTES = _int("PSXDATA_QUOTE_POLL_INTERVAL_MINUTES", 15)
        # PSX-local window (Mon-Fri) for scheduled quote polls; manual syncs ignore it.
        self.PSXDATA_QUOTE_WINDOW = os.environ.get("PSXDATA_QUOTE_WINDOW", "09:15-17:00")
        self.PSXDATA_QUOTES_STALE_AFTER_MINUTES = _int("PSXDATA_QUOTES_STALE_AFTER_MINUTES", 45)
        # Daily bars are stale when the newest bar is more than this many weekdays old.
        self.PSXDATA_BARS_STALE_AFTER_WEEKDAYS = _int("PSXDATA_BARS_STALE_AFTER_WEEKDAYS", 2)

        # Research layer (docs/research_methodology.md, docs/news_sentiment.md).
        self.ARTIFACTS_DIR = os.environ.get("ARTIFACTS_DIR", str(PROJECT_ROOT / "artifacts"))
        self.CORPORATE_ACTIONS_FILE = os.environ.get("CORPORATE_ACTIONS_FILE",
                                                     str(PROJECT_ROOT / "config" / "corporate_actions.yaml"))
        self.NEWS_CONFIG = os.environ.get("NEWS_CONFIG", str(PROJECT_ROOT / "config" / "news.yaml"))
        self.NEWS_ENABLED = _bool("NEWS_ENABLED")
        # Sentiment backend: lexicon_v1 | phrasebank_lr_v1 | finbert (finbert needs requirements-ml.txt).
        self.NEWS_SENTIMENT_MODEL = os.environ.get("NEWS_SENTIMENT_MODEL", "lexicon_v1")
        # First date with systematic news coverage (backfill start). Before it, "no news" means
        # "not covered", which is not the same as insufficient evidence.
        self.NEWS_COVERAGE_START = os.environ.get("NEWS_COVERAGE_START", "2023-01-01")
        # Daily research workflow (docs/model_governance.md). Scheduled only with ENABLE_SCHEDULED_INGESTION.
        self.ENABLE_RESEARCH_PIPELINE = _bool("ENABLE_RESEARCH_PIPELINE")
        self.RESEARCH_PIPELINE_TIME = os.environ.get("RESEARCH_PIPELINE_TIME", "18:30")
        self.FORECAST_HORIZONS = tuple(int(h) for h in os.environ.get("FORECAST_HORIZONS", "1,5,20").split(","))

        # Public website (docs/deployment.md).
        # Live PSX news search makes outbound Google News requests: limit callers and the total budget.
        self.NEWS_SEARCH_PER_IP_PER_MINUTE = _int("NEWS_SEARCH_PER_IP_PER_MINUTE", 20)
        self.NEWS_SEARCH_OUTBOUND_PER_MINUTE = _int("NEWS_SEARCH_OUTBOUND_PER_MINUTE", 30)
        self.SYMBOL_SUGGEST_PER_IP_PER_MINUTE = _int("SYMBOL_SUGGEST_PER_IP_PER_MINUTE", 120)
        # Seconds an engine result is reused per worker (data changes at most once per refresh).
        self.ENGINE_CACHE_SECONDS = _int("ENGINE_CACHE_SECONDS", 60)
        # Number of reverse proxies in front of the app whose X-Forwarded-* headers are trusted (0 = none).
        self.TRUSTED_PROXY_COUNT = _int("TRUSTED_PROXY_COUNT", 0)
        # Operations pages (/ops: system health, models, pipeline) require HTTP Basic auth with the admin
        # token as password. Default: required in production, open elsewhere.
        self.OPS_DASHBOARD_AUTH = _bool("OPS_DASHBOARD_AUTH", self.ENV_NAME == "production")

    def validate(self) -> None:
        if not self.SQLALCHEMY_DATABASE_URI:
            raise ConfigError("DATABASE_URL is required")
        if not self.SQLALCHEMY_DATABASE_URI.startswith("postgresql"):
            raise ConfigError("DATABASE_URL must be a PostgreSQL URL (postgresql+psycopg://...)")
        if self.LOG_FORMAT not in {"json", "text"}:
            raise ConfigError("LOG_FORMAT must be 'json' or 'text'")
        if self.ENABLE_MODEL_PROMOTION and not self.ENABLE_MODEL_TRAINING:
            raise ConfigError("ENABLE_MODEL_PROMOTION requires ENABLE_MODEL_TRAINING")
        if not self.PSXDATA_API_URL.startswith(("http://", "https://")):
            raise ConfigError("PSXDATA_API_URL must be an http(s) URL")
        if not 1 <= self.PSXDATA_TIMEOUT_SECONDS <= 300:
            raise ConfigError("PSXDATA_TIMEOUT_SECONDS must be between 1 and 300")
        if not 1 <= self.PSXDATA_MAX_ATTEMPTS <= 5:
            raise ConfigError("PSXDATA_MAX_ATTEMPTS must be between 1 and 5")
        if self.PSXDATA_REQUEST_SPACING_SECONDS < 1.0:
            raise ConfigError("PSXDATA_REQUEST_SPACING_SECONDS must be >= 1.0 (psxdata-api allows 60 requests/min)")
        if self.PSXDATA_QUOTE_POLL_INTERVAL_MINUTES < 5:
            raise ConfigError("PSXDATA_QUOTE_POLL_INTERVAL_MINUTES must be >= 5")
        if not 0 <= self.PSXDATA_INCREMENTAL_OVERLAP_DAYS <= 60:
            raise ConfigError("PSXDATA_INCREMENTAL_OVERLAP_DAYS must be between 0 and 60")
        if not self.PSXDATA_SYMBOLS:
            raise ConfigError("PSXDATA_SYMBOLS must list at least one symbol")
        if "KSE100" not in self.PSXDATA_SYMBOLS:
            raise ConfigError("PSXDATA_SYMBOLS must include KSE100 (the benchmark for relative indicators)")
        missing = sorted(set(self.MODEL_SYMBOLS) - set(self.PSXDATA_SYMBOLS))
        if missing:
            raise ConfigError(f"MODEL_SYMBOLS must be a subset of PSXDATA_SYMBOLS (missing: {', '.join(missing)})")
        if "KSE100" not in self.MODEL_SYMBOLS:
            raise ConfigError("MODEL_SYMBOLS must include KSE100")
        if self.PSXDATA_BACKFILL_START is not None:
            try:
                date.fromisoformat(self.PSXDATA_BACKFILL_START)
            except ValueError as exc:
                raise ConfigError("PSXDATA_BACKFILL_START must be YYYY-MM-DD") from exc
        parse_clock("PSXDATA_DAILY_SYNC_TIME", self.PSXDATA_DAILY_SYNC_TIME)
        parse_clock("PSXDATA_SESSION_FINAL_TIME", self.PSXDATA_SESSION_FINAL_TIME)
        parse_clock("RESEARCH_PIPELINE_TIME", self.RESEARCH_PIPELINE_TIME)
        if self.NEWS_SENTIMENT_MODEL not in {"lexicon_v1", "phrasebank_lr_v1", "finbert"}:
            raise ConfigError("NEWS_SENTIMENT_MODEL must be lexicon_v1, phrasebank_lr_v1 or finbert")
        try:
            date.fromisoformat(self.NEWS_COVERAGE_START)
        except ValueError as exc:
            raise ConfigError("NEWS_COVERAGE_START must be YYYY-MM-DD") from exc
        if not self.FORECAST_HORIZONS or any(h < 1 or h > 60 for h in self.FORECAST_HORIZONS):
            raise ConfigError("FORECAST_HORIZONS must be comma-separated integers in 1..60")
        parse_window("PSXDATA_QUOTE_WINDOW", self.PSXDATA_QUOTE_WINDOW)
        for name in ("NEWS_SEARCH_PER_IP_PER_MINUTE", "NEWS_SEARCH_OUTBOUND_PER_MINUTE",
                     "SYMBOL_SUGGEST_PER_IP_PER_MINUTE"):
            if not 1 <= getattr(self, name) <= 10_000:
                raise ConfigError(f"{name} must be between 1 and 10000")
        if not 0 <= self.TRUSTED_PROXY_COUNT <= 5:
            raise ConfigError("TRUSTED_PROXY_COUNT must be between 0 and 5")
        if self.OPS_DASHBOARD_AUTH and not self.ADMIN_API_TOKEN:
            raise ConfigError("OPS_DASHBOARD_AUTH requires ADMIN_API_TOKEN (used as the ops password)")

    def as_flask_mapping(self) -> dict:
        return {k: v for k, v in vars(self).items() if k.isupper()} | {
            "TESTING": self.TESTING,
            "DEBUG": self.DEBUG,
            "ENV_NAME": self.ENV_NAME,
        }


class DevelopmentConfig(BaseConfig):
    ENV_NAME = "development"
    DEBUG = True

    def __init__(self) -> None:
        super().__init__()
        if not self.SECRET_KEY:
            # Ephemeral key: sessions do not survive restarts, which is acceptable locally.
            self.SECRET_KEY = secrets.token_hex(32)
        self.LOG_FORMAT = os.environ.get("LOG_FORMAT", "text").lower()


class TestingConfig(BaseConfig):
    __test__ = False  # not a pytest test class
    ENV_NAME = "testing"
    TESTING = True

    def __init__(self) -> None:
        super().__init__()
        self.SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "")
        self.SECRET_KEY = "testing-secret-key"
        self.ADMIN_API_TOKEN = "test-admin-token"
        self.CELERY_TASK_ALWAYS_EAGER = True
        self.LOG_FORMAT = "json"
        self.LOG_FILE = None
        self.DEPENDENCY_CHECK_TIMEOUT_SECONDS = 0.5
        # Deterministic provider settings: tests never reach a real psxdata-api service
        # (HTTP is mocked; ".invalid" cannot resolve even if a mock were missed).
        self.PSXDATA_ENABLED = True
        self.PSXDATA_API_URL = "http://psxdata-api.invalid"
        self.PSXDATA_SYMBOLS = ("SYS", "FFC", "MEBL", "MARI", "KSE100")
        self.MODEL_SYMBOLS = ("SYS", "FFC", "MEBL", "MARI", "KSE100")
        self.PSXDATA_MAX_ATTEMPTS = 3
        self.PSXDATA_BACKFILL_START = None

    def validate(self) -> None:
        if not self.SQLALCHEMY_DATABASE_URI:
            raise ConfigError("TEST_DATABASE_URL is required to run database-backed tests")
        super().validate()


class ProductionConfig(BaseConfig):
    ENV_NAME = "production"

    def validate(self) -> None:
        super().validate()
        if not self.SECRET_KEY or self.SECRET_KEY == "replace_me" or len(self.SECRET_KEY) < 32:
            raise ConfigError("SECRET_KEY must be set to a random value of at least 32 characters")
        if not self.ADMIN_API_TOKEN or self.ADMIN_API_TOKEN == "replace_me" or len(self.ADMIN_API_TOKEN) < 24:
            raise ConfigError("ADMIN_API_TOKEN must be set (>= 24 characters) in production")


CONFIGS: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def load_config(env_name: str | None = None) -> BaseConfig:
    name = (env_name or os.environ.get("APP_ENV") or "development").lower()
    if name not in CONFIGS:
        raise ConfigError(f"Unknown APP_ENV {name!r}; expected one of {sorted(CONFIGS)}")
    return CONFIGS[name]()
