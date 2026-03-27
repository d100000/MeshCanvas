"""FastAPI application entry point.

Slim orchestrator: creates app, wires up lifespan, mounts routers.
Business logic lives in services/; data access in repositories/.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import is_configured
from app.core.middleware import create_http_middleware
from app.core.request_logger import RequestLogger
from app.core.security import RateLimiter, build_security_headers
from app.database import LocalDatabase
from app.routers import analysis, auth, canvas, chat_ws, models, pages, settings, setup
from app.services.auth_service import AuthManager

# ---- Logging configuration ----
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
LOG_CLEANUP_INTERVAL_HOURS = 24


# ---- Lifespan ----

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("application starting up")

    # Initialize shared state
    database = LocalDatabase()
    if is_configured():
        await database.initialize()
        await database.delete_expired_sessions()

    request_logger = RequestLogger()
    auth_manager = AuthManager(database)
    rate_limiter = RateLimiter()

    # Store on app.state for dependency injection
    app.state.database = database
    app.state.auth_manager = auth_manager
    app.state.request_logger = request_logger
    app.state.rate_limiter = rate_limiter

    await request_logger.cleanup_old_logs()
    cleanup_task = asyncio.create_task(_periodic_log_cleanup(request_logger, database))

    logger.info("startup complete, log cleanup every %dh (retention %dd)",
                LOG_CLEANUP_INTERVAL_HOURS, request_logger.retention_days)

    yield

    # Shutdown
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    logger.info("application shut down")


# ---- App factory ----

app = FastAPI(title="Multi-Model Web Chat", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# HTTP middleware
_security_headers = build_security_headers()
_request_logger = RequestLogger()  # temporary for middleware setup
app.middleware("http")(create_http_middleware(_request_logger, _security_headers))

# Routers
app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(setup.router)
app.include_router(settings.router)
app.include_router(canvas.router)
app.include_router(models.router)
app.include_router(analysis.router)
app.include_router(chat_ws.router)


# ---- Background tasks ----

async def _periodic_log_cleanup(request_logger: RequestLogger, database: LocalDatabase) -> None:
    while True:
        await asyncio.sleep(LOG_CLEANUP_INTERVAL_HOURS * 3600)
        try:
            await request_logger.cleanup_old_logs()
            if is_configured():
                await database.delete_expired_sessions()
        except Exception:
            logger.warning("periodic cleanup failed", exc_info=True)
