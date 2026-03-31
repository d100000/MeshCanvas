"""Tests for key API endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestPublicEndpoints:
    @pytest.mark.asyncio
    async def test_landing_page(self, client: AsyncClient):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "NanoBob" in resp.text

    @pytest.mark.asyncio
    async def test_login_page(self, client: AsyncClient):
        resp = await client.get("/login")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_login_page(self, client: AsyncClient):
        resp = await client.get("/admin")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_captcha_endpoint(self, client: AsyncClient):
        resp = await client.get("/api/captcha")
        assert resp.status_code == 200
        data = resp.json()
        assert "question" in data
        assert "token" in data
        assert "= ?" in data["question"]
        # Token should have 3 parts (not 4 — answer must not leak)
        parts = data["token"].split("|")
        assert len(parts) == 3

    @pytest.mark.asyncio
    async def test_registration_status(self, client: AsyncClient):
        resp = await client.get("/api/auth/registration-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "allow" in data


class TestAuthFlow:
    @pytest.mark.asyncio
    async def test_register_login_logout(self, client: AsyncClient):
        """Full auth lifecycle."""
        from app.captcha import generate as captcha_generate
        import app.captcha as captcha_mod
        import re

        orig_min = captcha_mod.MIN_AGE
        captcha_mod.MIN_AGE = 0

        try:
            # Register
            q, t = captcha_generate()
            m = re.match(r"(\d+)\s*([+×])\s*(\d+)", q)
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            answer = a + b if op == "+" else a * b

            resp = await client.post("/api/auth/register", json={
                "username": "apitest01",
                "password": "testpass123",
                "captcha_token": t,
                "captcha_answer": str(answer),
                "website": "",
            })
            # Could be 200 (new) or 409 (already exists from other test)
            assert resp.status_code in (200, 409)

            # Login
            q2, t2 = captcha_generate()
            m2 = re.match(r"(\d+)\s*([+×])\s*(\d+)", q2)
            a2, op2, b2 = int(m2.group(1)), m2.group(2), int(m2.group(3))
            ans2 = a2 + b2 if op2 == "+" else a2 * b2

            resp = await client.post("/api/auth/login", json={
                "username": "apitest01",
                "password": "testpass123",
                "captcha_token": t2,
                "captcha_answer": str(ans2),
                "website": "",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["username"] == "apitest01"

            # Session check
            resp = await client.get("/api/auth/session")
            assert resp.status_code == 200
            session_data = resp.json()
            assert session_data["authenticated"] is True
            assert session_data["username"] == "apitest01"

            # Logout
            resp = await client.post("/api/auth/logout")
            assert resp.status_code == 200

            # Session should be invalid now
            resp = await client.get("/api/auth/session")
            assert resp.status_code == 200
            session_data = resp.json()
            assert session_data["authenticated"] is False
        finally:
            captcha_mod.MIN_AGE = orig_min

    @pytest.mark.asyncio
    async def test_honeypot_rejects_bots(self, client: AsyncClient):
        """Filling the honeypot field should be rejected."""
        from app.captcha import generate as captcha_generate
        import app.captcha as captcha_mod
        import re

        orig_min = captcha_mod.MIN_AGE
        captcha_mod.MIN_AGE = 0
        try:
            q, t = captcha_generate()
            m = re.match(r"(\d+)\s*([+×])\s*(\d+)", q)
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            answer = a + b if op == "+" else a * b

            resp = await client.post("/api/auth/register", json={
                "username": "botuser99",
                "password": "testpass123",
                "captcha_token": t,
                "captcha_answer": str(answer),
                "website": "i-am-a-bot",  # Honeypot filled!
            })
            assert resp.status_code == 400
        finally:
            captcha_mod.MIN_AGE = orig_min


class TestProtectedEndpoints:
    @pytest.mark.asyncio
    async def test_models_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/models")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_canvases_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/canvases")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_settings_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/settings")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_api_requires_admin(self, client: AsyncClient):
        resp = await client.get("/api/admin/users")
        assert resp.status_code == 401


class TestAdminEndpoints:
    @pytest.mark.asyncio
    async def test_admin_users_list(self, admin_client: AsyncClient):
        resp = await admin_client.get("/api/admin/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        # Default admin should exist
        usernames = [u["username"] for u in data["users"]]
        assert "admin" in usernames

    @pytest.mark.asyncio
    async def test_admin_model_config(self, admin_client: AsyncClient):
        resp = await admin_client.get("/api/admin/model-config")
        assert resp.status_code == 200
        data = resp.json()
        assert "api_base_url" in data
        assert "models" in data

    @pytest.mark.asyncio
    async def test_admin_usage(self, admin_client: AsyncClient):
        resp = await admin_client.get("/api/admin/usage")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_config(self, admin_client: AsyncClient):
        resp = await admin_client.get("/api/admin/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data

    @pytest.mark.asyncio
    async def test_admin_audit_logs(self, admin_client: AsyncClient):
        resp = await admin_client.get("/api/admin/audit-logs")
        assert resp.status_code == 200
        data = resp.json()
        assert "logs" in data
