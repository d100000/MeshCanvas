"""Tests for the authentication module (app.auth)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.auth import AuthManager, AuthError


@pytest_asyncio.fixture
async def auth(initialized_db):
    """AuthManager wired to the test database."""
    return AuthManager(initialized_db)


class TestUsernameValidation:
    def test_valid_usernames(self):
        for name in ["alice", "Bob_42", "a.b-c", "usr", "A" * 32]:
            result = AuthManager._normalize_username(name)
            assert result == name.strip()

    def test_too_short(self):
        with pytest.raises(AuthError, match="3-32"):
            AuthManager._normalize_username("ab")

    def test_too_long(self):
        with pytest.raises(AuthError, match="3-32"):
            AuthManager._normalize_username("a" * 33)

    def test_invalid_chars(self):
        for name in ["hello world", "user@name", "名前", "a/b", "a\\b"]:
            with pytest.raises(AuthError):
                AuthManager._normalize_username(name)

    def test_strips_whitespace(self):
        result = AuthManager._normalize_username("  alice  ")
        assert result == "alice"


class TestPasswordValidation:
    def test_valid_password(self):
        AuthManager._validate_password("12345678")  # Should not raise

    def test_too_short(self):
        with pytest.raises(AuthError, match="8"):
            AuthManager._validate_password("1234567")

    def test_too_long(self):
        with pytest.raises(AuthError, match="128"):
            AuthManager._validate_password("x" * 129)


class TestPasswordHashing:
    def test_deterministic(self):
        h1 = AuthManager._hash_password("secret", "salt123")
        h2 = AuthManager._hash_password("secret", "salt123")
        assert h1 == h2

    def test_different_salts_produce_different_hashes(self):
        h1 = AuthManager._hash_password("secret", "salt1")
        h2 = AuthManager._hash_password("secret", "salt2")
        assert h1 != h2

    def test_different_passwords_produce_different_hashes(self):
        h1 = AuthManager._hash_password("password1", "same_salt")
        h2 = AuthManager._hash_password("password2", "same_salt")
        assert h1 != h2

    def test_returns_hex_string(self):
        h = AuthManager._hash_password("test", "salt")
        assert isinstance(h, str)
        int(h, 16)  # Should be valid hex


class TestRegisterLogin:
    @pytest.mark.asyncio
    async def test_register_and_login(self, auth: AuthManager):
        user, token, expires = await auth.register("newuser1", "password123")
        assert user["username"] == "newuser1"
        assert isinstance(token, str)
        assert len(token) > 20

        # Login with same credentials
        user2, token2, expires2 = await auth.login("newuser1", "password123")
        assert user2["username"] == "newuser1"
        assert token != token2  # Different session

    @pytest.mark.asyncio
    async def test_register_duplicate_fails(self, auth: AuthManager):
        await auth.register("dupuser", "password123")
        with pytest.raises(AuthError, match="已存在"):
            await auth.register("dupuser", "password456")

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, auth: AuthManager):
        await auth.register("logintest", "correctpw1")
        with pytest.raises(AuthError, match="错误"):
            await auth.login("logintest", "wrongpassword")

    @pytest.mark.asyncio
    async def test_login_nonexistent_user(self, auth: AuthManager):
        with pytest.raises(AuthError, match="错误"):
            await auth.login("nosuchuser999", "anypassword")


class TestSession:
    @pytest.mark.asyncio
    async def test_get_user_from_valid_token(self, auth: AuthManager):
        await auth.register("sessuser", "password123")
        _, token, _ = await auth.login("sessuser", "password123")

        user = await auth.get_user_from_token(token)
        assert user is not None
        assert user["username"] == "sessuser"

    @pytest.mark.asyncio
    async def test_get_user_from_invalid_token(self, auth: AuthManager):
        user = await auth.get_user_from_token("invalid-token-xyz")
        assert user is None

    @pytest.mark.asyncio
    async def test_get_user_from_none_token(self, auth: AuthManager):
        user = await auth.get_user_from_token(None)
        assert user is None

    @pytest.mark.asyncio
    async def test_logout_invalidates_token(self, auth: AuthManager):
        await auth.register("logoutuser", "password123")
        _, token, _ = await auth.login("logoutuser", "password123")

        # Token works before logout
        user = await auth.get_user_from_token(token)
        assert user is not None

        # Logout
        await auth.logout(token)

        # Token no longer works
        user = await auth.get_user_from_token(token)
        assert user is None


class TestAdminLogin:
    @pytest.mark.asyncio
    async def test_admin_login_succeeds_for_admin(self, auth: AuthManager):
        """Default admin user should be able to admin_login."""
        user, token, _ = await auth.admin_login("admin", "admin")
        assert user["role"] == "admin"

    @pytest.mark.asyncio
    async def test_admin_login_fails_for_regular_user(self, auth: AuthManager):
        await auth.register("normie01", "password123")
        with pytest.raises(AuthError, match="管理员权限"):
            await auth.admin_login("normie01", "password123")
