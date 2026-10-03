from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from digest.models import Item

log = logging.getLogger("digest")

VERSION = 1


class State:
    """跨天的记忆：推送过哪些内容、没有 RSS 的博客页面上已经见过哪些文章。

    存在仓库的 state 分支里，由工作流在推送成功后提交。文件不存在时视为第一次运行。
    """

    def __init__(self, path: Path | None, keep_days: int = 30) -> None:
        self.path = path
        self.keep_days = keep_days
        self.sent: dict[str, str] = {}  # 归一化 URL -> 推送日期
        self.pages: dict[str, list[str]] = {}  # 信息源 id -> 页面上见过的文章链接
        self.last_sent: str | None = None  # 最近一次推送的日期，防止定时任务同一天推两次
        if path and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == VERSION:
                self.sent = dict(data.get("sent", {}))
                self.pages = {k: list(v) for k, v in data.get("pages", {}).items()}
                self.last_sent = data.get("last_sent")
            else:
                log.warning("state 文件版本不匹配，按第一次运行处理")

    def was_sent(self, item: Item) -> bool:
        return item.canonical in self.sent

    def mark_sent(self, items: list[Item], today: date) -> None:
        for item in items:
            self.sent[item.canonical] = today.isoformat()
        self.last_sent = today.isoformat()

    def new_links(self, source_id: str, links: list[str]) -> list[str] | None:
        """返回页面上第一次出现的链接；这个来源第一次运行时返回 None（只记录，不推送旧文章）。"""
        known = self.pages.get(source_id)
        self.pages[source_id] = list(dict.fromkeys([*links, *(known or [])]))[:200]
        if known is None:
            return None
        seen = set(known)
        return [link for link in links if link not in seen]

    def save(self, today: date) -> None:
        if not self.path:
            return
        cutoff = (today - timedelta(days=self.keep_days)).isoformat()
        self.sent = {url: day for url, day in self.sent.items() if day >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": VERSION,
            "last_sent": self.last_sent,
            "sent": dict(sorted(self.sent.items())),
            "pages": self.pages,
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
