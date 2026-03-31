from __future__ import annotations

import asyncio
import hmac
import json as _json
import logging
import math
import secrets
import sqlite3
from time import monotonic

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.deps import (
    database,
    auth_manager,
    rate_limiter,
    request_logger,
    _require_admin,
    _get_admin_user,
    _parse_json_body,
    _is_origin_allowed,
    _emit_admin_audit,
    _log_login_failure,
    _log_login_success,
    _read_admin_session_login_form,
    _request_is_https,
    _load_global_service_settings,
    _create_llm_client_from_settings,
    _sanitize_extra_headers,
    _summarize_extra_headers,
    _build_openai_default_headers,
    _mask_key,
    _pick_saved_model_for_test,
    _parse_optional_user_id_query,
    _normalize_system_config_value,
    _require_origin,
    _http_log_tasks,
    _ADMIN_RECHARGE_REMARK_MAX,
    _PRICING_POINTS_MAX,
    _CONFIG_NUM_MAX,
    _RECHARGE_POINTS_ABS_MAX,
    _ADMIN_MODEL_TEST_TIMEOUT_SECONDS,
    _ADMIN_MODEL_TEST_MAX_PREVIEW_CHARS,
    _ADMIN_MODEL_TEST_MAX_TOKENS,
    _ADMIN_MODEL_TEST_RATE_LIMIT,
    ADMIN_SESSION_COOKIE_NAME,
    OriginError,
    _render_admin_login_html,
)
from app.auth import AuthError, AuthManager, ADMIN_SESSION_COOKIE_NAME, SESSION_DAYS
from app.captcha import verify as captcha_verify, check_honeypot
from app.llm_client import create_llm_client
from app.schemas.admin import (
    AdminLoginRequest,
    RechargeRequest,
    SetRoleRequest,
    ResetPasswordRequest,
    ChangePasswordRequest,
    PricingRequest,
    ModelConfigRequest,
    ModelConfigTestRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/admin/session-login")
async def admin_session_login(request: Request) -> Response:
    """浏览器原生表单登录：303 重定向 + Set-Cookie，避免 fetch 跳转时部分环境下 Cookie 未生效。"""
    client_host = request.client.host if request.client else "unknown"
    try:
        await _require_origin(request)
    except OriginError:
        _log_login_failure(
            route="/admin/session-login",
            client_host=client_host,
            username="-",
            reason="origin_denied",
        )
        return RedirectResponse(url="/admin?error=origin", status_code=303)
    if not await rate_limiter.allow_async(f"admin-login:{client_host}", limit=10, window_seconds=600):
        _log_login_failure(
            route="/admin/session-login",
            client_host=client_host,
            username="-",
            reason="rate_limited",
        )
        return RedirectResponse(url="/admin?error=rate", status_code=303)
    try:
        form_data = await _read_admin_session_login_form(request)
    except Exception:
        logger.exception("admin session-login: failed to read form body")
        return RedirectResponse(url="/admin?error=form", status_code=303)

    username = form_data.get("username", "")
    password = form_data.get("password", "")

    # --- captcha / honeypot ---
    if check_honeypot(form_data.get("website")):
        return RedirectResponse(url="/admin?error=badcreds", status_code=303)
    cap_err = captcha_verify(form_data.get("captcha_token", ""), form_data.get("captcha_answer", ""))
    if cap_err:
        return RedirectResponse(url="/admin?error=captcha", status_code=303)
    # --- end captcha ---

    try:
        _user_info, token, _ = await auth_manager.admin_login(username, password)
    except AuthError as exc:
        detail = str(exc)
        if "管理员权限" in detail:
            code = "forbidden"
        elif "用户名需为" in detail or "密码长度" in detail or "至少需要" in detail:
            code = "invalid"
        else:
            code = "badcreds"
        _log_login_failure(
            route="/admin/session-login",
            client_host=client_host,
            username=username,
            reason=f"{code}:{detail}",
        )
        return RedirectResponse(url=f"/admin?error={code}", status_code=303)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        logger.exception("admin session-login: database or filesystem error (%s)", exc)
        return RedirectResponse(url="/admin?error=db", status_code=303)
    except Exception:
        logger.exception("admin session-login failed")
        return RedirectResponse(url="/admin?error=server", status_code=303)

    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
        max_age=SESSION_DAYS * 24 * 60 * 60,
        path="/",
    )
    return response


