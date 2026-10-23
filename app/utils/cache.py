"""Small caching helpers.

TTLCache: thread-safe in-process cache with expiry and a size bound (per gunicorn worker).
SharedCache: JSON values in Redis with a TTL, shared by all workers; every Redis failure degrades
to a cache miss (the caller recomputes) and is logged, never raised.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import redis

log = logging.getLogger(__name__)


class TTLCache:
    def __init__(self, ttl_seconds: float, maxsize: int = 256, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl, self._max, self._clock = ttl_seconds, maxsize, clock
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires <= self._clock():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (self._clock() + self._ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class SharedCache:
    def __init__(self, url: str, prefix: str, timeout: float = 0.5) -> None:
        self._prefix = prefix
        self._client = redis.Redis.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout)

    def get(self, key: str) -> Any | None:
        try:
            raw = self._client.get(self._prefix + key)
        except redis.RedisError:
            log.warning("shared cache read failed", extra={"event_type": "cache_error"})
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        try:
            self._client.set(self._prefix + key, json.dumps(value, default=str), ex=ttl_seconds)
        except redis.RedisError:
            log.warning("shared cache write failed", extra={"event_type": "cache_error"})
