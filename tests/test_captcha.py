"""Tests for the CAPTCHA module (app.captcha)."""

from __future__ import annotations

import time

import pytest

from app.captcha import generate, verify, check_honeypot, _SECRET, MIN_AGE, MAX_AGE


class TestGenerate:
    def test_returns_question_and_token(self):
        question, token = generate()
        assert isinstance(question, str)
        assert isinstance(token, str)
        assert "= ?" in question

    def test_question_format_addition(self):
        """Generate many captchas and verify addition format."""
        for _ in range(200):
            q, _ = generate()
            assert "= ?" in q
            # Should contain either + or ×
            assert "+" in q or "×" in q

    def test_answer_never_exceeds_100(self):
        """Critical: all generated answers must be ≤ 100."""
        for _ in range(5000):
            q, _ = generate()
            import re
            m = re.match(r"(\d+)\s*([+×])\s*(\d+)", q)
            assert m, f"Cannot parse: {q}"
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            answer = a + b if op == "+" else a * b
            assert answer <= 100, f"Answer {answer} > 100 for {q}"

    def test_token_has_three_parts(self):
        """Token must be ts|nonce|sig — never expose answer."""
        _, token = generate()
        parts = token.split("|")
        assert len(parts) == 3, f"Token has {len(parts)} parts: {token}"

    def test_token_does_not_contain_answer(self):
        """Security: token must NOT contain the answer in plaintext."""
        for _ in range(100):
            q, token = generate()
            import re
            m = re.match(r"(\d+)\s*([+×])\s*(\d+)", q)
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            answer = str(a + b if op == "+" else a * b)
            # The first part of token is timestamp, not the answer
            ts_part = token.split("|")[0]
            assert ts_part != answer, "Token first part looks like the answer!"


class TestVerify:
    def test_correct_answer_succeeds(self):
        """Verify returns None on correct answer (after MIN_AGE bypass)."""
        import app.captcha as mod
        orig = mod.MIN_AGE
        mod.MIN_AGE = 0
        try:
            q, token = generate()
            answer = _solve(q)
            result = verify(token, str(answer))
            assert result is None, f"Expected None, got: {result}"
        finally:
            mod.MIN_AGE = orig

    def test_wrong_answer_fails(self):
        import app.captcha as mod
        orig = mod.MIN_AGE
        mod.MIN_AGE = 0
        try:
            q, token = generate()
            answer = _solve(q)
            result = verify(token, str(answer + 1))
            assert result is not None
            assert "错误" in result
        finally:
            mod.MIN_AGE = orig

    def test_expired_token_fails(self):
        """Token older than MAX_AGE should be rejected."""
        import app.captcha as mod
        orig_max = mod.MAX_AGE
        mod.MAX_AGE = 0  # Expire immediately
        mod.MIN_AGE = 0
        try:
            q, token = generate()
            answer = _solve(q)
            time.sleep(0.1)
            result = verify(token, str(answer))
            assert result is not None
            assert "过期" in result
        finally:
            mod.MAX_AGE = orig_max
            mod.MIN_AGE = 2

    def test_too_fast_submission_fails(self):
        """Token submitted before MIN_AGE should be rejected."""
        q, token = generate()
        answer = _solve(q)
        result = verify(token, str(answer))
        assert result is not None
        assert "过快" in result

    def test_invalid_token_format(self):
        result = verify("invalid", "42")
        assert result is not None
        assert "无效" in result

    def test_empty_answer(self):
        _, token = generate()
        result = verify(token, "")
        assert result is not None

    def test_non_numeric_answer(self):
        _, token = generate()
        result = verify(token, "abc")
        assert result is not None


class TestHoneypot:
    def test_empty_is_clean(self):
        assert check_honeypot("") is False
        assert check_honeypot(None) is False

    def test_filled_is_suspicious(self):
        assert check_honeypot("bot-value") is True
        assert check_honeypot("anything") is True


def _solve(question: str) -> int:
    import re
    m = re.match(r"(\d+)\s*([+×])\s*(\d+)", question)
    assert m
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    return a + b if op == "+" else a * b
