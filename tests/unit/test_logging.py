import json
import logging

from app.utils.logging import ContextFilter, JsonFormatter, log_context, redact


def _record(msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("psx.test", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_redacts_database_password():
    out = redact("connect postgresql+psycopg://psx:s3cret@db:5432/x failed")
    assert "s3cret" not in out
    assert "postgresql+psycopg://psx:***@db" in out


def test_redacts_tokens_and_keys():
    for text in ("Authorization: Bearer abc123", "api_key=abc123", 'password="abc123"', "token: abc123"):
        assert "abc123" not in redact(text), text


def test_json_formatter_includes_context_and_redacts():
    flt = ContextFilter("testing", "api")
    with log_context(correlation_id="cid-1", symbol="OGDC"):
        record = _record("redis://:hunter2@host:6379/0 down", event_type="x", details={"password": "p"})
        flt.filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["correlation_id"] == "cid-1"
    assert payload["symbol"] == "OGDC"
    assert payload["environment"] == "testing"
    assert payload["service"] == "api"
    assert payload["event_type"] == "x"
    assert "hunter2" not in payload["message"]
    assert payload["severity"] == "INFO"


def test_context_is_scoped():
    flt = ContextFilter("testing", "api")
    with log_context(run_id="r1"):
        pass
    record = _record("after")
    flt.filter(record)
    assert not hasattr(record, "run_id")
