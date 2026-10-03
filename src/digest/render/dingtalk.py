from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from digest.config import Config, Section
from digest.models import TOPICS, Highlight, Item
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
    chars = sum(len(i.title_zh or i.title) + len(i.blurb or "") for i in items)
    # 中文约每分钟 300 字，另给每条留出扫一眼来源信息的时间
    return max(1, round(chars / 300 + len(items) * 0.15))


def meta_parts(item: Item, original: bool = False) -> list[str]:
    """标题下灰色小字的各个部分，钉钉版和完整版共用：热度和讨论在前，用图标代替文字，手机上一行放得下。

    original 为真时先列出原标题（完整版用来核对译文；钉钉消息里不放，省一行）。
    """
    parts: list[str] = []
    if original and item.title_zh:
        parts.append(md_text(item.title))
    if heat := item.score_text or (count_label(item.score) if item.score else None):
        parts.append(f"🔥 {md_text(heat)}")
    if item.comments and item.discussion_url and (url := safe_url(item.discussion_url)):
        parts.append(f"[💬 {count_label(item.comments)}]({url})")
    if item.domain:
        parts.append(md_text(item.domain))
    parts.append(md_text(item.source_name))
    # 有领域标签时，来源自带的话题标签（如 Lobsters 的 #security）是重复信息
    parts += [md_text(extra) for extra in item.extras if not (item.topic and extra.startswith("#"))]
    if item.subject and not item.extras:
        parts.append(md_text(item.subject))
    parts += [f"[{md_text(label)}]({url})" for label, link in item.links if (url := safe_url(link))]
    also = [f"[{md_text(a.source_name)}]({url})" for a in item.also if (url := safe_url(a.url))]
    if also:
        parts.append("另见 " + "、".join(also))
    return parts


def topic_tag(item: Item) -> str | None:
    """标题后的领域小标签，如“🤖 AI”。“其他”和没有判断出领域的不显示。"""
    emoji = TOPICS.get(item.topic or "")
    return f"{emoji} {item.topic}" if emoji else None


def topic_summary(items: list[Item]) -> str | None:
    """今天各领域的条数，如“AI 5 · 科学 4 · 金融 3”，多的在前，“其他”放最后。没有大模型时不显示。"""
    if not any(item.topic for item in items):
        return None
    counts = Counter(item.topic or "其他" for item in items)
    order = list(TOPICS)
    ranked = sorted(counts, key=lambda t: (t == "其他", -counts[t], order.index(t)))
    return " · ".join(f"{t} {counts[t]}" for t in ranked)


def section_heading(section: Section) -> str:
    return f"{section.icon} {section.name}" if section.icon else section.name


def render_item(number: int, item: Item) -> str:
    title = md_text(item.title_zh or item.title)
    url = safe_url(item.url)
    first = f"**{number}. [{title}]({url})**" if url else f"**{number}. {title}**"
    if tag := topic_tag(item):
        first += " " + grey(tag)
    lines = [first]
    if item.blurb:
        lines.append(md_text(truncate(item.blurb, 120)))
    lines.append(grey(" · ".join(meta_parts(item))))
    return LINE.join(lines)


def numbering(items: list[Item]) -> dict[str, int]:
    """条目编号跨板块连续，今日要点按编号指向条目。"""
    return {item.key: n for n, item in enumerate(items, 1)}


def render_highlights(highlights: list[Highlight], items: list[Item]) -> str | None:
    numbers = numbering(items)
    lines = [
        f"{n}. {md_text(h.text)} {grey(f'第 {numbers[h.key]} 条')}"
        for n, h in enumerate((h for h in highlights if h.key in numbers), 1)
    ]
    return "**今日要点**\n\n" + "\n".join(lines) if lines else None


def _compose(
    sections: list[tuple[Section, list[Item]]],
    highlights: list[Highlight],
    config: Config,
    now: datetime,
    archive_url: str | None,
) -> str:
    items = [item for _, section_items in sections for item in section_items]
    overview = f"{len(items)} 条 · 约 {reading_minutes(items)} 分钟"
    if topics := topic_summary(items):
        overview += f"｜{topics}"
    lines = [f"### {config.title} · {date_label(now)}", grey(overview)]
    if block := render_highlights(highlights, items):
        lines.append(block)
    number = 0
    for section, section_items in sections:
        lines.append(f"#### {section_heading(section)}")
        for item in section_items:
            number += 1
            lines.append(render_item(number, item))
    footer = [f"[完整版与落选条目]({url})"] if archive_url and (url := safe_url(archive_url)) else []
    footer.append(f"数据截至 {now:%H:%M}")
    lines += ["---", grey(" · ".join(footer))]
    return BREAK.join(lines)


def render(selection: Selection, config: Config, now: datetime, archive_url: str | None = None) -> Message:
    sections = [(section, list(items)) for section, items in selection.sections]
    highlights = selection.highlights
    text = _compose(sections, highlights, config, now, archive_url)
    while len(text.encode()) > MAX_BYTES and sections:
        # 超长时从最后一个板块的末尾逐条删除，完整内容仍在归档里；指向被删条目的要点一并去掉
        sections[-1][1].pop()
        if not sections[-1][1]:
            sections.pop()
        text = _compose(sections, highlights, config, now, archive_url)

    items = [item for _, section_items in sections for item in section_items]
    shown = [h for h in highlights if h.key in numbering(items)]
    title = f"{config.title} {len(items)} 条"
    # 通知栏只显示标题：有今日要点时放第一条要点，否则放第一条的标题
    if shown:
        title += f"｜{shown[0].text}"
    elif items:
        title += f"｜{items[0].title_zh or items[0].title}"
    title = truncate(title, 60)
    return Message(title=title, text=text)
