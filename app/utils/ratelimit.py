"""Fixed-window rate limiting shared across workers (Redis), with an in-process fallback.

Used for public endpoints that trigger outbound requests (news search -> Google News). If Redis is
unavailable the limiter keeps counting in process memory, so limits still apply (per worker)
rather than silently disappearing.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import redis

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after: int


class RateLimiter:
    def __init__(self, url: str, *, prefix: str = "rl:", timeout: float = 0.5) -> None:
        self._prefix = prefix
        self._client = redis.Redis.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout)
        self._local: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int, *, now: float | None = None) -> Decision:
        now = time.time() if now is None else now
        window = int(now // window_seconds)
        retry_after = int(window_seconds - (now % window_seconds)) + 1
        name = f"{self._prefix}{key}:{window}"
        try:
            pipe = self._client.pipeline()
            pipe.incr(name)
            pipe.expire(name, window_seconds + 1)
            count = int(pipe.execute()[0])
        except redis.RedisError:
            log.warning("rate limiter: Redis unavailable, counting in process",
                        extra={"event_type": "ratelimit_fallback"})
            with self._lock:
                w, c = self._local.get(key, (window, 0))
                count = (c if w == window else 0) + 1
                self._local[key] = (window, count)
        return Decision(count <= limit, max(0, limit - count), retry_after)
