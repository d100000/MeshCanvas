from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = 300  # purge empty buckets every 5 minutes

    async def allow_async(self, key: str, limit: int, window_seconds: int) -> bool:
        async with self._lock:
            return self._allow_unlocked(key, limit, window_seconds)

    def _allow_unlocked(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        bucket = self._buckets[key]
        threshold = now - window_seconds
        while bucket and bucket[0] < threshold:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        self._maybe_cleanup(now)
        return True

    def _maybe_cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        empty_keys = [k for k, v in self._buckets.items() if not v]
        for k in empty_keys:
            del self._buckets[k]


def build_security_headers() -> dict[str, str]:
    return {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": "; ".join(
            [
                "default-src 'self'",
                "img-src 'self' data:",
                "style-src 'self'",
                "script-src 'self'",
                "connect-src 'self' ws: wss:",
                "font-src 'self' data:",
                "base-uri 'self'",
                "form-action 'self'",
                "frame-ancestors 'none'",
            ]
        ),
    }
