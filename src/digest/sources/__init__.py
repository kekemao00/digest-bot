from __future__ import annotations

from typing import Any

from digest.sources.base import Source
from digest.sources.feeds import Feed
from digest.sources.github_trending import GitHubTrending
from digest.sources.hf_papers import HFDailyPapers
from digest.sources.hn import HackerNews
from digest.sources.lobsters import Lobsters
from digest.sources.pages import NewsPage

REGISTRY: dict[str, type[Source]] = {
    cls.type: cls for cls in (HackerNews, Lobsters, GitHubTrending, HFDailyPapers, Feed, NewsPage)
}


def build_sources(config: tuple[dict[str, Any], ...]) -> list[Source]:
    """按配置实例化启用的信息源；未知类型直接报错，避免拼写错误被静默忽略。"""
    sources = []
    for opts in config:
        if opts["type"] not in REGISTRY:
            raise ValueError(f"信息源 {opts['id']!r} 的类型 {opts['type']!r} 未知，可选：{', '.join(REGISTRY)}")
        if opts.get("enabled", True):
            sources.append(REGISTRY[opts["type"]](opts))
    return sources
