from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from digest.models import Item

if TYPE_CHECKING:
    from digest.state import State


class Source(ABC):
    type: str = ""
    default_name: str = ""

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.id = str(options["id"])
        self.name = str(options.get("name") or self.default_name or self.id)
        self.section = str(options["section"])
        # 同一板块里多个来源交替排列时的权重，越大越靠前
        self.weight = float(options.get("weight", 1.0))
        # 本来源每天最多入选几条，防止单一来源刷屏
        self.max_items = int(options["max_items"]) if "max_items" in options else None
        pattern = options.get("exclude_title")
        self.exclude_title = re.compile(pattern) if pattern else None

    @abstractmethod
    def fetch(self, client: httpx.Client, now: datetime, state: State) -> list[Item]:
        """抓取候选条目。"""

    def reject_reason(self, item: Item) -> str | None:
        """条目未达到入选门槛时返回原因，达到时返回 None。"""
        if self.exclude_title and self.exclude_title.search(item.title):
            return "标题命中排除规则"
        return None

    def base_score(self, item: Item) -> float:
        """来源内排序用的基础分，侧重领域加权在此基础上进行。"""
        return float(item.score)

    def item(self, **fields: Any) -> Item:
        return Item(source=self.id, source_name=self.name, section=self.section, **fields)
