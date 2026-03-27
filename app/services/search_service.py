"""Firecrawl web search integration with detailed logging."""

from __future__ import annotations

import logging
from time import perf_counter
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

        timeout_sec = self.timeout_ms / 1000 + 10
        timeout = httpx.Timeout(timeout_sec)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        logger.info(
            "firecrawl request: query=%r limit=%d country=%s timeout=%dms endpoint=%s",
            query, search_limit, self.country, self.timeout_ms, self.endpoint,
        )
        started_at = perf_counter()

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(self.endpoint, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            elapsed_ms = (perf_counter() - started_at) * 1000
            logger.error(
                "firecrawl timeout: query=%r elapsed=%.0fms timeout_limit=%.0fs error=%s",
                query, elapsed_ms, timeout_sec, exc,
            )
            raise RuntimeError(f"Firecrawl 搜索超时（{elapsed_ms:.0f}ms）: {exc}") from exc
        except httpx.ConnectError as exc:
            elapsed_ms = (perf_counter() - started_at) * 1000
            logger.error(
                "firecrawl connection error: query=%r elapsed=%.0fms error=%s",
                query, elapsed_ms, exc,
            )
            raise RuntimeError(f"Firecrawl 连接失败: {exc}") from exc
        except httpx.HTTPError as exc:
            elapsed_ms = (perf_counter() - started_at) * 1000
            logger.error(
                "firecrawl HTTP error: query=%r elapsed=%.0fms error_type=%s error=%s",
                query, elapsed_ms, type(exc).__name__, exc,
            )
            raise RuntimeError(f"Firecrawl 请求失败: {exc}") from exc

        elapsed_ms = (perf_counter() - started_at) * 1000
        status_code = response.status_code

        if status_code != 200:
            body_preview = response.text[:500] if response.text else "(empty)"
            logger.error(
                "firecrawl HTTP %d: query=%r elapsed=%.0fms body=%s",
                status_code, query, elapsed_ms, body_preview,
            )
            raise RuntimeError(
                f"Firecrawl 返回 HTTP {status_code}（耗时 {elapsed_ms:.0f}ms）"
            )

        try:
            raw = response.json()
        except (ValueError, TypeError) as exc:
            body_preview = response.text[:300] if response.text else "(empty)"
            logger.error(
                "firecrawl invalid JSON: query=%r elapsed=%.0fms body=%s error=%s",
                query, elapsed_ms, body_preview, exc,
            )
            raise RuntimeError(f"Firecrawl 返回了无效的 JSON 响应: {exc}") from exc

        # Parse results
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

        if items:
            logger.info(
                "firecrawl OK: query=%r results=%d elapsed=%.0fms titles=%s",
                query, len(items), elapsed_ms,
                [it.title[:40] for it in items[:5]],
            )
        else:
            logger.warning(
                "firecrawl empty: query=%r elapsed=%.0fms raw_keys=%s data_keys=%s",
                query, elapsed_ms, list(raw.keys()), list(data.keys()),
            )

        return SearchBundle(query=query, items=items)
