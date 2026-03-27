# Backward-compatible re-export — new code should import from app.services.auth_service
from app.services.auth_service import *  # noqa: F401,F403
from app.services.auth_service import (  # noqa: F401
    AuthManager,
    SESSION_COOKIE_NAME,
    SESSION_DAYS,
)
from app.core.exceptions import AuthError  # noqa: F401
