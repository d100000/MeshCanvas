# Backward-compatible re-export — new code should import from app.services.search_service or app.models.search
from app.services.search_service import FirecrawlSearchService  # noqa: F401
from app.models.search import SearchBundle, SearchItem  # noqa: F401
