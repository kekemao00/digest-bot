from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import httpx

from digest.models import Item


class Source(ABC):
    name: str = ""
    display_name: str = ""

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.section = str(options["section"])

    @abstractmethod
    def fetch(self, client: httpx.Client, now: datetime) -> list[Item]:
        """抓取时间窗内的候选条目。"""

    def reject_reason(self, item: Item) -> str | None:
        """条目未达到入选门槛时返回原因，达到时返回 None。"""
        return None

    def base_score(self, item: Item) -> float:
        """板块内排序用的基础分，侧重领域加权在此基础上进行。"""
        return float(item.score)
