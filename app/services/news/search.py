"""On-demand PSX news search: headlines about ONE listed company, and nothing else.

Flow for a symbol (e.g. LUCK):
  1. Resolve it against the PSX listing (psxdata-api /eligible-scrips, cached); unknown -> error.
  2. Build the company's matching rules: curated rules from config/news.yaml for tracked symbols,
     otherwise derived from the listed company name (name without "Limited", plus the exact-case
     ticker), always requiring Pakistani context (publisher or keyword) and excluding other listed
     companies whose names contain this one's (so "Fauji Fertilizer Bin Qasim" never matches FFC).
  3. Query Google News RSS for the company name (headline metadata only; links are shown as given,
     never followed), keep only headlines that pass the rules, merge in stored headlines (with their
     FinBERT tone) for tracked symbols, collapse syndicated duplicates, newest first.
Results are cached for RESULTS_TTL; the API layer rate-limits callers and the outbound budget.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.extensions import db
from app.models.research import NewsArticle, NewsMention
from app.services.news.google_news import GoogleNewsRss, NewsProviderError, domain_of
from app.services.news.relevance import (
    MARKET,
    RELEVANCE_THRESHOLD,
    EntityConfig,
    NewsConfig,
    load_news_config,
    normalize_title,
    relevance,
    title_similarity,
)
from app.utils.cache import SharedCache, TTLCache
from app.utils.logging import redact
from app.utils.time import PSX_TZ, utcnow

log = logging.getLogger(__name__)

SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{1,12}")
INDEX_ALIASES = {"KSE100": "KSE-100 Index"}
LISTING_TTL = 24 * 3600
RESULTS_TTL = 15 * 60
LOOKBACK_DAYS = 90
MAX_RESULTS = 40
DUPLICATE_SIMILARITY = 0.8
# Quote / screener / SEO pages are not news, whatever company they name.
NON_NEWS = re.compile(r"(share|stock) price (today|and chart|history|live)|stock quote|\bcompetitors\s*[-–]|"
                      r"\bPSX:[A-Z0-9]+\b|price,? news (&|and) analysis|cricket team records", re.IGNORECASE)
_SUFFIX = re.compile(r"[\s,.]*\b(limited|ltd\.?|\(pvt\.?\)|plc)\.?\s*$", re.IGNORECASE)

_listing_local = TTLCache(3600, maxsize=2)
_results_local = TTLCache(RESULTS_TTL, maxsize=512)
_config_local = TTLCache(300, maxsize=2)


class InvalidSymbol(ValueError):
    pass


class UnknownSymbol(LookupError):
    pass


class SearchUnavailable(RuntimeError):
    pass


def normalize_symbol(raw: str | None) -> str:
    symbol = (raw or "").strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise InvalidSymbol("symbol must be 1-12 letters or digits, e.g. LUCK")
    return symbol


def _news_config(config: Mapping[str, Any]) -> NewsConfig:
    cfg = _config_local.get("cfg")
    if cfg is None:
        cfg = load_news_config(Path(config["NEWS_CONFIG"]))
        _config_local.set("cfg", cfg)
    return cfg


def load_listing(config: Mapping[str, Any]) -> dict[str, str]:
    """{symbol: company name} for listed PSX instruments. Falls back to the tracked symbols only when
    the listing service is unavailable (then other symbols cannot be resolved)."""
    listing = _listing_local.get("listing")
    if listing is not None:
        return listing
    shared = SharedCache(config["REDIS_URL"], "psx:")
    listing = shared.get("listing:v1")
    if not listing and config.get("PSXDATA_ENABLED"):
        from app.services.ingestion.providers.base import ProviderError
        from app.services.ingestion.providers.psxdata_api import PsxDataApiClient

        try:
            client = PsxDataApiClient(base_url=config["PSXDATA_API_URL"], timeout_seconds=15, max_attempts=1)
            listing = client.fetch_listing()
            shared.set("listing:v1", listing, LISTING_TTL)
        except ProviderError as exc:
            log.warning("PSX listing unavailable", extra={"event_type": "listing_unavailable",
                                                          "details": {"error": str(exc)[:200]}})
            listing = None
    if not listing:
        cfg = _news_config(config)
        listing = {s: (e.strong[0] if e.strong else s) for s, e in cfg.entities.items() if s != MARKET}
        return listing | INDEX_ALIASES  # degraded: do not cache, retry next time
    listing = dict(listing) | INDEX_ALIASES
    _listing_local.set("listing", listing)
    return listing


def suggest(config: Mapping[str, Any], q: str, limit: int = 8) -> list[dict[str, str]]:
    """Symbols whose ticker starts with q, then companies whose name contains q."""
    q = (q or "").strip()
    if not q:
        return []
    listing = load_listing(config)
    qu, ql = q.upper(), q.lower()
    by_symbol = sorted(s for s in listing if s.startswith(qu))
    by_name = sorted(s for s, n in listing.items() if ql in n.lower() and s not in by_symbol)
    tracked = set(config["PSXDATA_SYMBOLS"])
    return [{"symbol": s, "name": listing[s], "tracked": s in tracked} for s in (by_symbol + by_name)[:limit]]


def company_alias(name: str) -> str:
    alias = _SUFFIX.sub("", name.strip()).strip(" ,.")
    return alias or name.strip()


def entity_for(symbol: str, name: str, cfg: NewsConfig, listing: Mapping[str, str]) -> EntityConfig:
    if symbol in INDEX_ALIASES:
        return cfg.entities[MARKET]
    if symbol in cfg.entities:
        return cfg.entities[symbol]
    alias = company_alias(name)
    strong = [alias]
    queries = [f'"{alias}"']
    if symbol.isalpha() and len(symbol) >= 3:
        strong.append(symbol)  # tickers match case-sensitively (see relevance._pattern)
        queries.append(f'"{symbol}" Pakistan')  # local press often uses the ticker (e.g. HBL, OGDC)
    alias_l = alias.lower()
    others = {company_alias(n) for s, n in listing.items() if s != symbol}
    exclude = sorted(o for o in others if alias_l in o.lower() and o.lower() != alias_l)[:25]
    exclude = [*cfg.search_exclusions.get(symbol, ()), *exclude]
    return EntityConfig(symbol, tuple(strong), (), (), tuple(exclude), tuple(queries), require_pk_context=True)


def _stored(symbol: str, since) -> list[dict[str, Any]]:  # noqa: ANN001
    key = MARKET if symbol in INDEX_ALIASES else symbol
    rows = db.session.execute(
        select(NewsArticle, NewsMention).join(NewsMention, NewsMention.article_id == NewsArticle.id)
        .where(NewsMention.symbol == key, NewsMention.relevance >= RELEVANCE_THRESHOLD,
               NewsArticle.published_at >= since)
        .order_by(NewsArticle.published_at.desc()).limit(200)).all()
    return [{"title": a.title, "url": a.url, "url_hash": a.url_hash, "publisher": a.publisher,
             "publisher_domain": a.publisher_domain, "published_at": a.published_at,
             "match": m.relevance_reason, "tone": m.sentiment_label,
             "tone_score": float(m.sentiment_score) if m.sentiment_score is not None else None,
             "event": m.event_category, "source": "stored"} for a, m in rows if not NON_NEWS.search(a.title)]


def _merge(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Newest first; syndicated near-duplicates collapse into one item listing extra publishers."""
    items = sorted(items, key=lambda i: (i["published_at"] is not None, i["published_at"]), reverse=True)
    seen: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for it in items:
        if it["url_hash"] in seen:
            prev = seen[it["url_hash"]]
            prev["tone"] = prev["tone"] or it.get("tone")
            prev["tone_score"] = prev["tone_score"] if prev["tone_score"] is not None else it.get("tone_score")
            continue
        norm = normalize_title(it["title"])
        dup = next((o for o in out if title_similarity(norm, o["_norm"]) >= DUPLICATE_SIMILARITY), None)
        if dup is not None:
            if it.get("publisher") and it["publisher"] not in dup["also"] and it["publisher"] != dup["publisher"]:
                dup["also"].append(it["publisher"])
            dup["tone"] = dup["tone"] or it.get("tone")
            seen[it["url_hash"]] = dup
            continue
        it = dict(it, _norm=norm, also=[])
        seen[it["url_hash"]] = it
        out.append(it)
    for it in out:
        it.pop("_norm", None)
        it["published_at"] = it["published_at"].isoformat() if it["published_at"] else None
        it.pop("url_hash", None)
    return out[:limit]


