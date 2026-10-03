from __future__ import annotations

import calendar
import html
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import feedparser
import httpx

from digest.models import Item
from digest.render.text import clean
from digest.sources.base import Source

if TYPE_CHECKING:
    from digest.state import State

_TAGS = re.compile(r"<[^>]+>")
# 期刊订阅源描述里的固定前缀，不是摘要：
#   Nature：“Nature, Published online: 02 October 2026; doi:10.1038/…”
#   Science：“Science, Volume 394, Issue 6819, Page 15-15, October 2026.”
_BOILERPLATE = re.compile(
    r"^(?:[^;]{0,80}Published online:[^;]*;\s*doi:\S+|[^,]{0,60}, Volume \d+, Issue \d+, Page [^,]+, \w+ \d{4}\.)\s*",
    re.I,
)
_PAGES = re.compile(r"\bPages? (e?[\w]+)(?:-(\w+))?", re.I)


def page_count(text: str) -> int | None:
    """从 “Page 15-15” 或 “Page eadk1234” 里估算篇幅；e 开头的是在线发表的研究论文，按足够长处理。"""
    m = _PAGES.search(text)
    if not m:
        return None
    first, last = m.group(1), m.group(2)
    if first.lower().startswith("e"):
        return 99
    if first.isdigit() and last and last.isdigit():
        return int(last) - int(first) + 1
    return 1
# 只取摘要的前一部分，摘要只用于关键词匹配和（第三阶段）大模型输入
MAX_SUMMARY = 1500


def strip_html(text: str, sep: str = " ") -> str:
    """去掉 HTML 标签。标题里的 <i>、<sub> 等行内标签用 sep="" 去掉，避免把一个词拆开。"""
    return clean(html.unescape(_TAGS.sub(sep, text)))


def entry_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        if parsed := entry.get(key):
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


class Feed(Source):
    """RSS / Atom 订阅源：实验室博客、期刊、arXiv。"""

    type = "feed"

    def fetch(self, client: httpx.Client, now: datetime, state: State) -> list[Item]:
        self.now = now
        resp = client.get(self.options["url"])
        resp.raise_for_status()
        # 先由 httpx 下载（统一超时和重试），再交给 feedparser 解析内容
        parsed = feedparser.parse(resp.content)
        if not parsed.entries:
            # 被拦截时常常返回 200 的 HTML 页面，解析出来是空的，要报出来而不是当作“今天没有新内容”
            detail = type(parsed.bozo_exception).__name__ if parsed.bozo else "没有任何条目"
            raise ValueError(f"订阅源为空或被拦截（{detail}）")
        min_pages = int(self.options.get("min_pages", 0))
        # 期刊等只给日期不给时间，时间窗放宽到 3 天；推送过的内容由跨天去重挡掉
        since = now - timedelta(hours=float(self.options.get("window_hours", 72)))
        link_pattern = re.compile(self.options["link_pattern"]) if self.options.get("link_pattern") else None
        items = []
        for entry in parsed.entries:
            link = entry.get("link") or ""
            # 期刊标题里常带 <i> 等斜体标签
            title = strip_html(entry.get("title") or "", sep="")
            published = entry_time(entry)
            if not link or not title or not published or published < since:
                continue
            if link_pattern and not link_pattern.search(link):
                continue
            raw_summary = strip_html(entry.get("summary") or "")
            if min_pages and (pages := page_count(raw_summary)) is not None and pages < min_pages:
                continue  # 新闻、观点等短文章
            summary = _BOILERPLATE.sub("", raw_summary)[:MAX_SUMMARY] or None
            items.append(
                self.item(
                    id=entry.get("id") or link,
                    title=title,
                    url=link,
                    published_at=published,
                    summary=summary,
                    extras=self.extras(entry),
                )
            )
        return items

    def extras(self, entry: Any) -> list[str]:
        if label := self.options.get("label"):
            return [str(label)]
        # arXiv 的 Atom 带主分类，例如 q-fin.TR
        if category := (entry.get("arxiv_primary_category") or {}).get("term"):
            return [f"arXiv {category}"]
        return []

    def base_score(self, item: Item) -> float:
        # 订阅源没有热度数据，同一来源内越新越靠前（每小时减 1 分）
        now = getattr(self, "now", None) or datetime.now(timezone.utc)
        hours = (now - item.published_at).total_seconds() / 3600
        return max(1.0, 100.0 - hours)
