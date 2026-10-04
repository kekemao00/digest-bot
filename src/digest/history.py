from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from digest.config import Config
from digest.interests import InterestMatcher
from digest.models import TOPICS
from digest.pipeline import Selection
from digest.render.dingtalk import numbering
from digest.render.text import WEEKDAYS, md_text, safe_url

log = logging.getLogger("digest")

# 归档目录下的两个文件：逐条清单（每行一条推送过的内容）和由它生成的目录页
INDEX = "items.jsonl"
README = "README.md"


def records(selection: Selection, config: Config, today: date) -> list[dict[str, Any]]:
    """当天入选条目的逐条记录，字段固定，方便以后检索和统计。"""
    matcher = InterestMatcher(config.focus)
    numbers = numbering(selection.items)
    highlights = {h.key: (rank, h.text) for rank, h in enumerate(selection.highlights, 1) if h.key in numbers}
    rows = []
    for section, items in selection.sections:
        for item in items:
            rank, text = highlights.get(item.key, (None, None))
            rows.append(
                {
                    "date": today.isoformat(),
                    "edition": config.edition,
                    "n": numbers[item.key],
                    "section": section.name,
                    "title": item.title_zh or item.title,
                    "original_title": item.title,
                    "summary": item.blurb,
                    "url": item.url,
                    "discussion_url": item.discussion_url,
                    "source": item.source_name,
                    "topic": item.topic,
                    "kind": item.kind,
                    "score": item.quality,
                    "focus": matcher.names(item.focus),
                    "highlight_rank": rank,
                    "highlight": text,
                }
            )
    return rows


def _edition(row: dict[str, Any]) -> str:
    # 加晚间版之前的记录没有 edition 字段，都是早间版
    return row.get("edition") or "morning"


def update(archive_dir: Path, selection: Selection, config: Config, today: date) -> None:
    """把这一版的入选条目写进逐条清单并重建目录页。同一天同一版重跑时替换原来的记录，不会重复。"""
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / INDEX
    day = today.isoformat()
    # 其他日期和另一版的行原样保留，即使某一行解析不了也不丢
    kept: list[tuple[str, dict[str, Any]]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and ((row := _row(line)).get("date") != day or _edition(row) != config.edition):
                kept.append((line, row))
    new = [(json.dumps(row, ensure_ascii=False), row) for row in records(selection, config, today)]
    path.write_text("".join(f"{line}\n" for line, _ in kept + new), encoding="utf-8")
    (archive_dir / README).write_text(render_readme([row for _, row in kept + new if row], config), encoding="utf-8")


def _row(line: str) -> dict[str, Any]:
    try:
        row = json.loads(line)
    except ValueError:
        log.warning("归档清单里有一行无法解析，原样保留")
        return {}
    return row if isinstance(row, dict) else {}


def _topics(rows: list[dict[str, Any]]) -> str:
    counts = Counter(row.get("topic") or "其他" for row in rows)
    if set(counts) == {"其他"}:
        return ""
    order = list(TOPICS)
    ranked = sorted(counts, key=lambda t: (t == "其他", -counts[t], order.index(t) if t in order else len(order)))
    return " · ".join(f"{md_text(t)} {counts[t]}" for t in ranked)


def render_readme(rows: list[dict[str, Any]], config: Config) -> str:
    """目录页：按月分组，最新的在最上面；每天一行，后面跟当天的要点。"""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if isinstance(row.get("date"), str):
            by_day.setdefault(row["date"], []).append(row)
    lines = [
        f"# {config.title}归档",
        "",
        f"每天推送后自动更新，共 {len(by_day)} 天。点日期看当天完整版（含落选条目和原因）。"
        f"逐条数据在 [{INDEX}]({INDEX})，每行一条推送过的内容，可以下载后检索或统计。",
    ]
    month = None
    for day in sorted(by_day, reverse=True):
        try:
            when = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            continue
        if (when.year, when.month) != month:
            month = (when.year, when.month)
            lines += ["", f"## {when.year} 年 {when.month} 月", ""]
        day_label = f"{when.month}月{when.day}日 周{WEEKDAYS[when.weekday()]}"
        # 同一天里晚间版在上面，和整页最新的在最上面一致
        for edition, suffix, name in (("evening", "-evening", " 晚间"), ("morning", "", "")):
            edition_rows = sorted((r for r in by_day[day] if _edition(r) == edition), key=lambda r: r.get("n") or 0)
            if not edition_rows:
                continue
            summary = f"{len(edition_rows)} 条"
            if topics := _topics(edition_rows):
                summary += f"｜{topics}"
            lines.append(f"- [{day_label}{name}]({when:%Y}/{when:%m-%d}{suffix}.md) · {summary}  ")
            lines.append(f"  {_gist(edition_rows)}")
    return "\n".join(lines) + "\n"


def _gist(rows: list[dict[str, Any]]) -> str:
    """当天的三条要点；没有要点（未启用大模型）时用前三条的标题。"""
    picked = sorted((r for r in rows if r.get("highlight")), key=lambda r: r.get("highlight_rank") or 0) or rows[:3]
    links = []
    for row in picked:
        text = md_text(str(row.get("highlight") or row.get("title") or ""))
        url = safe_url(str(row.get("url") or ""))
        links.append(f"[{text}]({url})" if url else text)
    return "；".join(links)
