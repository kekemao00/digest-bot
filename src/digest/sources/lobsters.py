from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from digest.models import Item
from digest.sources.base import Source

if TYPE_CHECKING:
    from digest.state import State

API = "https://lobste.rs/hottest.json"


class Lobsters(Source):
    type = "lobsters"
    default_name = "Lobsters"

    def fetch(self, client: httpx.Client, now: datetime, state: State) -> list[Item]:
        resp = client.get(API)
        resp.raise_for_status()
        since = now - timedelta(hours=float(self.options.get("window_hours", 30)))
        items = [self.parse(story) for story in resp.json()]
        return [i for i in items if i and i.published_at >= since]

    def parse(self, story: dict) -> Item | None:
        title = (story.get("title") or "").strip()
        short_id = str(story.get("short_id") or "")
        discussion = story.get("comments_url") or story.get("short_id_url")
        if not title or not short_id or not discussion:
            return None
        return self.item(
            id=short_id,
            title=title,
            url=story.get("url") or discussion,
            discussion_url=discussion,
            show_domain=True,
            published_at=datetime.fromisoformat(story["created_at"]),
            score=int(story.get("score") or 0),
            comments=int(story.get("comment_count") or 0),
            extras=[f"#{t}" for t in story.get("tags", [])[:3]],
        )

    def reject_reason(self, item: Item) -> str | None:
        if reason := super().reject_reason(item):
            return reason
        min_score = int(self.options.get("min_score", 20))
        if item.score >= min_score:
            return None
        return f"热度不足（{item.score} 分，门槛 {min_score} 分）"
