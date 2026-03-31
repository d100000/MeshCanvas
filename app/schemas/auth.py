"""Auth-related request / response models."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ── Requests ─────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str
    password: str
    captcha_token: str = ""
    captcha_answer: str = ""
    website: str = ""  # honeypot field


class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_token: str = ""
    captcha_answer: str = ""
    website: str = ""  # honeypot field


# ── Responses ────────────────────────────────────────────────────────────────

class AuthOkResponse(BaseModel):
    ok: bool = True
    username: str


class SessionResponse(BaseModel):
    authenticated: bool
    username: Optional[str] = None


class RegistrationStatusResponse(BaseModel):
    allow: bool


class CaptchaResponse(BaseModel):
    question: str
    token: str
