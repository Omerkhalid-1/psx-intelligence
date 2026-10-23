"""Relevance (is this headline about the company/market?) and event categorisation.

Relevance is decided independently of sentiment:
  1.0  a strong alias appears (e.g. "Meezan Bank"; all-caps tickers match case-sensitively)
  0.6  a weak alias appears together with at least one context word
  0.0  otherwise, or an exclusion phrase appears
Headlines below RELEVANCE_THRESHOLD are stored (for audit) but excluded from sentiment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RELEVANCE_THRESHOLD = 0.5
MARKET = "MARKET"


@dataclass(frozen=True)
class EntityConfig:
    symbol: str
    strong: tuple[str, ...]
    weak: tuple[str, ...]
    context: tuple[str, ...]
    exclude: tuple[str, ...]
    queries: tuple[str, ...]
    require_pk_context: bool = False


@dataclass(frozen=True)
class PkContext:
    publisher_domains: frozenset[str] = frozenset()
    words: tuple[str, ...] = ()
    foreign_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewsConfig:
    entities: dict[str, EntityConfig]
    event_keywords: dict[str, tuple[str, ...]]
    provider: dict[str, Any]
    pk_context: PkContext = PkContext()
    # Extra exclusions for derived (non-curated) symbols in the on-demand search: {symbol: (phrases)}.
    search_exclusions: dict[str, tuple[str, ...]] = field(default_factory=dict)


def load_news_config(path: Path) -> NewsConfig:
    doc = yaml.safe_load(path.read_text())
    entities = {}
    for symbol, e in (doc.get("symbols") or {}).items():
        entities[symbol] = EntityConfig(symbol, tuple(e.get("strong_aliases", [])), tuple(e.get("weak_aliases", [])),
                                        tuple(e.get("context_words", [])), tuple(e.get("exclude", [])),
                                        tuple(e.get("queries", [])), bool(e.get("require_pk_context", False)))
    m = doc.get("market") or {}
    entities[MARKET] = EntityConfig(MARKET, tuple(m.get("strong_aliases", [])), (), (), (), tuple(m.get("queries", [])))
    events = {k: tuple(v) for k, v in (doc.get("event_keywords") or {}).items()}
    pk = doc.get("pk_context") or {}
    context = PkContext(frozenset(d.lower() for d in pk.get("publisher_domains", [])), tuple(pk.get("words", [])),
                        tuple(pk.get("foreign_markers", [])))
    overrides = {str(k).upper(): tuple((v or {}).get("exclude", []))
                 for k, v in (doc.get("search_overrides") or {}).items()}
    return NewsConfig(entities, events, doc.get("provider") or {}, context, overrides)


def _pattern(phrase: str) -> re.Pattern[str]:
    flags = 0 if (phrase.isupper() and len(phrase) <= 6) else re.IGNORECASE  # tickers: exact case
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", flags)


def relevance(title: str, entity: EntityConfig, publisher_domain: str | None = None,
              pk: PkContext | None = None) -> tuple[float, str]:
    if entity.require_pk_context and pk is not None:
        marker = next((m for m in pk.foreign_markers if _contains(title, m)), None)
        if marker:
            return 0.0, f"foreign-market marker '{marker}'"
        pk_publisher = (publisher_domain or "").lower() in pk.publisher_domains
        pk_word = next((w for w in pk.words if _pattern(w).search(title)), None)
        if not (pk_publisher or pk_word):
            return 0.0, "no Pakistani context (publisher or keyword) for an ambiguous company name"
    for phrase in entity.exclude:
        if _pattern(phrase).search(title):
            return 0.0, f"excluded phrase '{phrase}'"
    for phrase in entity.strong:
        if _pattern(phrase).search(title):
            return 1.0, f"alias '{phrase}'"
    for phrase in entity.weak:
        if _pattern(phrase).search(title):
            ctx = [w for w in entity.context if _pattern(w).search(title)]
            if ctx:
                return 0.6, f"alias '{phrase}' with context '{ctx[0]}'"
            return 0.2, f"ambiguous alias '{phrase}' without context"
    return 0.0, "no alias"


def _contains(title: str, marker: str) -> bool:
    return marker in title if not marker.isalnum() else bool(_pattern(marker).search(title))


def event_category(title: str, keywords: dict[str, tuple[str, ...]]) -> str:
    for category, words in keywords.items():
        if any(_pattern(w).search(title) for w in words):
            return category
    return "general"


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s%]")


def normalize_title(title: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", title.lower())).strip()


def title_similarity(a: str, b: str) -> float:
    """Jaccard similarity of word sets of normalised titles (for syndicated-story clustering)."""
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
