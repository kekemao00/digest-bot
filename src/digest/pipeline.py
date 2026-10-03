from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from digest.config import Config, Section
from digest.interests import InterestMatcher
from digest.models import Also, Highlight, Item
from digest.sources.base import Source
from digest.state import State
from digest.urls import canonical_url

# 同一内容同时出现在多个来源，说明关注度更高
MULTI_SOURCE_BOOST = 1.2
# 大模型审阅最多几轮：第一轮审两倍配额的候选，之后只审因淘汰而补进名单的条目
MAX_REVIEW_ROUNDS = 3

# 审阅函数：给条目补上中文标题等字段，返回要淘汰的条目 {Item.key: 原因}
Review = Callable[[list[Item]], dict[str, str]]


@dataclass
class Rejected:
    item: Item
    reason: str


@dataclass
class Selection:
    sections: list[tuple[Section, list[Item]]]
    rejected: list[Rejected] = field(default_factory=list)
    candidates: int = 0
    highlights: list[Highlight] = field(default_factory=list)

    @property
    def items(self) -> list[Item]:
        return [item for _, items in self.sections for item in items]


def quality_factor(quality: int | None) -> float:
    """大模型评分对排序的影响：5 分不变，每高一分加 8%。没有评分时不影响。"""
    return 1.0 if quality is None else 1.0 + 0.08 * (quality - 5)


def merge_duplicates(candidates: list[Item], order: dict[str, int]) -> tuple[list[Item], list[Rejected]]:
    """同一篇文章在多个来源出现时合并为一条：保留配置里靠前的来源，其他来源作为“另见”。"""
    groups: dict[str, list[Item]] = defaultdict(list)
    for item in candidates:
        groups[item.canonical].append(item)
    merged: list[Item] = []
    rejected: list[Rejected] = []
    for group in groups.values():
        group.sort(key=lambda i: order.get(i.source, len(order)))
        primary, rest = group[0], group[1:]
        for other in rest:
            if other.source == primary.source:
                rejected.append(Rejected(other, "重复条目"))
                continue
            link = other.discussion_url or other.url
            # 另一来源只是同一链接（如 arXiv 原文）时不再重复列出
            if canonical_url(link) != primary.canonical:
                primary.also.append(Also(other.source_name, link))
            rejected.append(Rejected(other, f"与 {primary.source_name} 的同一内容合并"))
        merged.append(primary)
    return merged, rejected


def interleave(items: list[Item], sources: dict[str, Source]) -> list[Item]:
    """板块内多个来源按权重交替排列：不同来源的热度数值不能直接比较，比较的是“在本来源的名次”。"""
    by_source: dict[str, list[Item]] = defaultdict(list)
    for item in sorted(items, key=lambda i: i.rank, reverse=True):
        by_source[item.source].append(item)
    keyed = []
    for source_id, source_items in by_source.items():
        weight = sources[source_id].weight or 1.0
        # 权重小于 1 的来源，第一条也会排在高权重来源的前几条之后
        keyed += [((pos + 1) / weight, -item.rank, item) for pos, item in enumerate(source_items)]
    return [item for *_, item in sorted(keyed, key=lambda k: (k[0], k[1]))]


def _rank(items: list[Item], sources: dict[str, Source], matcher: InterestMatcher) -> None:
    for item in items:
        # 大模型判断过侧重领域时以它为准，关键词匹配只是没有大模型时的近似
        item.focus = list(item.llm_focus) if item.llm_focus is not None else matcher.match(item)
        boost = MULTI_SOURCE_BOOST if item.also else 1.0
        base = sources[item.source].base_score(item)
        item.rank = base * matcher.weight(item.focus) * boost * quality_factor(item.quality)


