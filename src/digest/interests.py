from __future__ import annotations

import re
from collections.abc import Iterable

from digest.config import Focus
from digest.models import Item

_ASCII_WORD = re.compile(r"^[\x00-\x7f]+$")


def _pattern(keyword: str) -> re.Pattern[str]:
    if not _ASCII_WORD.match(keyword):
        # 中文没有词边界，按子串匹配
        return re.compile(re.escape(keyword))
    # 短的大写缩写（AI、LLM、Fed、SEC）区分大小写，避免误伤普通单词
    flags = 0 if len(keyword) <= 4 and keyword != keyword.lower() else re.I
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])", flags)


class InterestMatcher:
    def __init__(self, focus: Iterable[Focus]) -> None:
        self.focus = list(focus)
        self._patterns = {f.key: [_pattern(k) for k in f.keywords] for f in self.focus}

    def match(self, item: Item) -> list[str]:
        text = " ".join(t for t in (item.title, item.summary) if t)
        return [f.key for f in self.focus if any(p.search(text) for p in self._patterns[f.key])]

    def weight(self, keys: Iterable[str]) -> float:
        weights = [f.weight for f in self.focus if f.key in set(keys)]
        return max(weights, default=1.0)

    def names(self, keys: Iterable[str]) -> list[str]:
        wanted = set(keys)
        return [f.name for f in self.focus if f.key in wanted]
