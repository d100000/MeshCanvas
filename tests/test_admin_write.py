"""Tests for admin write operations (recharge, set-role, reset-password, pricing, config)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _ensure_test_user(admin_client: AsyncClient) -> dict:
    """Ensure a non-admin user exists and return its info dict."""
    resp = await admin_client.get("/api/admin/users")
    users = resp.json()["users"]
    target = next((u for u in users if u["username"] != "admin"), None)
    if target is not None:
        return target
    # Create one via registration
    from app.captcha import generate as captcha_generate
    import app.captcha as captcha_mod
    from tests.conftest import _solve_captcha
    from app.deps import rate_limiter
    rate_limiter._buckets.clear()
    orig = captcha_mod.MIN_AGE
    captcha_mod.MIN_AGE = 0
    try:
        q, t = captcha_generate()
        a = _solve_captcha(q)
        await admin_client.post("/api/auth/register", json={
            "username": "admintest_user",
            "password": "testpass123",
            "captcha_token": t,
            "captcha_answer": str(a),
            "website": "",
        })
    finally:
        captcha_mod.MIN_AGE = orig
    resp = await admin_client.get("/api/admin/users")
    users = resp.json()["users"]
    return next(u for u in users if u["username"] != "admin")


class TestAdminRecharge:
    @pytest.mark.asyncio
    async def test_recharge_user(self, admin_client: AsyncClient):
        target = await _ensure_test_user(admin_client)

        resp = await admin_client.post("/api/admin/recharge", json={
            "user_id": target["id"],
            "points": 50.0,
            "remark": "测试充值",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "balance" in data

    @pytest.mark.asyncio
    async def test_recharge_nonexistent_user(self, admin_client: AsyncClient):
        resp = await admin_client.post("/api/admin/recharge", json={
            "user_id": 99999,
            "points": 10.0,
        })
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_recharge_zero_points_rejected(self, admin_client: AsyncClient):
        resp = await admin_client.post("/api/admin/recharge", json={
            "user_id": 1,
            "points": 0,
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_recharge_requires_admin(self, auth_client: AsyncClient):
        resp = await auth_client.post("/api/admin/recharge", json={
            "user_id": 1,
            "points": 10.0,
        })
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_recharge_invalid_payload(self, admin_client: AsyncClient):
        """Missing required fields should return 422 (Pydantic validation)."""
        resp = await admin_client.post("/api/admin/recharge", json={})
        assert resp.status_code == 422


class TestAdminSetRole:
    @pytest.mark.asyncio
    async def test_set_role_to_admin(self, admin_client: AsyncClient):
        target = await _ensure_test_user(admin_client)

        resp = await admin_client.post("/api/admin/set-role", json={
            "user_id": target["id"],
            "role": "admin",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Revert back to user
        resp = await admin_client.post("/api/admin/set-role", json={
            "user_id": target["id"],
            "role": "user",
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_set_role_invalid_value(self, admin_client: AsyncClient):
        """Pydantic validator should reject invalid role."""
        resp = await admin_client.post("/api/admin/set-role", json={
            "user_id": 1,
            "role": "superadmin",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_cannot_remove_own_admin(self, admin_client: AsyncClient):
        """Admin should not be able to demote themselves."""
        # Get admin user ID
        resp = await admin_client.get("/api/admin/users")
        users = resp.json()["users"]
        admin_user = next(u for u in users if u["username"] == "admin")

        resp = await admin_client.post("/api/admin/set-role", json={
            "user_id": admin_user["id"],
            "role": "user",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_set_role_nonexistent_user(self, admin_client: AsyncClient):
        resp = await admin_client.post("/api/admin/set-role", json={
            "user_id": 99999,
            "role": "admin",
        })
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_set_role_requires_admin(self, auth_client: AsyncClient):
        resp = await auth_client.post("/api/admin/set-role", json={
            "user_id": 1,
            "role": "admin",
        })
        assert resp.status_code == 401


class TestAdminResetPassword:
    @pytest.mark.asyncio
    async def test_reset_password(self, admin_client: AsyncClient):
        target = await _ensure_test_user(admin_client)

        resp = await admin_client.post("/api/admin/reset-password", json={
            "user_id": target["id"],
            "new_password": "newpassword123",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_reset_password_too_short(self, admin_client: AsyncClient):
        """Password shorter than 8 chars should fail Pydantic validation."""
        resp = await admin_client.post("/api/admin/reset-password", json={
            "user_id": 1,
            "new_password": "short",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_reset_password_nonexistent_user(self, admin_client: AsyncClient):
        resp = await admin_client.post("/api/admin/reset-password", json={
            "user_id": 99999,
            "new_password": "validpassword123",
        })
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_reset_password_requires_admin(self, auth_client: AsyncClient):
        resp = await auth_client.post("/api/admin/reset-password", json={
            "user_id": 1,
            "new_password": "newpassword123",
        })
        assert resp.status_code == 401


class TestAdminPricing:
    @pytest.mark.asyncio
    async def test_upsert_pricing(self, admin_client: AsyncClient):
        resp = await admin_client.put("/api/admin/pricing", json={
            "model_id": "test-model-pricing",
            "display_name": "Test Model",
            "input_points_per_1k": 1.5,
            "output_points_per_1k": 3.0,
            "is_active": 1,
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_get_pricing_list(self, admin_client: AsyncClient):
        resp = await admin_client.get("/api/admin/pricing")
        assert resp.status_code == 200
        data = resp.json()
        assert "pricing" in data
        assert isinstance(data["pricing"], list)

    @pytest.mark.asyncio
    async def test_pricing_empty_model_id_rejected(self, admin_client: AsyncClient):
        """model_id must have min_length=1."""
        resp = await admin_client.put("/api/admin/pricing", json={
            "model_id": "",
            "input_points_per_1k": 1.0,
            "output_points_per_1k": 2.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_pricing_negative_points_rejected(self, admin_client: AsyncClient):
        """Negative pricing should fail ge=0 constraint."""
        resp = await admin_client.put("/api/admin/pricing", json={
            "model_id": "negative-test",
            "input_points_per_1k": -1.0,
            "output_points_per_1k": 2.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_pricing_invalid_is_active(self, admin_client: AsyncClient):
        """is_active must be 0 or 1."""
        resp = await admin_client.put("/api/admin/pricing", json={
            "model_id": "active-test",
            "is_active": 5,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_delete_pricing(self, admin_client: AsyncClient):
        # Create first
        await admin_client.put("/api/admin/pricing", json={
            "model_id": "to-delete",
            "input_points_per_1k": 1.0,
            "output_points_per_1k": 2.0,
        })
        resp = await admin_client.delete("/api/admin/pricing/to-delete")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_pricing_requires_admin(self, auth_client: AsyncClient):
        resp = await auth_client.put("/api/admin/pricing", json={
            "model_id": "test",
            "input_points_per_1k": 1.0,
            "output_points_per_1k": 2.0,
        })
        assert resp.status_code == 401


class TestAdminConfig:
    @pytest.mark.asyncio
    async def test_update_config(self, admin_client: AsyncClient):
        resp = await admin_client.put("/api/admin/config", json={
            "config_default_points": "200",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_update_config_unknown_key_ignored(self, admin_client: AsyncClient):
        """Unknown config keys should be silently ignored, not rejected."""
        resp = await admin_client.put("/api/admin/config", json={
            "unknown_key": "value",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_update_config_requires_admin(self, auth_client: AsyncClient):
        resp = await auth_client.put("/api/admin/config", json={
            "config_default_points": "100",
        })
        assert resp.status_code == 401


class TestAdminModelConfig:
    @pytest.mark.asyncio
    async def test_update_model_config(self, admin_client: AsyncClient):
        resp = await admin_client.put("/api/admin/model-config", json={
            "api_base_url": "https://api.example.com/v1",
            "api_format": "openai",
            "api_key": "sk-testkey123456",
            "models": [{"name": "GPT-4", "id": "gpt-4"}],
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_model_config_empty_base_url_rejected(self, admin_client: AsyncClient):
        resp = await admin_client.put("/api/admin/model-config", json={
            "api_base_url": "",
            "api_key": "sk-test",
            "models": [{"name": "Test", "id": "test"}],
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_model_config_empty_models_rejected(self, admin_client: AsyncClient):
        """min_length=1 on models list."""
        resp = await admin_client.put("/api/admin/model-config", json={
            "api_base_url": "https://api.example.com/v1",
            "api_key": "sk-test",
            "models": [],
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_model_config_invalid_format_rejected(self, admin_client: AsyncClient):
        resp = await admin_client.put("/api/admin/model-config", json={
            "api_base_url": "https://api.example.com/v1",
            "api_format": "invalid_format",
            "api_key": "sk-test",
            "models": [{"name": "Test", "id": "test"}],
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_model_config_requires_admin(self, auth_client: AsyncClient):
        resp = await auth_client.put("/api/admin/model-config", json={
            "api_base_url": "https://api.example.com/v1",
            "api_key": "sk-test",
            "models": [{"name": "Test", "id": "test"}],
        })
        assert resp.status_code == 401


class TestAdminRechargeLog:
    @pytest.mark.asyncio
    async def test_get_recharge_logs(self, admin_client: AsyncClient):
        resp = await admin_client.get("/api/admin/recharge-logs")
        assert resp.status_code == 200
        data = resp.json()
        assert "logs" in data

    @pytest.mark.asyncio
    async def test_recharge_logs_requires_admin(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/admin/recharge-logs")
        assert resp.status_code == 401
