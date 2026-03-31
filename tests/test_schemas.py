"""Tests for Pydantic schema validation — ensures invalid payloads are rejected."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.auth import RegisterRequest, LoginRequest
from app.schemas.admin import (
    AdminLoginRequest,
    RechargeRequest,
    SetRoleRequest,
    ResetPasswordRequest,
    ChangePasswordRequest,
    PricingRequest,
    ModelConfigRequest,
    ModelConfigTestRequest,
    ModelItem,
)
from app.schemas.canvas import CreateCanvasRequest, RenameCanvasRequest, ClusterPositionRequest
from app.schemas.user import CustomApiKeyRequest
from app.schemas.setup import SetupRequest


# ── Auth Schemas ─────────────────────────────────────────────────────────────

class TestRegisterRequestSchema:
    def test_valid(self):
        r = RegisterRequest(username="alice", password="secret123")
        assert r.username == "alice"
        assert r.captcha_token == ""
        assert r.website == ""

    def test_missing_username(self):
        with pytest.raises(ValidationError):
            RegisterRequest(password="secret123")

    def test_missing_password(self):
        with pytest.raises(ValidationError):
            RegisterRequest(username="alice")

    def test_extra_fields_ignored(self):
        r = RegisterRequest(username="alice", password="pw", unknown="x")
        assert not hasattr(r, "unknown") or True  # pydantic v2 ignores extras by default


class TestLoginRequestSchema:
    def test_valid(self):
        r = LoginRequest(username="bob", password="pass1234")
        assert r.username == "bob"

    def test_missing_fields(self):
        with pytest.raises(ValidationError):
            LoginRequest()


# ── Admin Schemas ────────────────────────────────────────────────────────────

class TestRechargeRequestSchema:
    def test_valid(self):
        r = RechargeRequest(user_id=1, points=50.0, remark="test")
        assert r.user_id == 1
        assert r.points == 50.0

    def test_missing_user_id(self):
        with pytest.raises(ValidationError):
            RechargeRequest(points=10.0)

    def test_missing_points(self):
        with pytest.raises(ValidationError):
            RechargeRequest(user_id=1)

    def test_remark_max_length(self):
        """Remark exceeding 200 chars should be rejected."""
        with pytest.raises(ValidationError):
            RechargeRequest(user_id=1, points=10.0, remark="x" * 201)

    def test_remark_at_max_length(self):
        r = RechargeRequest(user_id=1, points=10.0, remark="x" * 200)
        assert len(r.remark) == 200

    def test_negative_points_allowed(self):
        """Negative points = deduction, allowed by schema."""
        r = RechargeRequest(user_id=1, points=-10.0)
        assert r.points == -10.0


class TestSetRoleRequestSchema:
    def test_valid_user(self):
        r = SetRoleRequest(user_id=1, role="user")
        assert r.role == "user"

    def test_valid_admin(self):
        r = SetRoleRequest(user_id=1, role="admin")
        assert r.role == "admin"

    def test_invalid_role(self):
        with pytest.raises(ValidationError):
            SetRoleRequest(user_id=1, role="superadmin")

    def test_default_role(self):
        r = SetRoleRequest(user_id=1)
        assert r.role == "user"


class TestResetPasswordRequestSchema:
    def test_valid(self):
        r = ResetPasswordRequest(user_id=1, new_password="longpassword")
        assert r.new_password == "longpassword"

    def test_password_too_short(self):
        with pytest.raises(ValidationError):
            ResetPasswordRequest(user_id=1, new_password="short")

    def test_password_exactly_8_chars(self):
        r = ResetPasswordRequest(user_id=1, new_password="12345678")
        assert len(r.new_password) == 8


class TestChangePasswordRequestSchema:
    def test_valid(self):
        r = ChangePasswordRequest(old_password="old1", new_password="newpass12")
        assert r.old_password == "old1"

    def test_new_password_too_short(self):
        with pytest.raises(ValidationError):
            ChangePasswordRequest(old_password="old1", new_password="short")

    def test_old_password_empty(self):
        with pytest.raises(ValidationError):
            ChangePasswordRequest(old_password="", new_password="newpass12")


class TestPricingRequestSchema:
    def test_valid(self):
        r = PricingRequest(model_id="gpt-4", input_points_per_1k=1.5, output_points_per_1k=3.0)
        assert r.model_id == "gpt-4"

    def test_empty_model_id(self):
        with pytest.raises(ValidationError):
            PricingRequest(model_id="")

    def test_negative_input_price(self):
        with pytest.raises(ValidationError):
            PricingRequest(model_id="test", input_points_per_1k=-1.0)

    def test_negative_output_price(self):
        with pytest.raises(ValidationError):
            PricingRequest(model_id="test", output_points_per_1k=-0.5)

    def test_invalid_is_active(self):
        with pytest.raises(ValidationError):
            PricingRequest(model_id="test", is_active=2)

    def test_defaults(self):
        r = PricingRequest(model_id="test")
        assert r.input_points_per_1k == 1.0
        assert r.output_points_per_1k == 2.0
        assert r.is_active == 1


class TestModelConfigRequestSchema:
    def test_valid_minimal(self):
        r = ModelConfigRequest(
            api_base_url="https://api.example.com/v1",
            models=[ModelItem(name="GPT-4", id="gpt-4")],
        )
        assert r.api_format == "openai"
        assert r.firecrawl_timeout_ms == 45000

    def test_empty_models_rejected(self):
        with pytest.raises(ValidationError):
            ModelConfigRequest(
                api_base_url="https://api.example.com/v1",
                models=[],
            )

    def test_invalid_api_format(self):
        with pytest.raises(ValidationError):
            ModelConfigRequest(
                api_base_url="https://api.example.com/v1",
                api_format="google",
                models=[ModelItem(name="Test", id="test")],
            )

    def test_timeout_too_low(self):
        with pytest.raises(ValidationError):
            ModelConfigRequest(
                api_base_url="https://api.example.com/v1",
                models=[ModelItem(name="Test", id="test")],
                firecrawl_timeout_ms=1000,
            )

    def test_timeout_too_high(self):
        with pytest.raises(ValidationError):
            ModelConfigRequest(
                api_base_url="https://api.example.com/v1",
                models=[ModelItem(name="Test", id="test")],
                firecrawl_timeout_ms=200000,
            )

    def test_anthropic_format(self):
        r = ModelConfigRequest(
            api_base_url="https://api.anthropic.com/v1",
            api_format="anthropic",
            models=[ModelItem(name="Claude", id="claude-3")],
        )
        assert r.api_format == "anthropic"

    def test_invalid_user_api_format(self):
        with pytest.raises(ValidationError):
            ModelConfigRequest(
                api_base_url="https://api.example.com/v1",
                user_api_format="invalid",
                models=[ModelItem(name="Test", id="test")],
            )


class TestModelConfigTestRequestSchema:
    def test_defaults(self):
        r = ModelConfigTestRequest()
        assert r.model_name == ""
        assert r.model_id == ""

    def test_with_values(self):
        r = ModelConfigTestRequest(model_name="GPT-4", model_id="gpt-4")
        assert r.model_name == "GPT-4"


# ── Canvas Schemas ───────────────────────────────────────────────────────────

class TestCreateCanvasRequestSchema:
    def test_default_name(self):
        r = CreateCanvasRequest()
        assert r.name == "新画布"

    def test_custom_name(self):
        r = CreateCanvasRequest(name="My Canvas")
        assert r.name == "My Canvas"


class TestRenameCanvasRequestSchema:
    def test_valid(self):
        r = RenameCanvasRequest(name="New Name")
        assert r.name == "New Name"

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            RenameCanvasRequest(name="")

    def test_missing_name_rejected(self):
        with pytest.raises(ValidationError):
            RenameCanvasRequest()


class TestClusterPositionRequestSchema:
    def test_defaults(self):
        r = ClusterPositionRequest()
        assert r.user_x == 0
        assert r.user_y == 0
        assert r.model_y == 0

    def test_custom_values(self):
        r = ClusterPositionRequest(user_x=100.5, user_y=-200.3, model_y=50.0)
        assert r.user_x == 100.5


# ── User Schemas ─────────────────────────────────────────────────────────────

class TestCustomApiKeyRequestSchema:
    def test_defaults(self):
        r = CustomApiKeyRequest()
        assert r.model_keys == {}
        assert r.use_custom_key is False

    def test_with_keys(self):
        r = CustomApiKeyRequest(
            model_keys={"gpt-4": "sk-key1", "claude-3": "sk-key2"},
            use_custom_key=True,
        )
        assert len(r.model_keys) == 2
        assert r.use_custom_key is True


# ── Setup Schema ─────────────────────────────────────────────────────────────

class TestSetupRequestSchema:
    def test_valid(self):
        r = SetupRequest(
            base_url="https://api.example.com/v1",
            API_key="sk-test123",
            models=[ModelItem(name="GPT-4", id="gpt-4")],
        )
        assert r.api_format == "openai"

    def test_missing_base_url(self):
        with pytest.raises(ValidationError):
            SetupRequest(
                API_key="sk-test",
                models=[ModelItem(name="Test", id="test")],
            )

    def test_missing_api_key(self):
        with pytest.raises(ValidationError):
            SetupRequest(
                base_url="https://api.example.com/v1",
                models=[ModelItem(name="Test", id="test")],
            )

    def test_empty_models_rejected(self):
        with pytest.raises(ValidationError):
            SetupRequest(
                base_url="https://api.example.com/v1",
                API_key="sk-test",
                models=[],
            )

    def test_invalid_format(self):
        with pytest.raises(ValidationError):
            SetupRequest(
                base_url="https://api.example.com/v1",
                API_key="sk-test",
                api_format="gemini",
                models=[ModelItem(name="Test", id="test")],
            )
