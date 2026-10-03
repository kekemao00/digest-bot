from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote, urlsplit

WEEKDAYS = "一二三四五六日"

_SPACES = re.compile(r"\s+")


def clean(text: str) -> str:
    """去掉换行和多余空白：外部文本里的换行会打乱消息排版。"""
    return _SPACES.sub(" ", text).strip()


_MARKDOWN = str.maketrans({"[": "［", "]": "］", "*": "＊", "<": "‹", ">": "›", "`": "'", "|": "｜"})


def md_text(text: str) -> str:
    """外部文本放进 Markdown 前的处理：控制字符换成外观相近的全角字符，
    标题原样可读，但不能带入链接、标签、加粗或表格分隔。"""
    return clean(text).translate(_MARKDOWN)


def safe_url(url: str) -> str | None:
    """只放行 http(s) 链接，并转义会提前结束 Markdown 链接的字符。"""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    # 括号和空格不在 safe 里，会被编码，避免提前结束 Markdown 链接
    return quote(url.strip(), safe=":/?#@!$&'*+,;=%~-._")


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def date_label(day: datetime) -> str:
    return f"{day.month}月{day.day}日 周{WEEKDAYS[day.weekday()]}"


def count_label(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f} 万".replace(".0 ", " ")
    return str(n)
