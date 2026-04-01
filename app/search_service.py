from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import httpx

logger = logging.getLogger(__name__)


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
    queries_used: list[str] = field(default_factory=list)

    def as_prompt_block(self) -> str:
        if not self.items:
            return "未找到可用联网搜索结果。"

        queries_desc = "、".join(self.queries_used) if self.queries_used else self.query
        sections: list[str] = [
            f"以下是通过 Firecrawl 实时联网搜索获得的资料（共 {len(self.items)} 条，覆盖多个搜索方向），"
            f"请优先基于这些资料回答，并在结尾给出参考来源链接。",
            f"搜索方向：{queries_desc}",
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

        return SearchBundle(query=query, items=items, queries_used=[query])

    async def search_batch(
        self,
        queries: list[dict[str, str]],
        limit_per_query: int = 5,
    ) -> SearchBundle:
        """并行执行多个搜索查询，合并去重结果。

        queries: [{"query": "...", "purpose": "..."}, ...]
        """
        if not self.enabled or not queries:
            combined_q = " / ".join(q.get("query", "") for q in queries[:3])
            return SearchBundle(query=combined_q, items=[])

        query_strings = [q.get("query", "").strip() for q in queries if q.get("query", "").strip()]
        if not query_strings:
            return SearchBundle(query="", items=[])

        logger.info(
            "firecrawl batch search: %d queries, limit_per_query=%d, queries=%s",
            len(query_strings), limit_per_query,
            [q[:60] for q in query_strings],
        )

        tasks = [
            self.search(query=q, limit=limit_per_query)
            for q in query_strings
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        seen_urls: set[str] = set()
        merged_items: list[SearchItem] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("firecrawl batch sub-query failed: %s", result)
                continue
            for item in result.items:
                url_key = item.url.rstrip("/").lower()
                if url_key not in seen_urls:
                    seen_urls.add(url_key)
                    item.rank = len(merged_items) + 1
                    merged_items.append(item)

        combined_query = " / ".join(query_strings[:5])
        if len(query_strings) > 5:
            combined_query += f" 等{len(query_strings)}个方向"

        logger.info(
            "firecrawl batch OK: queries=%d total_results=%d unique=%d",
            len(query_strings), sum(
                len(r.items) for r in results if not isinstance(r, Exception)
            ), len(merged_items),
        )

        return SearchBundle(
            query=combined_query,
            items=merged_items,
            queries_used=query_strings,
        )
