"""HTTP middleware for logging and security headers."""

from __future__ import annotations

import asyncio
from time import perf_counter
from urllib.parse import urlparse

from fastapi import Request

from app.core.request_logger import RequestLogger
from app.core.security import build_security_headers


def _is_origin_allowed(origin: str | None, host: str | None) -> bool:
    if not origin or not host:
        return False
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    return parsed.netloc.lower() == host.lower()


def create_http_middleware(request_logger: RequestLogger, security_headers: dict[str, str]):
    """Return a FastAPI middleware callable."""

    async def middleware(request: Request, call_next):
        started_at = perf_counter()
        response = await call_next(request)
        duration_ms = round((perf_counter() - started_at) * 1000, 2)
        client_host = request.client.host if request.client else "unknown"
        asyncio.create_task(
            request_logger.log_event(
                {
                    "type": "http_request",
                    "method": request.method,
                    "path": request.url.path,
                    "query": str(request.url.query),
                    "status_code": response.status_code,
                    "client_id": client_host,
                    "duration_ms": duration_ms,
                }
            )
        )
        for key, value in security_headers.items():
            response.headers.setdefault(key, value)
        return response

    return middleware
