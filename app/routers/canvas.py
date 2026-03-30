from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.auth import AuthError
from app.deps import (
    database,
    _get_request_user,
    _require_user,
    _unauthorized_json,
    _require_origin,
    _parse_json_body,
    OriginError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/canvases")
async def list_canvases(request: Request) -> JSONResponse:
    user = await _get_request_user(request)
    if not user:
        return _unauthorized_json()
    canvases = await database.get_canvases(user["user_id"])
    return JSONResponse({"canvases": canvases})


@router.post("/api/canvases")
async def create_canvas(request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await _require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    name = str(payload.get("name", "")).strip() or "新画布"
    canvas_id = await database.create_canvas(user["user_id"], name)
    return JSONResponse({"canvas_id": canvas_id, "name": name})


@router.patch("/api/canvases/{canvas_id}")
async def rename_canvas(canvas_id: str, request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await _require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    name = str(payload.get("name", "")).strip()
    if not name:
        return JSONResponse({"detail": "名称不能为空。"}, status_code=400)
    ok = await database.rename_canvas(canvas_id, user["user_id"], name)
    if not ok:
        return JSONResponse({"detail": "画布不存在。"}, status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/api/canvases/{canvas_id}")
async def delete_canvas(canvas_id: str, request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await _require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    ok = await database.delete_canvas(canvas_id, user["user_id"])
    if not ok:
        return JSONResponse({"detail": "画布不存在。"}, status_code=404)
    return JSONResponse({"ok": True})


@router.get("/api/canvases/{canvas_id}/state")
async def get_canvas_state(canvas_id: str, request: Request) -> JSONResponse:
    user = await _get_request_user(request)
    if not user:
        return _unauthorized_json()
    state = await database.get_canvas_state(canvas_id, user["user_id"])
    if state is None:
        return JSONResponse({"detail": "画布不存在。"}, status_code=404)
    return JSONResponse(state)


@router.put("/api/cluster-positions/{request_id}")
async def save_cluster_position(request_id: str, request: Request) -> JSONResponse:
    try:
        user = await _require_user(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        await _require_origin(request)
    except OriginError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    try:
        user_x = float(payload.get("user_x", 0))
        user_y = float(payload.get("user_y", 0))
        model_y = float(payload.get("model_y", 0))
    except (TypeError, ValueError):
        return JSONResponse({"detail": "坐标格式错误。"}, status_code=400)
    ok = await database.upsert_cluster_position(request_id, user["user_id"], user_x, user_y, model_y)
    if not ok:
        return JSONResponse({"detail": "请求不存在或无权限。"}, status_code=404)
    return JSONResponse({"ok": True})
