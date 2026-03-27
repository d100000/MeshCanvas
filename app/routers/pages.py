"""HTML page-serving routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.core.config import is_configured
from app.dependencies import get_request_user

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _html_response(path: Path) -> FileResponse:
    resp = FileResponse(path)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/")
async def index(request: Request) -> Response:
    if not is_configured():
        return RedirectResponse(url="/setup", status_code=303)
    user = await get_request_user(request)
    if not user:
        return _html_response(STATIC_DIR / "login.html")
    return _html_response(STATIC_DIR / "index.html")


@router.get("/login")
async def login_page(request: Request) -> Response:
    if not is_configured():
        return RedirectResponse(url="/setup", status_code=303)
    user = await get_request_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)
    return _html_response(STATIC_DIR / "login.html")


@router.get("/settings")
async def settings_page(request: Request) -> Response:
    if not is_configured():
        return RedirectResponse(url="/setup", status_code=303)
    user = await get_request_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _html_response(STATIC_DIR / "settings.html")


@router.get("/setup")
async def setup_page(request: Request) -> Response:
    if is_configured():
        return RedirectResponse(url="/", status_code=303)
    return _html_response(STATIC_DIR / "setup.html")
