from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone

from digest.config import Section
from digest.models import Item
from digest.pipeline import select


def make(i: int, title: str, points: int) -> Item:
    return Item(source="hn", source_name="Hacker News", section="tech", id=str(i), title=title,
                url=f"https://example.com/{i}", published_at=datetime.now(timezone.utc), score=points)


def test_section_limit_and_rejections(config, hn_source, hn_items):
    selection = select(hn_items, [hn_source], config)
    assert len(selection.items) == 5
    reasons = {r.item.title: r.reason for r in selection.rejected}
    assert "热度不足" in reasons["Small startup postmortem"]
    assert any("板块上限" in r for r in reasons.values())


def test_focus_items_ranked_higher(config, hn_source, hn_items):
    selection = select(hn_items, [hn_source], config)
    ranks = {i.title: i.rank for i in hn_items}
    # 分数相近时，侧重领域的条目因为加权排在前面
    assert ranks["Claude and GPT compared on agent benchmarks"] > ranks["A deep dive into the *new* Linux scheduler"]
    assert selection.items == sorted(selection.items, key=lambda i: i.rank, reverse=True)


def test_reserves_slots_outside_focus(config, hn_source):
    big = replace(config, max_items=6, sections=(Section("tech", "科技热议", 6),))
    focus = [make(i, f"LLM news number {i}", 1000 - i) for i in range(10)]
    other = [make(100 + i, f"Gardening tips {i}", 300 - i) for i in range(5)]
    selection = select(focus + other, [hn_source], big)
    outside = [i for i in selection.items if not i.focus]
    assert len(selection.items) == 6
    assert len(outside) >= math.ceil(big.min_outside_focus * 6)


def test_reserve_does_not_invent_items(config, hn_source):
    focus = [make(i, f"LLM news number {i}", 1000 - i) for i in range(5)]
    selection = select(focus, [hn_source], config)
    assert len(selection.items) == 5  # 侧重之外没有达标内容时不留空位


def test_global_cap(config, hn_source):
    capped = replace(config, max_items=2)
    items = [make(i, f"Story {i}", 500 + i) for i in range(4)]
    selection = select(items, [hn_source], capped)
    assert len(selection.items) == 2
    assert any("全天总数" in r.reason for r in selection.rejected)


def test_duplicates_removed(config, hn_source):
    a = make(1, "Same story", 500)
    selection = select([a, replace(a)], [hn_source], config)
    assert len(selection.items) == 1