@router.post("/api/admin/login")
async def admin_login(request: Request, body: AdminLoginRequest) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    if not await rate_limiter.allow_async(f"admin-login:{client_host}", limit=10, window_seconds=600):
        _log_login_failure(
            route="/api/admin/login",
            client_host=client_host,
            username="-",
            reason="rate_limited",
        )
        return JSONResponse({"detail": "登录过于频繁。"}, status_code=429)
    try:
        # --- captcha / honeypot ---
        if check_honeypot(body.website):
            return JSONResponse({"detail": "请求异常。"}, status_code=400)
        cap_err = captcha_verify(body.captcha_token, body.captcha_answer)
        if cap_err:
            return JSONResponse({"detail": cap_err}, status_code=400)
        # --- end captcha ---
        user_info, token, _ = await auth_manager.admin_login(body.username, body.password)
    except AuthError as exc:
        _log_login_failure(
            route="/api/admin/login",
            client_host=client_host,
            username=body.username,
            reason=str(exc),
        )
        return JSONResponse({"detail": str(exc)}, status_code=400)
    response = JSONResponse({"ok": True, "username": user_info["username"]})
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME, value=token, httponly=True,
        samesite="lax", secure=_request_is_https(request),
        max_age=SESSION_DAYS * 24 * 60 * 60, path="/",
    )
    return response


