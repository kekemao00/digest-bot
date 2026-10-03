from __future__ import annotations

from typing import Any

from digest.sources.base import Source
from digest.sources.hn import HackerNews

REGISTRY: dict[str, type[Source]] = {
    HackerNews.name: HackerNews,
}


def build_sources(config: dict[str, dict[str, Any]]) -> list[Source]:
    """按配置实例化启用的信息源；配置里出现未知来源时直接报错，避免拼写错误被静默忽略。"""
    sources = []
    for name, opts in config.items():
        if name not in REGISTRY:
            raise ValueError(f"未知的信息源 {name!r}，可选：{', '.join(REGISTRY)}")
        if opts.get("enabled", True):
            sources.append(REGISTRY[name](opts))
    return sources
