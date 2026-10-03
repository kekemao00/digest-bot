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


def test_merges_same_story_across_sources(config, hn_source, hn_items):
    from conftest import fetch_with, fixture_text, source_options
    from digest.sources.lobsters import Lobsters

    lobsters = Lobsters(source_options(config, "lobsters"))
    lob_items = fetch_with(lobsters, fixture_text("lobsters.json"))
    selection = select(hn_items + lob_items, [hn_source, lobsters], config)
    fed = next(i for i in selection.items if "Fed signals" in i.title)
    assert fed.source == "hn"  # 配置里靠前的来源为主
    assert [a.source_name for a in fed.also] == ["Lobsters"]
    assert any("合并" in r.reason for r in selection.rejected)


def test_already_sent_items_are_skipped(config, hn_source, hn_items):
    from datetime import date

    from digest.state import State

    state = State(None)
    first = select(hn_items, [hn_source], config, state)
    state.mark_sent(first.items, date(2026, 10, 2))
    second = select(hn_items, [hn_source], config, state)
    assert not {i.key for i in first.items} & {i.key for i in second.items}
    assert any(r.reason == "近期已推送过" for r in second.rejected)


def test_per_source_cap_and_interleaving(config):
    from conftest import NOW
    from digest.sources.feeds import Feed

    big = replace(config, sections=(Section("labs", "实验室动态", 3),), focus=())
    a = Feed({"id": "a", "type": "feed", "section": "labs", "url": "x", "max_items": 1})
    b = Feed({"id": "b", "type": "feed", "section": "labs", "url": "x", "weight": 0.5})
    a.now = b.now = NOW

    def post(source, n):
        return source.item(id=f"{source.id}{n}", title=f"Post {source.id}{n}", url=f"https://{source.id}.test/{n}",
                           published_at=NOW)

    items = [post(a, 1), post(a, 2), post(b, 1), post(b, 2), post(b, 3)]
    selection = select(items, [a, b], big)
    assert [i.id for i in selection.items] == ["a1", "b1", "b2"]
    assert any("每天 1 条" in r.reason for r in selection.rejected)


def test_review_replaces_rejected_items(config, hn_source, hn_items):
    batches = []

    def review(items):
        batches.append([i.key for i in items])
        return {i.key: "大模型评分 2/10（观点）" for i in items if "Voyager" in i.title}

    selection = select(hn_items, [hn_source], config, review=review)
    assert len(selection.items) == 5  # 被淘汰的由替补补上
    assert not any("Voyager" in i.title for i in selection.items)
    assert all(i.reviewed for i in selection.items)
    assert any(r.reason == "大模型评分 2/10（观点）" for r in selection.rejected)
    # 第一轮就带上替补一起审，之后不会重复审同一条
    assert len(batches[0]) > 5
    reviewed = [key for batch in batches for key in batch]
    assert len(reviewed) == len(set(reviewed))


def test_review_rounds_cover_late_replacements(config, hn_source):
    one = replace(config, sections=(Section("tech", "科技热议", 2),), max_items=2, focus=())
    items = [make(i, f"Story {i}", 1000 - i) for i in range(10)]

    def review(batch):
        # 每轮把送审的都淘汰掉，替补要在下一轮补审
        return {i.key: "大模型判断为营销" for i in batch if int(i.id) < 6}

    selection = select(items, [hn_source], one, review=review)
    assert [i.id for i in selection.items] == ["6", "7"]
    assert all(i.reviewed for i in selection.items)


def test_model_judgement_drives_focus_and_order(config, hn_source):
    plain = replace(config, sections=(Section("tech", "科技热议", 2),), max_items=2)
    items = [make(1, "LLM benchmark roundup", 500), make(2, "Gardening tips", 500), make(3, "Bird migration", 500)]

    def review(batch):
        for item in batch:
            item.llm_focus = []  # 模型认为都不属于侧重领域，关键词 LLM 不再加权
            item.quality = 9 if item.id == "3" else 5
        return {}

    selection = select(items, [hn_source], plain, review=review)
    assert selection.items[0].id == "3"
    assert all(not i.focus for i in selection.items)