@router.post("/api/admin/logout")
async def admin_logout(request: Request) -> JSONResponse:
    origin = request.headers.get("origin")
    if not _is_origin_allowed(origin, request.headers.get("host")):
        return JSONResponse({"detail": "非法来源。"}, status_code=403)
    await auth_manager.logout(request.cookies.get(ADMIN_SESSION_COOKIE_NAME))
    response = JSONResponse({"ok": True})
    response.delete_cookie(key=ADMIN_SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/api/admin/users")
async def admin_list_users(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    users = await database.list_users_admin()
    return JSONResponse({"users": users})


@router.post("/api/admin/recharge")
async def admin_recharge(request: Request, body: RechargeRequest) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if not math.isfinite(body.points) or abs(body.points) > _RECHARGE_POINTS_ABS_MAX:
        return JSONResponse({"detail": "点数无效或超出允许范围。"}, status_code=400)
    remark = body.remark.strip()
    if len(remark) > _ADMIN_RECHARGE_REMARK_MAX:
        remark = remark[:_ADMIN_RECHARGE_REMARK_MAX]
    if body.user_id <= 0 or body.points == 0:
        return JSONResponse({"detail": "用户 ID 和点数不能为空。"}, status_code=400)
    if not await database.get_user_by_id(body.user_id):
        return JSONResponse({"detail": "用户不存在。"}, status_code=404)
    ok = await database.add_points_non_negative(body.user_id, body.points, admin["user_id"], remark)
    if not ok:
        current_balance = await database.get_user_balance(body.user_id)
        return JSONResponse(
            {"detail": f"余额不足，当前余额 {current_balance:.2f}，无法扣减 {abs(body.points):.2f}。"},
            status_code=400,
        )
    new_balance = await database.get_user_balance(body.user_id)
    _emit_admin_audit(
        admin["user_id"], "recharge",
        target_user_id=body.user_id,
        points=body.points, remark=remark, new_balance=round(new_balance, 2),
    )
    return JSONResponse({"ok": True, "balance": new_balance})


@router.post("/api/admin/set-role")
async def admin_set_role(request: Request, body: SetRoleRequest) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if body.user_id <= 0:
        return JSONResponse({"detail": "用户 ID 无效。"}, status_code=400)
    target = await database.get_user_by_id(body.user_id)
    if not target:
        return JSONResponse({"detail": "用户不存在。"}, status_code=404)
    if body.user_id == admin["user_id"] and body.role == "user":
        return JSONResponse({"detail": "不能取消自己的管理员权限。"}, status_code=400)
    current_role = str(target.get("role") or "user")
    if current_role == "admin" and body.role == "user":
        if await database.count_users_with_role("admin") <= 1:
            return JSONResponse({"detail": "至少需要保留一名管理员。"}, status_code=400)
    await database.set_user_role(body.user_id, body.role)
    _emit_admin_audit(
        admin["user_id"], "set_role",
        target_user_id=body.user_id,
        new_role=body.role, prev_role=current_role,
    )
    return JSONResponse({"ok": True})


@router.post("/api/admin/reset-password")
async def admin_reset_password(request: Request, body: ResetPasswordRequest) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if body.user_id <= 0:
        return JSONResponse({"detail": "用户 ID 无效。"}, status_code=400)
    target = await database.get_user_by_id(body.user_id)
    if not target:
        return JSONResponse({"detail": "用户不存在。"}, status_code=404)
    username = target.get("username", "")
    salt = secrets.token_hex(16)
    password_hash = await asyncio.to_thread(AuthManager._hash_password, body.new_password, salt)
    await database.update_user_password(username, password_hash, salt)
    _emit_admin_audit(
        admin["user_id"], "reset_password",
        target_user_id=body.user_id,
    )
    return JSONResponse({"ok": True})


@router.post("/api/admin/change-password")
async def admin_change_password(request: Request, body: ChangePasswordRequest) -> JSONResponse:
    """管理员修改自己的密码。需要验证旧密码，成功后自动注销当前会话。"""
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)

    # 验证旧密码
    user_record = await database.get_user_by_username(admin["username"])
    if not user_record:
        return JSONResponse({"detail": "用户不存在。"}, status_code=404)
    expected_hash = await asyncio.to_thread(
        AuthManager._hash_password, body.old_password, user_record["password_salt"]
    )
    if not hmac.compare_digest(expected_hash, user_record["password_hash"]):
        return JSONResponse({"detail": "当前密码错误。"}, status_code=403)

    # 更新密码
    salt = secrets.token_hex(16)
    password_hash = await asyncio.to_thread(AuthManager._hash_password, body.new_password, salt)
    await database.update_user_password(admin["username"], password_hash, salt)

    # 审计日志
    _emit_admin_audit(
        admin["user_id"], "change_own_password",
    )

    # 注销当前管理员会话
    await auth_manager.logout(request.cookies.get(ADMIN_SESSION_COOKIE_NAME))
    response = JSONResponse({"ok": True})
    response.delete_cookie(key=ADMIN_SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/api/admin/pricing")
async def admin_get_pricing(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    pricing = await database.get_all_pricing()
    return JSONResponse({"pricing": pricing})


@router.put("/api/admin/pricing")
async def admin_update_pricing(request: Request, body: PricingRequest) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if (
        not math.isfinite(body.input_points_per_1k)
        or not math.isfinite(body.output_points_per_1k)
        or body.input_points_per_1k > _PRICING_POINTS_MAX
        or body.output_points_per_1k > _PRICING_POINTS_MAX
    ):
        return JSONResponse({"detail": "单价须为有限非负数且不超过上限。"}, status_code=400)
    display_name = body.display_name.strip() or body.model_id.strip()
    await database.upsert_pricing(body.model_id, display_name, body.input_points_per_1k, body.output_points_per_1k, body.is_active)
    _emit_admin_audit(
        admin["user_id"], "upsert_pricing",
        model_id=body.model_id, input_per_1k=body.input_points_per_1k, output_per_1k=body.output_points_per_1k,
    )
    return JSONResponse({"ok": True})


@router.delete("/api/admin/pricing/{model_id}")
async def admin_delete_pricing(model_id: str, request: Request) -> JSONResponse:
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    await database.delete_pricing(model_id)
    _emit_admin_audit(admin["user_id"], "delete_pricing", model_id=model_id)
    return JSONResponse({"ok": True})


@router.get("/api/admin/usage")
async def admin_usage_stats(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        uid = _parse_optional_user_id_query(request.query_params.get("user_id"))
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    stats = await database.get_usage_stats(uid)
    return JSONResponse({"stats": stats})


@router.get("/api/admin/recharge-logs")
async def admin_recharge_logs(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        uid = _parse_optional_user_id_query(request.query_params.get("user_id"))
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    logs = await database.get_recharge_logs(uid)
    return JSONResponse({"logs": logs})


@router.get("/api/admin/config")
async def admin_get_config(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    cfg = await database.get_system_config()
    return JSONResponse({"config": cfg})


@router.put("/api/admin/config")
async def admin_update_config(request: Request) -> JSONResponse:
    """Update system config. Still uses manual parsing because the allowlist
    filter + normalize logic is tightly coupled to the existing helper."""
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        payload = await _parse_json_body(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    allowed_keys = {"config_default_points", "config_low_balance_threshold", "config_allow_registration", "config_search_points_per_call"}
    changes: dict[str, str] = {}
    for key, value in payload.items():
        if key not in allowed_keys:
            continue
        normalized = _normalize_system_config_value(key, value)
        if normalized is None:
            return JSONResponse({"detail": f"配置项 {key} 的值无效。"}, status_code=400)
        await database.set_system_config(key, normalized)
        changes[key] = normalized
    if changes:
        _emit_admin_audit(admin["user_id"], "update_config", **changes)
    return JSONResponse({"ok": True})


@router.get("/api/admin/audit-logs")
async def admin_get_audit_logs(request: Request) -> JSONResponse:
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    try:
        limit = min(int(request.query_params.get("limit", "200")), 500)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        return JSONResponse({"detail": "limit/offset 须为整数。"}, status_code=400)
    action_filter = request.query_params.get("action") or None
    logs = await database.get_admin_audit_logs(limit=limit, offset=offset, action_filter=action_filter)
    return JSONResponse({"logs": logs})


@router.get("/api/admin/model-config")
async def admin_get_model_config(request: Request) -> JSONResponse:
    """读取全局模型/API/搜索配置（API Key 脱敏）。"""
    try:
        await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    gs = await _load_global_service_settings()
    return JSONResponse({
        "api_base_url": gs.get("api_base_url", ""),
        "api_format": gs.get("api_format", "openai"),
        "api_key_masked": _mask_key(gs.get("api_key", "")),
        "models": gs.get("models", []),
        "firecrawl_api_key_masked": _mask_key(gs.get("firecrawl_api_key", "")),
        "firecrawl_country": gs.get("firecrawl_country", "CN"),
        "firecrawl_timeout_ms": gs.get("firecrawl_timeout_ms", 45000),
        "preprocess_model": gs.get("preprocess_model", ""),
        "user_api_base_url": gs.get("user_api_base_url", ""),
        "user_api_format": gs.get("user_api_format", "openai"),
        "extra_params": gs.get("extra_params", {}),
        "extra_headers": _sanitize_extra_headers(gs.get("extra_headers", {})),
    })


@router.put("/api/admin/model-config")
async def admin_update_model_config(request: Request, body: ModelConfigRequest) -> JSONResponse:
    """更新全局模型/API/搜索配置。"""
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)

    api_base_url = body.api_base_url.strip()
    if not api_base_url:
        return JSONResponse({"detail": "请填写 API 地址。"}, status_code=400)

    models = [{"name": m.name.strip(), "id": m.id.strip()} for m in body.models if m.name.strip() and m.id.strip()]
    if not models:
        return JSONResponse({"detail": "请至少添加一个模型。"}, status_code=400)

    # 留空 key 时保留已有值
    api_key_raw = body.api_key.strip()
    firecrawl_key_raw = body.firecrawl_api_key.strip()
    existing = await _load_global_service_settings()
    if not api_key_raw:
        api_key_raw = existing.get("api_key", "")
    if not api_key_raw:
        return JSONResponse({"detail": "请填写 API Key。"}, status_code=400)
    if not firecrawl_key_raw:
        firecrawl_key_raw = existing.get("firecrawl_api_key", "")

    extra_headers_raw = _sanitize_extra_headers(body.extra_headers)

    await database.set_global_model_config(
        api_base_url=api_base_url,
        api_format=body.api_format,
        api_key=api_key_raw,
        models_json=_json.dumps(models, ensure_ascii=False),
        firecrawl_api_key=firecrawl_key_raw,
        firecrawl_country=body.firecrawl_country or "CN",
        firecrawl_timeout_ms=body.firecrawl_timeout_ms,
        preprocess_model=body.preprocess_model.strip(),
        user_api_base_url=body.user_api_base_url.strip(),
        user_api_format=body.user_api_format,
        extra_params=body.extra_params,
        extra_headers=extra_headers_raw,
    )
    _emit_admin_audit(
        admin["user_id"], "update_model_config",
        api_base_url=api_base_url, api_format=body.api_format, model_count=len(models),
        preprocess_model=body.preprocess_model.strip(),
        extra_headers=_summarize_extra_headers(extra_headers_raw),
    )
    return JSONResponse({"ok": True})


@router.post("/api/admin/model-config/test")
async def admin_test_model_config(request: Request, body: ModelConfigTestRequest) -> JSONResponse:
    client_host = request.client.host if request.client else "unknown"
    try:
        admin = await _require_admin(request)
    except AuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if not await rate_limiter.allow_async(
        f"admin-model-test:{admin['user_id']}:{client_host}",
        limit=_ADMIN_MODEL_TEST_RATE_LIMIT,
        window_seconds=60,
    ):
        return JSONResponse({"detail": "测试过于频繁，请稍后再试。"}, status_code=429)

    model_name = body.model_name.strip()
    model_id = body.model_id.strip()
    gs = await _load_global_service_settings()
    api_key = str(gs.get("api_key", "")).strip()
    api_base_url = str(gs.get("api_base_url", "")).strip()
    if not api_base_url or not api_key:
        return JSONResponse({"detail": "请先保存 API 地址和 API Key 后再测试。"}, status_code=400)

    selected = _pick_saved_model_for_test(gs.get("models", []), model_name=model_name, model_id=model_id)
    if not selected:
        return JSONResponse({"detail": "模型不存在或尚未保存，请先保存模型配置。"}, status_code=400)
    selected_name, selected_id = selected

    llm = create_llm_client(
        gs.get("api_format", "openai"),
        api_key=api_key,
        base_url=api_base_url,
        default_headers=_build_openai_default_headers(gs.get("extra_headers", {})),
    )
    started_at = monotonic()
    try:
        llm_resp = await asyncio.wait_for(
            llm.complete(
                model=selected_id,
                messages=[
                    {"role": "system", "content": "You are a concise assistant."},
                    {"role": "user", "content": "Reply with exactly: pong"},
                ],
                temperature=0,
                max_tokens=_ADMIN_MODEL_TEST_MAX_TOKENS,
            ),
            timeout=_ADMIN_MODEL_TEST_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        _emit_admin_audit(
            admin["user_id"],
            "test_model_connectivity",
            model_name=selected_name,
            model_id=selected_id,
            status="timeout",
            timeout_s=_ADMIN_MODEL_TEST_TIMEOUT_SECONDS,
        )
        return JSONResponse(
            {"detail": f"模型测试超时（>{_ADMIN_MODEL_TEST_TIMEOUT_SECONDS}s），请检查接口连通性。"},
            status_code=504,
        )
    except Exception as exc:
        error_type = exc.__class__.__name__
        logger.warning(
            "admin model connectivity test failed model_id=%s error_type=%s",
            selected_id,
            error_type,
            exc_info=True,
        )
        _emit_admin_audit(
            admin["user_id"],
            "test_model_connectivity",
            model_name=selected_name,
            model_id=selected_id,
            status="failed",
            error_type=error_type,
        )
        return JSONResponse(
            {"detail": "模型接口调用失败，请检查 API 地址、API Key 与模型 ID 是否有效。"},
            status_code=502,
        )

    latency_ms = int(max(0, round((monotonic() - started_at) * 1000)))
    preview = llm_resp.text
    if len(preview) > _ADMIN_MODEL_TEST_MAX_PREVIEW_CHARS:
        preview = preview[: _ADMIN_MODEL_TEST_MAX_PREVIEW_CHARS - 1] + "…"
    usage = {
        "prompt_tokens": llm_resp.usage.prompt_tokens,
        "completion_tokens": llm_resp.usage.completion_tokens,
        "total_tokens": llm_resp.usage.total_tokens,
    } if llm_resp.usage.total_tokens > 0 else None

    audit_detail: dict[str, object] = {
        "model_name": selected_name,
        "model_id": selected_id,
        "status": "ok",
        "latency_ms": latency_ms,
        "preview_length": len(preview),
    }
    if usage:
        audit_detail.update(usage)
    _emit_admin_audit(admin["user_id"], "test_model_connectivity", **audit_detail)

    return JSONResponse(
        {
            "ok": True,
            "model_name": selected_name,
            "model_id": selected_id,
            "latency_ms": latency_ms,
            "preview": preview,
            "usage": usage,
        }
    )
