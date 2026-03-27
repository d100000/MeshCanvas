"""Search result data models (extracted from search_service)."""

from __future__ import annotations

from dataclasses import dataclass


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
            "以下是通过 Firecrawl 实时联网搜索获得的资料，请优先基于这些资料回答，并在结尾给出参考来源链接。",
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
