"""Simple arithmetic CAPTCHA with HMAC-signed tokens.

Three layers of bot protection:
1. Arithmetic challenge (addition / multiplication, result ≤ 100)
2. Honeypot hidden field (bots auto-fill, humans don't see it)
3. Minimum submission time (token must be ≥ 2 s old)
"""

from __future__ import annotations

import hashlib
import hmac
import random
import secrets
import time

# Signing key – regenerated on each process start, which also
# invalidates any captcha tokens from a previous run.
_SECRET: bytes = secrets.token_bytes(32)

# Captcha validity window in seconds.
MAX_AGE = 300  # 5 min
MIN_AGE = 2    # anti-bot speed gate


def generate() -> tuple[str, str]:
    """Return *(question, token)*.

    ``question`` is a human-readable math expression such as ``"12 + 7 = ?"``.
    ``token`` is an opaque string that the client must send back together with
    the user's numeric answer so the server can verify correctness without
    storing any state.
    """
    op = random.choice(("+", "×"))
    if op == "+":
        a, b = random.randint(1, 50), random.randint(1, 49)
        answer = a + b
    else:
        a, b = random.randint(2, 12), random.randint(2, 9)
        answer = a * b
    question = f"{a} {op} {b} = ?"
    token = _make_token(answer)
    return question, token


def verify(token: str, user_answer: str) -> str | None:
    """Return *None* on success, or an error message string on failure."""
    try:
        parts = token.split("|")
        if len(parts) != 4:
            return "验证码无效，请刷新重试。"
        correct_str, ts_str, _nonce, sig = parts

        # Signature check
        payload = f"{correct_str}|{ts_str}|{_nonce}"
        expected = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return "验证码无效，请刷新重试。"

        # Expiry
        age = time.time() - int(ts_str)
        if age > MAX_AGE:
            return "验证码已过期，请刷新重试。"
        if age < MIN_AGE:
            return "提交过快，请稍后再试。"

        # Answer
        if int(user_answer.strip()) != int(correct_str):
            return "验证码答案错误。"

    except (ValueError, TypeError, AttributeError):
        return "验证码无效，请刷新重试。"

    return None  # success


def check_honeypot(value: str | None) -> bool:
    """Return *True* if the honeypot field is suspiciously filled."""
    return bool(value)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _make_token(answer: int) -> str:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    payload = f"{answer}|{ts}|{nonce}"
    sig = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"
