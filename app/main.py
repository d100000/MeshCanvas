"""Multi-Model Web Chat — FastAPI entry point.

This is the thin application shell: creates the FastAPI app, registers
middleware, mounts static files, includes routers, and defines the
startup event.  All route handlers live in ``app.routers.*``; shared
state and helpers live in ``app.deps``.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# ── shared state (singletons) ────────────────────────────────────────────────
from app.deps import (
    STATIC_DIR,
    _SETUP_ALLOWED_PREFIXES,
    database,
    request_logger,
    security_headers,
    _http_log_tasks,
)
from app.bootstrap_admin import ensure_default_admin_user
from app.config import is_configured
from app.security import LANDING_CSP

logger = logging.getLogger(__name__)


# ── FastAPI application ──────────────────────────────────────────────────────

app = FastAPI(title="Multi-Model Web Chat")
app.mount("/static", StaticFiles(directory=STATIC_DIR, html=False), name="static")


# ── JSON structured logging ──────────────────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    """Format Python log records as single-line JSON for ELK / Loki."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return _json.dumps(obj, ensure_ascii=False)


def _configure_json_logging() -> None:
    if os.getenv("LOG_FORMAT", "").lower() != "json":
        return
    json_handler = logging.StreamHandler()
    json_handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(json_handler)
    root.setLevel(logging.INFO)


# ── Startup event ────────────────────────────────────────────────────────────

async def _init_default_system_config() -> None:
    cfg = await database.get_system_config()
    defaults = {
        "config_default_points": "100",
        "config_low_balance_threshold": "10",
        "config_allow_registration": "true",
        "config_search_points_per_call": "5",
        "config_admin_initial_points_granted": "false",
    }
    for key, value in defaults.items():
        if key not in cfg:
            await database.set_system_config(key, value)
    admin_user = await database.get_user_by_username("admin")
    if admin_user:
        await database.ensure_user_balance(admin_user["id"])
        if cfg.get("config_admin_initial_points_granted", "false").lower() != "true":
            balance = await database.get_user_balance(admin_user["id"])
            if balance <= 0:
                await database.add_points(admin_user["id"], 100, admin_user["id"], "系统初始赠送")
            await database.set_system_config("config_admin_initial_points_granted", "true")


@app.on_event("startup")
async def initialize_local_database() -> None:
    _configure_json_logging()
    await database.initialize()
    await database.delete_expired_sessions()
    await ensure_default_admin_user(database)
    await _init_default_system_config()
    request_logger.set_db_callback(database.record_event)
    retention_days = max(1, int(os.getenv("LOG_RETENTION_DAYS", "30")))
    token_retention_days = max(1, int(os.getenv("TOKEN_LOG_RETENTION_DAYS", "90")))
    await asyncio.to_thread(request_logger.cleanup_old_jsonl)
    try:
        removed = await database.cleanup_old_events(retention_days)
        token_removed = await database.cleanup_old_token_usage(token_retention_days)
        failure_removed = await database.cleanup_old_failure_logs()
        if removed or token_removed or failure_removed:
            logger.info(
                "Startup cleanup: events=%d token_usage=%d failure_logs=%d",
                removed, token_removed, failure_removed,
            )
    except Exception:
        logger.warning("Startup cleanup failed", exc_info=True)


# ── Middleware ────────────────────────────────────────────────────────────────

@app.middleware("http")
async def log_http_request(request: Request, call_next):
    started_at = perf_counter()
    response = await call_next(request)
    duration_ms = round((perf_counter() - started_at) * 1000, 2)
    client_host = request.client.host if request.client else "unknown"

    if request.url.path.startswith("/static/"):
        ext = request.url.path.rsplit(".", 1)[-1].lower() if "." in request.url.path else ""
        if ext in ("js", "css", "png", "jpg", "jpeg", "webp", "svg", "ico", "woff", "woff2"):
            # 带 ?v= 版本参数的资源可以长缓存；ES module 子模块 import 不带 ?v= 需每次重新验证
            if "v=" in str(request.url.query):
                response.headers.setdefault("Cache-Control", "public, max-age=86400, immutable")
            else:
                response.headers.setdefault("Cache-Control", "no-cache")
        elif ext in ("html",):
            response.headers.setdefault("Cache-Control", "no-store")

    _t = asyncio.create_task(
        request_logger.emit(
            "http_request",
            level="info",
            client_host=client_host,
            data={
                "method": request.method,
                "path": request.url.path,
                "query": str(request.url.query) or None,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
    )
    _http_log_tasks.add(_t)
    _t.add_done_callback(_http_log_tasks.discard)
    for key, value in security_headers.items():
        if key == "Content-Security-Policy" and request.url.path == "/":
            response.headers[key] = LANDING_CSP
        else:
            response.headers.setdefault(key, value)
    return response


@app.middleware("http")
async def setup_guard(request: Request, call_next):
    """Block every route until initial setup is complete."""
    if not is_configured():
        path = request.url.path
        if not any(path.startswith(p) for p in _SETUP_ALLOWED_PREFIXES):
            accept = request.headers.get("accept", "")
            if "application/json" in accept or path.startswith("/api/") or path.startswith("/ws/"):
                return JSONResponse(
                    {"detail": "系统尚未完成初始化，请先访问 /setup 进行配置。"},
                    status_code=503,
                )
            return RedirectResponse(url="/setup", status_code=303)
    return await call_next(request)


# ── Include routers ──────────────────────────────────────────────────────────

from app.routers.pages import router as pages_router  # noqa: E402
from app.routers.auth import router as auth_router  # noqa: E402
from app.routers.admin import router as admin_router  # noqa: E402
from app.routers.user import router as user_router  # noqa: E402
from app.routers.models import router as models_router  # noqa: E402
from app.routers.canvas import router as canvas_router  # noqa: E402
from app.routers.chat_ws import router as chat_ws_router  # noqa: E402

app.include_router(pages_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(user_router)
app.include_router(models_router)
app.include_router(canvas_router)
app.include_router(chat_ws_router)
