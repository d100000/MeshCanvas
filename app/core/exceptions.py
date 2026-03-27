"""Application-wide exception types."""

from __future__ import annotations


class AuthError(Exception):
    """Raised when authentication or authorization fails (400/401)."""


class OriginError(Exception):
    """Raised when origin validation fails (403)."""


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""