def search(config: Mapping[str, Any], raw_symbol: str, *, provider: GoogleNewsRss | None = None,
           limit: int = MAX_RESULTS, use_cache: bool = True,
           outbound_gate: Callable[[], bool] | None = None) -> dict[str, Any]:
    """`outbound_gate` is called only on a cache miss; returning False skips the live request
    (stored headlines are still returned for tracked symbols)."""
    symbol = normalize_symbol(raw_symbol)
    listing = load_listing(config)
    tracked = symbol in config["PSXDATA_SYMBOLS"]
    if symbol not in listing and not tracked:
        raise UnknownSymbol(f"{symbol} is not a listed PSX symbol")
    cfg = _news_config(config)
    name = listing.get(symbol, symbol)
    entity = entity_for(symbol, name, cfg, listing)
    # The key carries a fingerprint of the matching rules: editing config/news.yaml invalidates old results.
    rules = hashlib.sha256(repr((entity, cfg.pk_context)).encode()).hexdigest()[:12]
    cache_key = f"news-search:v2:{symbol}:{rules}"
    shared = SharedCache(config["REDIS_URL"], "psx:")
    if use_cache:
        hit = _results_local.get(cache_key) or shared.get(cache_key)
        if hit:
            return dict(hit, cached=True)

    now = utcnow()
    since = now - timedelta(days=LOOKBACK_DAYS)
    live: list[dict[str, Any]] = []
    rejected = 0
    live_error = None
    if outbound_gate is None or outbound_gate():
        provider = provider or GoogleNewsRss(timeout=8, min_interval=0)
        after = (now.astimezone(PSX_TZ) - timedelta(days=LOOKBACK_DAYS)).date()
        for query in entity.queries[:2]:
            try:
                found = provider.search(query, after=after)
            except NewsProviderError as exc:
                live_error = redact(str(exc))[:200]
                continue
            for a in found:
                rel, reason = relevance(a.title, entity, domain_of(a.publisher_url), cfg.pk_context)
                if rel < RELEVANCE_THRESHOLD or NON_NEWS.search(a.title):
                    rejected += 1
                    continue
                live.append({"title": a.title, "url": a.link, "url_hash": hashlib.sha256(a.link.encode()).hexdigest(),
                             "publisher": a.publisher, "publisher_domain": domain_of(a.publisher_url),
                             "published_at": a.published_at, "match": reason, "tone": None, "tone_score": None,
                             "event": None, "source": "live"})
    else:
        live_error = "live search budget exhausted; showing stored headlines only"
    stored = _stored(symbol, since) if (tracked or symbol in INDEX_ALIASES) else []
    if not live and not stored and live_error:
        raise SearchUnavailable(live_error)
    results = _merge(live + stored, limit)
    out = {"symbol": symbol, "company": name, "tracked": tracked, "lookback_days": LOOKBACK_DAYS,
           "match_rules": {"aliases": list(entity.strong), "requires_pakistan_context": entity.require_pk_context,
                           "excluded_names": list(entity.exclude)},
           "results": results,
           "counts": {"shown": len(results), "live_matched": len(live), "stored": len(stored),
                      "rejected_as_unrelated": rejected},
           "source": "Google News RSS headlines (links open the publisher via Google News)",
           "fetched_at": now.isoformat(), "partial": live_error is not None, "warning": live_error, "cached": False}
    if live_error is None:
        _results_local.set(cache_key, out)
        shared.set(cache_key, out, RESULTS_TTL)
    return out


def clear_caches() -> None:
    """For tests and admin use."""
    _listing_local.clear()
    _results_local.clear()
    _config_local.clear()
