"""Test doubles for outbound HTTP. No test may reach a real provider."""

from __future__ import annotations

import json
from typing import Any

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict


class FakeAdapter(BaseAdapter):
    """Replays queued outcomes in order; each is an Exception or (status, body, headers).
    The last outcome repeats if more requests arrive than were queued."""

    def __init__(self, *outcomes: Any) -> None:
        super().__init__()
        self.outcomes = list(outcomes)
        self.requests: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):  # noqa: ANN001 - requests adapter signature
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        status, body, *rest = outcome
        headers = rest[0] if rest else {}
        resp = requests.Response()
        resp.status_code = status
        resp._content = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body).encode())
        resp.headers = CaseInsensitiveDict({"Content-Type": "application/json", **headers})
        resp.url = request.url
        resp.request = request
        return resp

    def close(self) -> None:
        pass


class RoutedAdapter(FakeAdapter):
    """Serves outcomes by URL path; a list of outcomes is consumed in order (last one repeats)."""

    def __init__(self, routes: dict[str, Any]) -> None:
        super().__init__(None)
        self.routes = {k: (list(v) if isinstance(v, list) else [v]) for k, v in routes.items()}

    def send(self, request, **kwargs):  # noqa: ANN001
        from urllib.parse import urlsplit

        path = urlsplit(request.url).path
        queue = self.routes.get(path)
        if queue is None:
            self.outcomes = [(404, {"error": {"status": 404, "code": "not_found", "message": path}})]
        else:
            self.outcomes = [queue.pop(0) if len(queue) > 1 else queue[0]]
        return super().send(request, **kwargs)


def fake_session(*outcomes: Any) -> tuple[requests.Session, FakeAdapter]:
    adapter = FakeAdapter(*outcomes)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session, adapter


# --- psxdata-api response builders (shapes match psxdata-api v0.5.0, verified live 2026-10-13).
# Values are invented test data, not market data.


def ok_body(data, *, cached: bool = False) -> dict:
    meta = {"timestamp": "2026-10-13T12:00:00+00:00", "cached": cached}
    if isinstance(data, list):
        meta["count"] = len(data)
    return {"data": data, "meta": meta}


def err_body(status: int, code: str, message: str = "error") -> dict:
    return {"error": {"status": status, "code": code, "message": message}}


def bar(day: str, close: float, *, open_=None, high=None, low=None, volume: int = 1000, anomaly: bool = False) -> dict:
    o = close if open_ is None else open_
    return {"date": day, "open": o, "high": high if high is not None else max(o, close) + 1,
            "low": low if low is not None else min(o, close) - 1, "close": close, "volume": volume,
            "is_anomaly": anomaly}


def bars_body(*rows: dict, cached: bool = False) -> dict:
    # psxdata-api returns newest first
    return ok_body(sorted(rows, key=lambda r: r["date"], reverse=True), cached=cached)


def quote_body(symbol: str, price: float, change_pct: float | None = 0.5, **extra) -> dict:
    data = {"symbol": symbol, "price": price, "market_cap": None, "pe_ratio": 10.5, "dividend_yield": 2.0,
            "free_float": None, "volume_avg_30d": 100000.0, "change_1y_pct": 5.0, "ldcp": None, "change": None,
            "change_pct": change_pct, **extra}
    return ok_body(data)


INDICES = ok_body(["KSE100", "KSE30", "KMI30"])
