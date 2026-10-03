from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit


@dataclass
class Item:
    """一条候选资讯。每个信息源都把自己的数据转换成这个结构。"""

    source: str  # 来源标识，如 "hn"
    source_name: str  # 展示用的来源名，如 "Hacker News"
    section: str  # 所属板块的 key
    id: str  # 来源内唯一的 id
    title: str
    url: str
    published_at: datetime
    discussion_url: str | None = None
    score: int = 0  # 来源自己的热度，如 HN 分数
    score_unit: str = "分"
    comments: int = 0
    summary: str | None = None  # 来源提供的摘要（RSS 描述、论文 abstract）
    title_zh: str | None = None  # 中文标题（第三阶段由大模型生成）
    one_liner: str | None = None  # 一句话（第三阶段由大模型生成）
    focus: list[str] = field(default_factory=list)  # 命中的侧重领域 key
    rank: float = 0.0  # 排序分，由筛选流程计算

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def domain(self) -> str | None:
        """原文所在域名；没有独立原文（如 Ask HN）时为 None。"""
        if self.discussion_url and self.url == self.discussion_url:
            return None
        host = urlsplit(self.url).hostname or ""
        return host.removeprefix("www.") or None
