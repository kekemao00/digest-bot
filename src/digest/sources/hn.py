from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from digest.models import Item
from digest.sources.base import Source

API = "https://hn.algolia.com/api/v1/search"
ITEM_URL = "https://news.ycombinator.com/item?id={}"

# 每月固定的招聘、求职帖不算资讯
SKIP_TITLES = re.compile(r"^(Ask HN: Who is hiring|Ask HN: Who wants to be hired|Ask HN: Freelancer\?)", re.I)


class HackerNews(Source):
    name = "hn"
    display_name = "Hacker News"

    def fetch(self, client: httpx.Client, now: datetime) -> list[Item]:
        since = now - timedelta(hours=float(self.options.get("window_hours", 24)))
        # 先用一个较低的分数下限缩小结果集，真正的门槛在 reject_reason 里判断，
        # 这样评论很多但分数一般的帖子也能被看到
        params = {
            "tags": "story",
            "numericFilters": f"created_at_i>{int(since.timestamp())},points>=20",
            "hitsPerPage": 500,
        }
        resp = client.get(API, params=params)
        resp.raise_for_status()
        return [item for hit in resp.json().get("hits", []) if (item := self.parse(hit))]

    def parse(self, hit: dict) -> Item | None:
        title = (hit.get("title") or "").strip()
        story_id = str(hit.get("objectID") or "")
        if not title or not story_id or SKIP_TITLES.match(title):
            return None
        discussion = ITEM_URL.format(story_id)
        return Item(
            source=self.name,
            source_name=self.display_name,
            section=self.section,
            id=story_id,
            title=title,
            url=hit.get("url") or discussion,
            discussion_url=discussion,
            published_at=datetime.fromtimestamp(int(hit["created_at_i"]), tz=timezone.utc),
            score=int(hit.get("points") or 0),
            comments=int(hit.get("num_comments") or 0),
        )

    def reject_reason(self, item: Item) -> str | None:
        min_points = int(self.options.get("min_points", 200))
        min_comments = int(self.options.get("min_comments", 100))
        if item.score >= min_points or item.comments >= min_comments:
            return None
        return f"热度不足（{item.score} 分 / {item.comments} 评论，门槛 {min_points} 分或 {min_comments} 评论）"

    def base_score(self, item: Item) -> float:
        # 评论多说明值得讨论，但比分数更容易被争议话题推高，所以只算一半
        return item.score + 0.5 * item.comments
