from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class SearchItem:
    title: str
    url: str
    snippet: str
    markdown_excerpt: str
    source_type: str = "web"
    rank: int = 0


@dataclass
class SearchBundle:
    query: str
    items: list[SearchItem]
    provider: str = "firecrawl"

    def as_prompt_block(self) -> str:
        if not self.items:
            return "未找到可用联网搜索结果。"

        sections: list[str] = [
            f"以下是通过 Firecrawl 实时联网搜索获得的资料，请优先基于这些资料回答，并在结尾给出参考来源链接。",
            f"搜索词：{self.query}",
        ]
        for index, item in enumerate(self.items, start=1):
            excerpt = item.markdown_excerpt or item.snippet
            excerpt = excerpt.strip()
            if len(excerpt) > 900:
                excerpt = excerpt[:900] + "..."
            sections.append(
                f"[{index}] {item.title}\nURL: {item.url}\n摘要: {item.snippet or '无'}\n摘录:\n{excerpt or '无'}"
            )
        return "\n\n".join(sections)


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
            return SearchBundle(query=query, items=[])  # 未配置时返回空结果，调用方无需关心

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
            markdown = str(item.get("markdown") or "")[:2000].strip()  # 截断过长摘录
            items.append(
                SearchItem(
                    title=title,
                    url=url,
                    snippet=description,
                    markdown_excerpt=markdown,
                    rank=index,
                )
            )
        return SearchBundle(query=query, items=items)
