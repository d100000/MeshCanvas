from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

_RATE_LIMITER_MAX_KEYS = 50_000


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
        is_new_key = key not in self._buckets
        if is_new_key and len(self._buckets) >= _RATE_LIMITER_MAX_KEYS:
            return False
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


_CSP_COMMON = [
    "default-src 'self'",
    "img-src 'self' data:",
    "media-src 'self' data:",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
]

# 严格策略：用于画布、登录、设置、管理后台等页面
STRICT_CSP = "; ".join(
    _CSP_COMMON
    + [
        "style-src 'self'",
        "script-src 'self'",
        "connect-src 'self' ws: wss:",
        "font-src 'self' data:",
    ]
)

# Landing page 策略：允许 Google Fonts 外部加载 + 内联样式（HTML style 属性）
LANDING_CSP = "; ".join(
    _CSP_COMMON
    + [
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "script-src 'self'",
        "connect-src 'self'",
        "font-src 'self' data: https://fonts.gstatic.com",
    ]
)


def build_security_headers() -> dict[str, str]:
    return {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": STRICT_CSP,
    }
