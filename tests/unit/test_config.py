import pytest

from app.config import ConfigError, DevelopmentConfig, ProductionConfig, TestingConfig, load_config


def test_unknown_env_rejected():
    with pytest.raises(ConfigError):
        load_config("staging-typo")


def test_database_url_required(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        DevelopmentConfig().validate()


def test_non_postgres_url_rejected(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    with pytest.raises(ConfigError, match="PostgreSQL"):
        DevelopmentConfig().validate()


def test_production_requires_strong_secrets(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("SECRET_KEY", "replace_me")
    monkeypatch.setenv("ADMIN_API_TOKEN", "x" * 40)
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        ProductionConfig().validate()
    monkeypatch.setenv("SECRET_KEY", "s" * 40)
    monkeypatch.setenv("ADMIN_API_TOKEN", "short")
    with pytest.raises(ConfigError, match="ADMIN_API_TOKEN"):
        ProductionConfig().validate()
    monkeypatch.setenv("ADMIN_API_TOKEN", "t" * 40)
    ProductionConfig().validate()


def test_invalid_boolean_flag_rejected(monkeypatch):
    monkeypatch.setenv("ENABLE_MODEL_TRAINING", "maybe")
    with pytest.raises(ConfigError, match="ENABLE_MODEL_TRAINING"):
        DevelopmentConfig()


def test_promotion_requires_training(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("ENABLE_MODEL_PROMOTION", "true")
    monkeypatch.setenv("ENABLE_MODEL_TRAINING", "false")
    with pytest.raises(ConfigError, match="PROMOTION"):
        DevelopmentConfig().validate()


def test_feature_flags_default_off(monkeypatch):
    for name in ("ENABLE_SCHEDULED_INGESTION", "ENABLE_MODEL_TRAINING", "ENABLE_MODEL_PROMOTION"):
        monkeypatch.delenv(name, raising=False)
    cfg = DevelopmentConfig()
    assert not cfg.ENABLE_SCHEDULED_INGESTION
    assert not cfg.ENABLE_MODEL_TRAINING
    assert not cfg.ENABLE_MODEL_PROMOTION


def test_testing_config_uses_test_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/prod")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://u:p@h/test")
    assert TestingConfig().SQLALCHEMY_DATABASE_URI.endswith("/test")


def _dev(monkeypatch, **env):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.delenv("MODEL_SYMBOLS", raising=False)  # hermetic: a developer .env may set it
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = DevelopmentConfig()
    cfg.validate()
    return cfg


def test_psxdata_defaults_are_safe(monkeypatch):
    for name in ("PSXDATA_ENABLED", "PSXDATA_SYMBOLS", "PSXDATA_API_URL", "PSXDATA_REQUEST_SPACING_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    cfg = _dev(monkeypatch)
    assert cfg.PSXDATA_ENABLED is False
    assert cfg.PSXDATA_API_URL == "http://127.0.0.1:8100"
    assert cfg.PSXDATA_SYMBOLS == ("SYS", "FFC", "MEBL", "MARI", "KSE100")
    assert cfg.PSXDATA_REQUEST_SPACING_SECONDS >= 1.0  # stays under 60 requests/minute


def test_psxdata_symbol_list_is_normalised(monkeypatch):
    cfg = _dev(monkeypatch, PSXDATA_SYMBOLS=" sys, FFC ,,sys,kse100")
    assert cfg.PSXDATA_SYMBOLS == ("SYS", "FFC", "KSE100")


@pytest.mark.parametrize("env,match", [
    ({"PSXDATA_API_URL": "ftp://x"}, "PSXDATA_API_URL"),
    ({"PSXDATA_REQUEST_SPACING_SECONDS": "0.2"}, "60 requests/min"),
    ({"PSXDATA_QUOTE_POLL_INTERVAL_MINUTES": "1"}, "QUOTE_POLL_INTERVAL"),
    ({"PSXDATA_TIMEOUT_SECONDS": "0"}, "TIMEOUT"),
    ({"PSXDATA_SYMBOLS": "SYS,DROP TABLE"}, "invalid symbol"),
    ({"PSXDATA_SYMBOLS": "../etc/passwd"}, "invalid symbol"),
    ({"PSXDATA_QUOTE_WINDOW": "17:00-09:00"}, "QUOTE_WINDOW"),
    ({"PSXDATA_DAILY_SYNC_TIME": "6pm"}, "DAILY_SYNC_TIME"),
    ({"PSXDATA_BACKFILL_START": "2016/01/01"}, "BACKFILL_START"),
])
def test_psxdata_invalid_settings_rejected(monkeypatch, env, match):
    with pytest.raises(ConfigError, match=match):
        _dev(monkeypatch, **env)


def test_model_symbols_must_be_a_subset_including_the_benchmark(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("PSXDATA_SYMBOLS", "SYS,LUCK,KSE100")
    monkeypatch.setenv("MODEL_SYMBOLS", "SYS,FFC,KSE100")
    with pytest.raises(ConfigError, match="subset"):
        DevelopmentConfig().validate()
    monkeypatch.setenv("MODEL_SYMBOLS", "SYS")
    with pytest.raises(ConfigError, match="MODEL_SYMBOLS must include KSE100"):
        DevelopmentConfig().validate()
    monkeypatch.setenv("PSXDATA_SYMBOLS", "SYS,LUCK")
    with pytest.raises(ConfigError, match="PSXDATA_SYMBOLS must include KSE100"):
        DevelopmentConfig().validate()
    monkeypatch.setenv("PSXDATA_SYMBOLS", "SYS,LUCK,KSE100")
    monkeypatch.setenv("MODEL_SYMBOLS", "SYS,KSE100")
    cfg = DevelopmentConfig()
    cfg.validate()
    assert cfg.MODEL_SYMBOLS == ("SYS", "KSE100") and "LUCK" in cfg.PSXDATA_SYMBOLS


def test_ops_auth_defaults_on_in_production_only(monkeypatch):
    monkeypatch.delenv("OPS_DASHBOARD_AUTH", raising=False)
    assert ProductionConfig().OPS_DASHBOARD_AUTH is True
    assert DevelopmentConfig().OPS_DASHBOARD_AUTH is False


def test_model_symbols_default_follows_the_data_symbols(monkeypatch):
    monkeypatch.delenv("MODEL_SYMBOLS", raising=False)
    cfg = _dev(monkeypatch, PSXDATA_SYMBOLS="SYS,LUCK,KSE100")
    assert cfg.MODEL_SYMBOLS == ("SYS", "KSE100")
