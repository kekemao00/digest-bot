from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

from digest.models import Item
from digest.sources.base import Source
from digest.sources.feeds import MAX_SUMMARY, strip_html

if TYPE_CHECKING:
    from digest.state import State

API = "https://api.crossref.org/journals/{issn}/works"


def _date(parts: dict | None) -> datetime | None:
    try:
        y, m, d = (list((parts or {})["date-parts"][0]) + [1, 1])[:3]
        return datetime(int(y), int(m), int(d), tzinfo=timezone.utc)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


class Crossref(Source):
    """按 ISSN 从 Crossref 取期刊新发表的文章。用于官方 RSS 拦截自动请求的期刊（Cell、PNAS）。"""

    type = "crossref"

    def fetch(self, client: httpx.Client, now: datetime, state: State) -> list[Item]:
        self.now = now
        days = int(self.options.get("window_days", 3))
        since = (now.astimezone(timezone.utc) - timedelta(days=days)).date()
        params = {
            "filter": f"from-pub-date:{since.isoformat()},type:journal-article",
            "sort": "published",
            "order": "desc",
            "rows": 100,
        }
        resp = client.get(API.format(issn=self.options["issn"]), params=params)
        resp.raise_for_status()
        require_abstract = bool(self.options.get("require_abstract", False))
        items = []
        for work in resp.json().get("message", {}).get("items", []):
            title = strip_html(" ".join(work.get("title") or []), sep="")
            doi = work.get("DOI")
            published = _date(work.get("published-online")) or _date(work.get("published"))
            abstract = strip_html(work.get("abstract") or "")
            if not title or not doi or not published:
                continue
            # 没有摘要的多半是评论、通信、更正，研究论文都有摘要
            if require_abstract and not abstract:
                continue
            items.append(
                self.item(
                    id=doi.lower(),
                    title=title,
                    url=f"https://doi.org/{doi}",
                    published_at=published,
                    summary=abstract.removeprefix("Abstract").strip()[:MAX_SUMMARY] or None,
                )
            )
        return items

    def base_score(self, item: Item) -> float:
        hours = (self.now - item.published_at).total_seconds() / 3600
        return max(1.0, 100.0 - hours)
