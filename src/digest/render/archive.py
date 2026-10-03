from __future__ import annotations

from datetime import datetime

from digest.config import Config
from digest.interests import InterestMatcher
from digest.models import Item
from digest.pipeline import Selection
from digest.render.dingtalk import meta_parts, numbering
from digest.render.text import WEEKDAYS, md_text, safe_url

# 落选条目只列分数最高的这么多条，太长就没人看了
MAX_REJECTED = 30


def _link(text: str, url: str | None) -> str:
    target = safe_url(url) if url else None
    return f"[{md_text(text)}]({target})" if target else md_text(text)


def _meta(item: Item, matcher: InterestMatcher) -> str:
    parts = meta_parts(item)
    if names := matcher.names(item.focus):
        parts.append("侧重：" + "、".join(names))
    if item.quality is not None:
        parts.append(f"大模型评分 {item.quality}" + (f"（{item.kind}）" if item.kind else ""))
    return " · ".join(parts)


def render(selection: Selection, config: Config, now: datetime, status: str | None = None) -> str:
    """当天的完整版：GitHub 上阅读的 Markdown，包含全部入选条目和主要落选条目。"""
    matcher = InterestMatcher(config.focus)
    items = selection.items
    lines = [
        f"# {config.title} · {now:%Y-%m-%d} 周{WEEKDAYS[now.weekday()]}",
        "",
        f"> 生成于 {now:%H:%M}（{config.timezone}）· 候选 {selection.candidates} 条 · 入选 {len(items)} 条",
    ]
    if status:
        lines[-1] += "  "  # 引用块里的硬换行，否则两行会连成一段
        lines.append(f"> {md_text(status)}")
    numbers = numbering(items)
    highlights = [h for h in selection.highlights if h.key in numbers]
    if highlights:
        lines += ["", "## 今日要点", ""]
        lines += [f"{n}. {md_text(h.text)}（第 {numbers[h.key]} 条）" for n, h in enumerate(highlights, 1)]
    number = 0
    for section, section_items in selection.sections:
        lines += ["", f"## {section.name}", ""]
        for item in section_items:
            number += 1
            lines.append(f"{number}. **{_link(item.title_zh or item.title, item.url)}**  ")
            if item.blurb:
                lines.append(f"   {md_text(item.blurb)}  ")
            lines.append(f"   {_meta(item, matcher)}")
    if not items:
        lines += ["", "今天没有达到门槛的内容。"]

    if selection.rejected:
        shown = selection.rejected[:MAX_REJECTED]
        lines += ["", "## 落选条目", ""]
        if len(selection.rejected) > len(shown):
            lines += [f"共 {len(selection.rejected)} 条，以下是排序最靠前的 {len(shown)} 条。", ""]
        lines += ["| 条目 | 来源 | 原因 |", "| --- | --- | --- |"]
        for r in shown:
            lines.append(f"| {_link(r.item.title, r.item.url)} | {r.item.source_name} | {md_text(r.reason)} |")
    return "\n".join(lines) + "\n"
