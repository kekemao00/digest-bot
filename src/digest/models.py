from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from digest.urls import canonical_url


@dataclass
class Also:
    """同一内容在其他来源的出现，例如 HN 和 Lobsters 都在讨论同一篇文章。"""

    source_name: str
    url: str


@dataclass
class Item:
    """一条候选资讯。每个信息源都把自己的数据转换成这个结构。"""

    source: str  # 信息源 id，如 "hn"、"openai"
    source_name: str  # 展示用的来源名，如 "Hacker News"
    section: str  # 所属板块的 key
    id: str  # 来源内唯一的 id
    title: str
    url: str
    published_at: datetime
    discussion_url: str | None = None
    show_domain: bool = False  # 聚合站（HN、Lobsters）的条目需要显示原文域名
    score: int = 0  # 来源自己的热度，如 HN 分数
    score_unit: str = "分"
    score_text: str | None = None  # 自定义热度文案，如 GitHub 的“今日 +878 star”
    comments: int = 0
    summary: str | None = None  # 来源提供的摘要（RSS 描述、论文 abstract、仓库简介）
    extras: list[str] = field(default_factory=list)  # 额外的元信息，如编程语言、arXiv 分类
    links: list[tuple[str, str]] = field(default_factory=list)  # 额外链接，如论文代码仓库
    also: list[Also] = field(default_factory=list)
    title_zh: str | None = None  # 中文标题（第三阶段由大模型生成）
    one_liner: str | None = None  # 一句话（第三阶段由大模型生成）
    focus: list[str] = field(default_factory=list)  # 命中的侧重领域 key
    rank: float = 0.0  # 排序分，由筛选流程计算

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def canonical(self) -> str:
        return canonical_url(self.url)

    @property
    def domain(self) -> str | None:
        """聚合站条目的原文域名；其他来源的域名就是来源本身，不再重复。"""
        if not self.show_domain or self.url == self.discussion_url:
            return None
        host = urlsplit(self.url).hostname or ""
        return host.removeprefix("www.") or None

    @property
    def blurb(self) -> str | None:
        """标题下的一行说明：优先用一句话摘要；没有时用来源自带的短简介（长摘要不放进消息）。"""
        if self.one_liner:
            return self.one_liner
        if self.summary and len(self.summary) <= 160:
            return self.summary
        return None
