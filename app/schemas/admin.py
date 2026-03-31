"""Admin-related request / response models."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ── Requests ─────────────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    username: str
    password: str
    captcha_token: str = ""
    captcha_answer: str = ""
    website: str = ""  # honeypot


class RechargeRequest(BaseModel):
    user_id: int
    points: float
    remark: str = Field(default="", max_length=200)


class SetRoleRequest(BaseModel):
    user_id: int
    role: str = "user"

    @field_validator("role")
    @classmethod
    def role_must_be_valid(cls, v: str) -> str:
        if v not in ("user", "admin"):
            raise ValueError("role must be 'user' or 'admin'")
        return v


class ResetPasswordRequest(BaseModel):
    user_id: int
    new_password: str = Field(..., min_length=8)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)


class ModelItem(BaseModel):
    name: str
    id: str


class ModelConfigRequest(BaseModel):
    api_base_url: str
    api_format: str = "openai"
    api_key: str = ""
    models: List[ModelItem] = Field(..., min_length=1)
    firecrawl_api_key: str = ""
    firecrawl_country: str = "CN"
    firecrawl_timeout_ms: int = Field(default=45000, ge=5000, le=120000)
    preprocess_model: str = ""
    user_api_base_url: str = ""
    user_api_format: str = "openai"
    extra_params: Dict[str, Any] = Field(default_factory=dict)
    extra_headers: Dict[str, str] = Field(default_factory=dict)

    @field_validator("api_format", "user_api_format")
    @classmethod
    def format_must_be_valid(cls, v: str) -> str:
        if v not in ("openai", "anthropic"):
            raise ValueError("api_format must be 'openai' or 'anthropic'")
        return v


class ModelConfigTestRequest(BaseModel):
    model_name: str = ""
    model_id: str = ""


class PricingRequest(BaseModel):
    model_id: str = Field(..., min_length=1)
    display_name: str = ""
    input_points_per_1k: float = Field(default=1.0, ge=0)
    output_points_per_1k: float = Field(default=2.0, ge=0)
    is_active: int = Field(default=1, ge=0, le=1)


_CONFIG_ALLOWLIST = frozenset({
    "config_default_points",
    "config_low_balance_threshold",
    "config_allow_registration",
    "config_search_points_per_call",
})


class SystemConfigRequest(BaseModel):
    """Accept only allowlisted config keys; extras are silently dropped."""
    config_default_points: Optional[str] = None
    config_low_balance_threshold: Optional[str] = None
    config_allow_registration: Optional[str] = None
    config_search_points_per_call: Optional[str] = None

    def to_update_dict(self) -> Dict[str, str]:
        """Return only the keys that were explicitly provided."""
        return {k: str(v) for k, v in self.dict(exclude_none=True).items()}
