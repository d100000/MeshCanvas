"""Firecrawl web search integration."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.models.search import SearchBundle, SearchItem

logger = logging.getLogger(__name__)


class FirecrawlSearchService:
    def __init__(self, api_key: str = "", country: str = "CN", timeout_ms: int = 45000) -> None:
        self.api_key = api_key.strip()
        self.country = country.strip() or "CN"
        self.timeout_ms = max(5_000, min(timeout_ms, 120_000))
        self.endpoint = "https://api.firecrawl.dev/v2/search"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def search(self, query: str, think_enabled: bool = False, limit: int | None = None) -> SearchBundle:
        if not self.enabled:
            raise RuntimeError("未配置 FIRECRAWL_API_KEY。")

        search_limit = limit or (6 if think_enabled else 4)
        payload: dict[str, Any] = {
            "query": query,
            "limit": search_limit,
            "sources": ["web"],
            "country": self.country,
            "timeout": self.timeout_ms,
            "scrapeOptions": {
                "formats": ["markdown"],
            },
        }

        timeout = httpx.Timeout(self.timeout_ms / 1000 + 10)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            logger.info("firecrawl search: query=%r limit=%d timeout=%dms", query, search_limit, self.timeout_ms)
            response = await client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
            try:
                raw = response.json()
            except (ValueError, TypeError) as exc:
                raise RuntimeError(f"Firecrawl 返回了无效的 JSON 响应: {exc}") from exc

        data = raw.get("data") or {}
        web_results = data.get("web") or []
        items: list[SearchItem] = []
        for index, item in enumerate(web_results, start=1):
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or item.get("metadata", {}).get("title") or url or "Untitled").strip()
            description = str(item.get("description") or item.get("snippet") or "").strip()
            markdown = str(item.get("markdown") or "").strip()
            items.append(
                SearchItem(
                    title=title,
                    url=url,
                    snippet=description,
                    markdown_excerpt=markdown,
                    rank=index,
                )
            )
        logger.info("firecrawl search completed: query=%r results=%d", query, len(items))
        return SearchBundle(query=query, items=items)