def _allocate(
    items: list[Item], sources: dict[str, Source], config: Config, scale: int = 1
) -> tuple[list[tuple[Section, list[Item]]], list[Rejected]]:
    """按来源和板块配额、总量上限、侧重之外的保底名额分配名额。scale > 1 时按放大的配额挑出送审名单。"""
    qualified: dict[str, list[Item]] = {s.key: [] for s in config.sections}
    for item in items:
        qualified[item.section].append(item)

    # 每个来源有自己的条数上限
    rejected: list[Rejected] = []
    per_source: dict[str, int] = defaultdict(int)
    for key, section_items in qualified.items():
        kept = []
        for item in interleave(section_items, sources):
            cap = sources[item.source].max_items
            if cap is not None and per_source[item.source] >= cap * scale:
                rejected.append(Rejected(item, f"超出 {item.source_name} 每天 {cap} 条的上限"))
                continue
            per_source[item.source] += 1
            kept.append(item)
        qualified[key] = kept

    limits = {section.key: section.limit * scale for section in config.sections}
    # 跨板块比较时用“在本板块的相对名次”
    ordered = sorted(
        ((pos / limits[section.key], item) for section in config.sections for pos, item in enumerate(qualified[section.key])),
        key=lambda p: (p[0], -p[1].rank),
    )
    total = min(config.max_items * scale, sum(min(limits[k], len(v)) for k, v in qualified.items()))
    outside_available = sum(min(limits[k], sum(1 for i in v if not i.focus)) for k, v in qualified.items())
    reserve = min(outside_available, math.ceil(config.min_outside_focus * total)) if config.focus else 0

    chosen: list[Item] = []
    chosen_keys: set[str] = set()
    per_section = {key: 0 for key in limits}

    def take(item: Item) -> None:
        chosen.append(item)
        chosen_keys.add(item.key)
        per_section[item.section] += 1

    # 先给侧重领域之外的内容留出保底名额，再按名次填满
    for _, item in ordered:
        if sum(1 for i in chosen if not i.focus) >= reserve:
            break
        if not item.focus and per_section[item.section] < limits[item.section]:
            take(item)
    for _, item in ordered:
        if len(chosen) >= total:
            break
        if item.key not in chosen_keys and per_section[item.section] < limits[item.section]:
            take(item)

    for _, item in ordered:
        if item.key in chosen_keys:
            continue
        section = config.section_by_key[item.section]
        if per_section[item.section] >= limits[item.section]:
            rejected.append(Rejected(item, f"超出“{section.name}”板块上限 {section.limit} 条"))
        else:
            rejected.append(Rejected(item, f"超出全天总数上限 {config.max_items} 条"))

    sections = []
    for section in config.sections:
        # 板块内的展示顺序沿用交替排列后的名次
        section_items = [i for i in qualified[section.key] if i.key in chosen_keys]
        if section_items:
            sections.append((section, section_items))
    return sections, rejected


def select(
    candidates: list[Item],
    sources: list[Source],
    config: Config,
    state: State | None = None,
    review: Review | None = None,
) -> Selection:
    """把候选条目压成当天的简报：合并、跨天去重、门槛、大模型审阅、排序、配额、侧重之外的保底名额。"""
    by_source = {s.id: s for s in sources}
    order = {s.id: n for n, s in enumerate(sources)}
    matcher = InterestMatcher(config.focus)
    total_candidates = len({i.key for i in candidates})

    unique: dict[str, Item] = {}
    for item in candidates:
        unique.setdefault(item.key, item)

    rejected: list[Rejected] = []
    passed: list[Item] = []
    for item in unique.values():
        if state and state.was_sent(item):
            rejected.append(Rejected(item, "近期已推送过"))
        elif reason := by_source[item.source].reject_reason(item):
            rejected.append(Rejected(item, reason))
        else:
            passed.append(item)
    pool, merged_away = merge_duplicates(passed, order)
    rejected += merged_away

    if review:
        # 只审有机会入选的条目：第一轮多审一倍作为替补，之后每轮补审新进入名单的条目
        for round_ in range(MAX_REVIEW_ROUNDS):
            _rank(pool, by_source, matcher)
            sections, _ = _allocate(pool, by_source, config, scale=2 if round_ == 0 else 1)
            todo = [item for _, section_items in sections for item in section_items if not item.reviewed]
            if not todo:
                break
            verdicts = review(todo)
            for item in todo:
                item.reviewed = True
            rejected += [Rejected(item, verdicts[item.key]) for item in pool if item.key in verdicts]
            pool = [item for item in pool if item.key not in verdicts]

    _rank(pool, by_source, matcher)
    sections, not_chosen = _allocate(pool, by_source, config)
    rejected += not_chosen
    rejected.sort(key=lambda r: r.item.rank, reverse=True)
    return Selection(sections=sections, rejected=rejected, candidates=total_candidates)
