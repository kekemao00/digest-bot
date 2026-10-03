from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx

from digest.models import Item
from digest.render.text import clean
from digest.sources.base import Source

if TYPE_CHECKING:
    from digest.state import State

API = "https://huggingface.co/api/daily_papers"


class HFDailyPapers(Source):
    """Hugging Face 每日论文：社区从当天 arXiv 新论文里挑出并点赞的 AI 论文。"""

    type = "hf_daily_papers"
    default_name = "HF 论文"

    def fetch(self, client: httpx.Client, now: datetime, state: State) -> list[Item]:
        # 取前一天（UTC）的榜单：北京时间早上运行时，前一天的点赞已基本稳定
        day = (now.astimezone(timezone.utc) - timedelta(days=1)).date()
        resp = client.get(API, params={"date": day.isoformat(), "limit": 100})
        resp.raise_for_status()
        return [item for entry in resp.json() if (item := self.parse(entry))]

    def parse(self, entry: dict) -> Item | None:
        # 论文的主要字段在 paper 里，外层也有一份标题和摘要
        paper = entry.get("paper") or entry
        arxiv_id = str(paper.get("id") or "")
        title = clean(paper.get("title") or entry.get("title") or "")
        if not arxiv_id or not title:
            return None
        published = paper.get("publishedAt") or entry.get("publishedAt")
        links = []
        if repo := paper.get("githubRepo"):
            links.append(("代码", repo))
        return self.item(
            id=arxiv_id,
            title=title,
            url=f"https://arxiv.org/abs/{arxiv_id}",
            discussion_url=f"https://huggingface.co/papers/{arxiv_id}",
            published_at=datetime.fromisoformat(published.replace("Z", "+00:00")) if published else datetime.now(timezone.utc),
            score=int(paper.get("upvotes") or entry.get("upvotes") or 0),
            comments=int(entry.get("numComments") or 0),
            summary=clean(paper.get("summary") or entry.get("summary") or "")[:1500] or None,
            links=links,
        )

    def reject_reason(self, item: Item) -> str | None:
        if reason := super().reject_reason(item):
            return reason
        min_upvotes = int(self.options.get("min_upvotes", 20))
        if item.score >= min_upvotes:
            return None
        return f"热度不足（{item.score} 赞，门槛 {min_upvotes}）"
