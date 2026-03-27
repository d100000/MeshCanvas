# Backward-compatible re-export — new code should import from app.services.chat_service
from app.services.chat_service import *  # noqa: F401,F403
from app.services.chat_service import MessageSink, MultiModelChatService  # noqa: F401
