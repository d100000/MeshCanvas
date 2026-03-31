"""Shared fixtures for tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Force an in-memory / temp DB before importing the app
_tmp_dir = tempfile.mkdtemp(prefix="nanobob_test_")
os.environ.setdefault("LOCAL_DB_PATH", os.path.join(_tmp_dir, "test.db"))
os.environ.setdefault("REQUEST_LOG_DIR", os.path.join(_tmp_dir, "logs"))

# Ensure models_setting.json exists so app boots without /setup redirect
_models_setting_path = os.path.join(_tmp_dir, "models_setting.json")
os.environ["MODELS_SETTING_PATH"] = _models_setting_path

import json as _json
Path(_models_setting_path).write_text(
    _json.dumps({
        "models": ["test-model-1", "test-model-2"],
        "API_key": "sk-test-key-for-testing",
        "base_url": "https://api.test.example/v1",
        "api_format": "openai",
    })
)

from app.main import app  # noqa: E402
from app.deps import database, auth_manager, rate_limiter  # noqa: E402

_auth_user_counter = 0


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def initialized_db():
    """Initialize database once per session."""
    await database.initialize()
    from app.bootstrap_admin import ensure_default_admin_user
    await ensure_default_admin_user(database)
    yield database


@pytest_asyncio.fixture
async def client(initialized_db) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client wired to the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_client(client: AsyncClient, initialized_db) -> AsyncClient:
    """Client already authenticated as a fresh test user (unique per test)."""
    global _auth_user_counter
    _auth_user_counter += 1
    username = f"tuser{_auth_user_counter:04d}"

    from app.captcha import generate as captcha_generate
    import app.captcha as captcha_mod

    # Reset rate limiter to avoid 429 across tests
    rate_limiter._buckets.clear()

    original_min = captcha_mod.MIN_AGE
    captcha_mod.MIN_AGE = 0

    try:
        question, token = captcha_generate()
        answer = _solve_captcha(question)

        resp = await client.post("/api/auth/register", json={
            "username": username,
            "password": "testpass123",
            "captcha_token": token,
            "captcha_answer": str(answer),
            "website": "",
        })
        assert resp.status_code == 200, f"Register {username} failed: {resp.text}"
    finally:
        captcha_mod.MIN_AGE = original_min

    return client


@pytest_asyncio.fixture
async def admin_client(client: AsyncClient, initialized_db) -> AsyncClient:
    """Client authenticated as the default admin user via admin_session cookie."""
    from app.captcha import generate as captcha_generate
    import app.captcha as captcha_mod

    # Reset rate limiter to avoid 429 across tests
    rate_limiter._buckets.clear()

    original_min = captcha_mod.MIN_AGE
    captcha_mod.MIN_AGE = 0

    try:
        question, token = captcha_generate()
        answer = _solve_captcha(question)
        resp = await client.post("/api/admin/login", json={
            "username": "admin",
            "password": "admin",
            "captcha_token": token,
            "captcha_answer": str(answer),
            "website": "",
        })
        assert resp.status_code == 200, f"Admin login failed: {resp.text}"
    finally:
        captcha_mod.MIN_AGE = original_min

    return client


def _solve_captcha(question: str) -> int:
    """Parse a captcha question like '12 + 7 = ?' and return the answer."""
    import re
    m = re.match(r"(\d+)\s*([+×])\s*(\d+)", question)
    if not m:
        raise ValueError(f"Cannot parse captcha: {question}")
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    return a + b if op == "+" else a * b
