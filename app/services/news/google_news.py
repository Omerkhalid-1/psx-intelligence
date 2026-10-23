"""Google News RSS discovery provider.

What it gives: headline (with " - Publisher" suffix), publisher name and site URL, a Google
redirect link, and an RFC-822 publication time. What it does NOT give: article text, guaranteed
completeness or delivery latency. Historical searches (after:/before:) mostly return a date-only
time (07:00/08:00 GMT placeholders), recorded as precision "date_only".

We never follow the redirect links or fetch article pages (no scraping of publisher sites).
Terms: Google's RSS feeds are used for the owner's private research only; commercial use or
redistribution has not been cleared (docs/data_sources.md).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlsplit

import requests
from defusedxml import ElementTree as SafeET

log = logging.getLogger(__name__)

PROVIDER_ID = "google_news_rss"
BASE_URL = "https://news.google.com/rss/search"
USER_AGENT = "psx-intelligence/0.1 (private research; RSS reader)"
PLACEHOLDER_TIMES = {(7, 0, 0), (8, 0, 0), (5, 0, 0), (0, 0, 0)}
MAX_BYTES = 5 * 1024 * 1024

_session_factory: Callable[[], requests.Session] = requests.Session
_sleep: Callable[[float], None] = time.sleep


class NewsProviderError(Exception):
    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class RawArticle:
    title: str  # publisher suffix removed
    link: str
    publisher: str | None
    publisher_url: str | None
    published_at: datetime | None
    published_precision: str  # exact | date_only | unknown


class GoogleNewsRss:
    provider_id = PROVIDER_ID

    def __init__(self, *, timeout: float = 20, min_interval: float = 3.0, language: str = "en", region: str = "PK",
                 session: requests.Session | None = None) -> None:
        self._timeout = timeout
        self._min_interval = min_interval
        self._lang, self._region = language, region
        self._session = session or _session_factory()
        self._last_request = 0.0

    def search(self, query: str, *, after: date | None = None, before: date | None = None) -> list[RawArticle]:
        q = query + (f" after:{after.isoformat()}" if after else "")
        q += f" before:{before.isoformat()}" if before else ""
        params = {"q": q, "hl": f"{self._lang}-{self._region}", "gl": self._region,
                  "ceid": f"{self._region}:{self._lang}"}
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            _sleep(wait)
        self._last_request = time.monotonic()
        try:
            resp = self._session.get(f"{BASE_URL}?{urlencode(params)}", timeout=(5, self._timeout),
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml"})
        except requests.RequestException as exc:
            raise NewsProviderError(f"request failed: {type(exc).__name__}", transient=True) from None
        if resp.status_code == 429 or resp.status_code >= 500:
            raise NewsProviderError(f"HTTP {resp.status_code}", transient=True)
        if resp.status_code != 200:
            raise NewsProviderError(f"HTTP {resp.status_code}", transient=False)
        if len(resp.content) > MAX_BYTES:
            raise NewsProviderError("feed exceeds size limit", transient=False)
        return parse_feed(resp.content)


def parse_feed(content: bytes) -> list[RawArticle]:
    try:
        root = SafeET.fromstring(content)  # defusedxml: no entity expansion / external entities
    except Exception as exc:  # noqa: BLE001 - any parse failure is a provider data error
        raise NewsProviderError(f"invalid RSS: {type(exc).__name__}", transient=False) from None
    out = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link.startswith("https://"):
            continue
        src = item.find("source")
        publisher = src.text.strip() if src is not None and src.text else None
        publisher_url = src.get("url") if src is not None else None
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].strip()
        published_at, precision = _parse_date(item.findtext("pubDate"))
        out.append(RawArticle(title, link, publisher, publisher_url, published_at, precision))
    return out


def _parse_date(raw: str | None) -> tuple[datetime | None, str]:
    if not raw:
        return None, "unknown"
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None, "unknown"
    if dt.tzinfo is None:
        return None, "unknown"
    dt = dt.astimezone(UTC)
    precision = "date_only" if (dt.hour, dt.minute, dt.second) in PLACEHOLDER_TIMES else "exact"
    return dt, precision


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url).hostname or ""
    return re.sub(r"^www\.", "", host.lower()) or None
