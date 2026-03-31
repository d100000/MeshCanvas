"""Tests for user settings and custom API key endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestUserSettings:
    @pytest.mark.asyncio
    async def test_get_settings(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is True
        assert "username" in data

    @pytest.mark.asyncio
    async def test_get_settings_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/settings")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_update_settings_forbidden(self, auth_client: AsyncClient):
        """Normal users cannot modify API settings — admin-only."""
        resp = await auth_client.put("/api/settings", json={"some": "data"})
        assert resp.status_code == 403


class TestUserCustomApiKey:
    @pytest.mark.asyncio
    async def test_get_custom_keys(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/user/custom-api-key")
        assert resp.status_code == 200
        data = resp.json()
        assert "model_keys" in data
        assert "use_custom_key" in data
        assert "models" in data

    @pytest.mark.asyncio
    async def test_set_custom_keys(self, auth_client: AsyncClient):
        resp = await auth_client.put("/api/user/custom-api-key", json={
            "model_keys": {"gpt-4": "sk-user-custom-key"},
            "use_custom_key": True,
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify key is now masked in response
        resp = await auth_client.get("/api/user/custom-api-key")
        data = resp.json()
        assert data["use_custom_key"] is True
        # Key should be masked, not plain
        if "gpt-4" in data["model_keys"]:
            assert data["model_keys"]["gpt-4"] != "sk-user-custom-key"

    @pytest.mark.asyncio
    async def test_set_custom_keys_keep_existing(self, auth_client: AsyncClient):
        """__KEEP__ sentinel should preserve existing key."""
        # Set a key first
        await auth_client.put("/api/user/custom-api-key", json={
            "model_keys": {"model-a": "sk-secret-value"},
            "use_custom_key": True,
        })
        # Now update with __KEEP__
        resp = await auth_client.put("/api/user/custom-api-key", json={
            "model_keys": {"model-a": "__KEEP__"},
            "use_custom_key": True,
        })
        assert resp.status_code == 200

        # Key should still exist (masked)
        resp = await auth_client.get("/api/user/custom-api-key")
        data = resp.json()
        assert "model-a" in data["model_keys"]

    @pytest.mark.asyncio
    async def test_delete_custom_keys(self, auth_client: AsyncClient):
        resp = await auth_client.delete("/api/user/custom-api-key")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify cleared
        resp = await auth_client.get("/api/user/custom-api-key")
        data = resp.json()
        assert data["use_custom_key"] is False
        assert data["model_keys"] == {}

    @pytest.mark.asyncio
    async def test_custom_keys_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/user/custom-api-key")
        assert resp.status_code == 401

        resp = await client.put("/api/user/custom-api-key", json={
            "model_keys": {},
            "use_custom_key": False,
        })
        assert resp.status_code == 401

        resp = await client.delete("/api/user/custom-api-key")
        assert resp.status_code == 401


class TestUserUsage:
    @pytest.mark.asyncio
    async def test_usage_detail(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/user/usage-detail")
        assert resp.status_code == 200
        data = resp.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_usage_detail_with_limit(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/user/usage-detail?limit=10")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_usage_summary(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/user/usage-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data

    @pytest.mark.asyncio
    async def test_usage_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/user/usage-detail")
        assert resp.status_code == 401

        resp = await client.get("/api/user/usage-summary")
        assert resp.status_code == 401
