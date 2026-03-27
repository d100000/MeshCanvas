# Backward-compatible re-export — new code should import from app.core.config
from app.core.config import *  # noqa: F401,F403
from app.core.config import (  # noqa: F401 — explicit re-exports for type checkers
    ModelInfo,
    ModelSettings,
    clear_config,
    get_config_path,
    get_database_path,
    get_global_user_settings,
    get_settings,
    is_configured,
    save_settings,
)
