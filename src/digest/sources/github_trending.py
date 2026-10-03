from __future__ import annotations

import html
import re
from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from digest.models import Item
from digest.render.text import clean
from digest.sources.base import Source

if TYPE_CHECKING:
    from digest.state import State

PAGE = "https://github.com/trending"

_ARTICLE = re.compile(r'<article class="Box-row">(.*?)</article>', re.S)
_REPO = re.compile(r'<h2[^>]*>\s*<a[^>]*href="/([\w.-]+/[\w.-]+)"', re.S)
# 仓库简介所在的段落；同一块里还有赞助按钮等其他段落，不能只按 <p> 匹配
_DESC = re.compile(r'<p class="col-9[^"]*">(.*?)</p>', re.S)
_LANG = re.compile(r'itemprop="programmingLanguage">([^<]+)<')
_STARS = re.compile(r'href="/[\w.-]+/[\w.-]+/stargazers"[^>]*>.*?([\d,]+)\s*</a>', re.S)
_TODAY = re.compile(r"([\d,]+)\s+stars\s+(?:today|this week|this month)")
_TAGS = re.compile(r"<[^>]+>")


def _int(text: str) -> int:
    return int(text.replace(",", ""))


class GitHubTrending(Source):
    """GitHub 没有 Trending 的官方接口，这里解析网页；页面结构变了会抓不到内容，并在日志里报警。"""

    type = "github_trending"
    default_name = "GitHub Trending"

    def fetch(self, client: httpx.Client, now: datetime, state: State) -> list[Item]:
        params = {"since": self.options.get("since", "daily")}
        if language := self.options.get("language"):
            params["spoken_language_code"] = language
        resp = client.get(PAGE, params=params)
        resp.raise_for_status()
        blocks = _ARTICLE.findall(resp.text)
        if not blocks:
            raise ValueError("GitHub Trending 页面结构变了，没有找到仓库列表")
        return [item for block in blocks if (item := self.parse(block, now))]

    def parse(self, block: str, now: datetime) -> Item | None:
        repo = _REPO.search(block)
        today = _TODAY.search(block)
        if not repo or not today:
            return None
        name = repo.group(1)
        desc = _DESC.search(block)
        lang = _LANG.search(block)
        stars = _STARS.search(block)
        extras = [lang.group(1).strip()] if lang else []
        if stars:
            extras.append(f"⭐ {_int(stars.group(1)):,}")
        gained = _int(today.group(1))
        return self.item(
            id=name.lower(),
            title=name,
            url=f"https://github.com/{name}",
            published_at=now,
            score=gained,
            score_text=f"今日 +{gained:,}",
            summary=clean(html.unescape(_TAGS.sub("", desc.group(1)))) if desc else None,
            extras=extras,
        )

    def reject_reason(self, item: Item) -> str | None:
        if reason := super().reject_reason(item):
            return reason
        min_stars = int(self.options.get("min_stars_today", 300))
        if item.score >= min_stars:
            return None
        return f"热度不足（今日 +{item.score} star，门槛 {min_stars}）"
