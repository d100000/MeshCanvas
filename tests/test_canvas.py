"""Tests for canvas CRUD operations."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestCanvasListRequiresAuth:
    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/canvases")
        assert resp.status_code == 401


class TestCanvasCreate:
    @pytest.mark.asyncio
    async def test_create_canvas_default_name(self, auth_client: AsyncClient):
        resp = await auth_client.post("/api/canvases", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert "canvas_id" in data
        assert data["name"] == "新画布"

    @pytest.mark.asyncio
    async def test_create_canvas_custom_name(self, auth_client: AsyncClient):
        resp = await auth_client.post("/api/canvases", json={"name": "我的画布"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "我的画布"

    @pytest.mark.asyncio
    async def test_create_canvas_empty_name_gets_default(self, auth_client: AsyncClient):
        resp = await auth_client.post("/api/canvases", json={"name": "  "})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "新画布"

    @pytest.mark.asyncio
    async def test_create_canvas_requires_auth(self, client: AsyncClient):
        resp = await client.post("/api/canvases", json={"name": "test"})
        assert resp.status_code == 401


class TestCanvasList:
    @pytest.mark.asyncio
    async def test_list_canvases(self, auth_client: AsyncClient):
        # Create a canvas first
        create_resp = await auth_client.post("/api/canvases", json={"name": "列表测试"})
        assert create_resp.status_code == 200

        # List should contain the canvas
        resp = await auth_client.get("/api/canvases")
        assert resp.status_code == 200
        data = resp.json()
        assert "canvases" in data
        assert isinstance(data["canvases"], list)
        assert len(data["canvases"]) >= 1


class TestCanvasRename:
    @pytest.mark.asyncio
    async def test_rename_canvas(self, auth_client: AsyncClient):
        # Create
        create_resp = await auth_client.post("/api/canvases", json={"name": "旧名称"})
        canvas_id = create_resp.json()["canvas_id"]

        # Rename
        resp = await auth_client.patch(f"/api/canvases/{canvas_id}", json={"name": "新名称"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_rename_canvas_empty_name_rejected(self, auth_client: AsyncClient):
        create_resp = await auth_client.post("/api/canvases", json={"name": "测试"})
        canvas_id = create_resp.json()["canvas_id"]

        resp = await auth_client.patch(f"/api/canvases/{canvas_id}", json={"name": "  "})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rename_nonexistent_canvas(self, auth_client: AsyncClient):
        resp = await auth_client.patch("/api/canvases/nonexistent-id", json={"name": "test"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rename_requires_auth(self, client: AsyncClient):
        resp = await client.patch("/api/canvases/any-id", json={"name": "test"})
        assert resp.status_code == 401


class TestCanvasDelete:
    @pytest.mark.asyncio
    async def test_delete_canvas(self, auth_client: AsyncClient):
        create_resp = await auth_client.post("/api/canvases", json={"name": "要删除的"})
        canvas_id = create_resp.json()["canvas_id"]

        resp = await auth_client.delete(f"/api/canvases/{canvas_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent_canvas(self, auth_client: AsyncClient):
        resp = await auth_client.delete("/api/canvases/nonexistent-id")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_requires_auth(self, client: AsyncClient):
        resp = await client.delete("/api/canvases/any-id")
        assert resp.status_code == 401


class TestCanvasState:
    @pytest.mark.asyncio
    async def test_get_canvas_state(self, auth_client: AsyncClient):
        create_resp = await auth_client.post("/api/canvases", json={"name": "状态测试"})
        canvas_id = create_resp.json()["canvas_id"]

        resp = await auth_client.get(f"/api/canvases/{canvas_id}/state")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_get_nonexistent_canvas_state(self, auth_client: AsyncClient):
        resp = await auth_client.get("/api/canvases/nonexistent-id/state")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_canvas_state_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/canvases/any-id/state")
        assert resp.status_code == 401


class TestClusterPositions:
    @pytest.mark.asyncio
    async def test_save_position_nonexistent_request(self, auth_client: AsyncClient):
        resp = await auth_client.put(
            "/api/cluster-positions/nonexistent-req",
            json={"user_x": 100.0, "user_y": 200.0, "model_y": 300.0},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_save_position_requires_auth(self, client: AsyncClient):
        resp = await client.put(
            "/api/cluster-positions/any-req",
            json={"user_x": 0, "user_y": 0, "model_y": 0},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_save_position_default_values(self, auth_client: AsyncClient):
        """Omitting coordinates should use defaults (0.0)."""
        resp = await auth_client.put(
            "/api/cluster-positions/nonexistent-req",
            json={},
        )
        # 404 because request doesn't exist, but validation passed
        assert resp.status_code == 404
