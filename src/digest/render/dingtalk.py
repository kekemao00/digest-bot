from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from digest.config import Config, Section
from digest.models import Item
from digest.pipeline import Selection
from digest.render.text import count_label, date_label, md_text, safe_url, truncate

GREY = "#999999"
# 钉钉 Markdown 消息正文有长度限制，留出余量
MAX_BYTES = 18000
# 条目之间空一行；同一条目内的几行用 Markdown 硬换行（行尾两个空格），视觉上归为一组
BREAK = "\n\n"
LINE = "  \n"


@dataclass
class Message:
    title: str  # 显示在通知和会话列表预览里
    text: str


def grey(text: str) -> str:
    return f"<font color={GREY}>{text}</font>"


def reading_minutes(items: list[Item]) -> int:
    chars = sum(len(i.title_zh or i.title) + len(i.one_liner or "") for i in items)
    # 中文约每分钟 300 字，另给每条留出扫一眼来源信息的时间
    return max(1, round(chars / 300 + len(items) * 0.15))


def meta_line(item: Item) -> str:
    parts: list[str] = []
    if item.title_zh:
        parts.append(md_text(truncate(item.title, 80)))
    if item.domain:
        parts.append(md_text(item.domain))
    if item.score:
        parts.append(f"{item.source_name} {count_label(item.score)} {item.score_unit}")
    else:
        parts.append(item.source_name)
    if item.comments and item.discussion_url and (url := safe_url(item.discussion_url)):
        parts.append(f"[{count_label(item.comments)} 条讨论]({url})")
    return grey(" · ".join(parts))


def render_item(number: int, item: Item) -> str:
    title = md_text(item.title_zh or item.title)
    url = safe_url(item.url)
    lines = [f"**{number}. [{title}]({url})**" if url else f"**{number}. {title}**"]
    if item.one_liner:
        lines.append(md_text(item.one_liner))
    lines.append(meta_line(item))
    return LINE.join(lines)


def _compose(sections: list[tuple[Section, list[Item]]], config: Config, now: datetime, archive_url: str | None) -> str:
    items = [item for _, section_items in sections for item in section_items]
    lines = [
        f"### {config.title} · {date_label(now)}",
        grey(f"{len(items)} 条 · 约 {reading_minutes(items)} 分钟"),
    ]
    number = 0
    for section, section_items in sections:
        lines.append(f"#### {section.name}")
        for item in section_items:
            number += 1
            lines.append(render_item(number, item))
    footer = [f"[完整版与落选条目]({url})"] if archive_url and (url := safe_url(archive_url)) else []
    footer.append(f"数据截至 {now:%H:%M}")
    lines += ["---", grey(" · ".join(footer))]
    return BREAK.join(lines)


def render(selection: Selection, config: Config, now: datetime, archive_url: str | None = None) -> Message:
    sections = [(section, list(items)) for section, items in selection.sections]
    text = _compose(sections, config, now, archive_url)
    while len(text.encode()) > MAX_BYTES and sections:
        # 超长时从最后一个板块的末尾逐条删除，完整内容仍在归档里
        sections[-1][1].pop()
        if not sections[-1][1]:
            sections.pop()
        text = _compose(sections, config, now, archive_url)

    items = [item for _, section_items in sections for item in section_items]
    title = f"{config.title} {len(items)} 条"
    if items:
        title += f"｜{items[0].title_zh or items[0].title}"
    title = truncate(title, 60)
    return Message(title=title, text=text)
