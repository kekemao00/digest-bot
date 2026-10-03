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
# Nature 系列订阅源的描述只有“期刊名, Published online: 日期; doi:…”，不是摘要
_BOILERPLATE = re.compile(r"^[^;]{0,80}Published online:[^;]*;\s*doi:\S+\s*", re.I)
# 只取摘要的前一部分，摘要只用于关键词匹配和（第三阶段）大模型输入
MAX_SUMMARY = 1500


def strip_html(text: str) -> str:
    return clean(html.unescape(_TAGS.sub(" ", text)))


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
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"订阅源解析失败：{type(parsed.bozo_exception).__name__}")
        # 期刊等只给日期不给时间，时间窗放宽到 3 天；推送过的内容由跨天去重挡掉
        since = now - timedelta(hours=float(self.options.get("window_hours", 72)))
        link_pattern = re.compile(self.options["link_pattern"]) if self.options.get("link_pattern") else None
        items = []
        for entry in parsed.entries:
            link = entry.get("link") or ""
            title = clean(html.unescape(entry.get("title") or ""))
            published = entry_time(entry)
            if not link or not title or not published or published < since:
                continue
            if link_pattern and not link_pattern.search(link):
                continue
            summary = _BOILERPLATE.sub("", strip_html(entry.get("summary") or ""))[:MAX_SUMMARY] or None
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
