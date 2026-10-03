from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from digest.config import Config, Section
from digest.interests import InterestMatcher
from digest.models import Also, Item
from digest.sources.base import Source
from digest.state import State
from digest.urls import canonical_url

# 同一内容同时出现在多个来源，说明关注度更高
MULTI_SOURCE_BOOST = 1.2


@dataclass
class Rejected:
    item: Item
    reason: str


@dataclass
class Selection:
    sections: list[tuple[Section, list[Item]]]
    rejected: list[Rejected] = field(default_factory=list)
    candidates: int = 0

    @property
    def items(self) -> list[Item]:
        return [item for _, items in self.sections for item in items]


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


def select(candidates: list[Item], sources: list[Source], config: Config, state: State | None = None) -> Selection:
    """把候选条目压成当天的简报：合并、跨天去重、门槛、排序、来源和板块配额、总量上限、侧重之外的保底名额。"""
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
    merged, merged_away = merge_duplicates(passed, order)
    rejected += merged_away

    qualified: dict[str, list[Item]] = {s.key: [] for s in config.sections}
    for item in merged:
        item.focus = matcher.match(item)
        boost = MULTI_SOURCE_BOOST if item.also else 1.0
        item.rank = by_source[item.source].base_score(item) * matcher.weight(item.focus) * boost
        qualified[item.section].append(item)

    # 每个来源有自己的条数上限
    per_source: dict[str, int] = defaultdict(int)
    for key, items in qualified.items():
        kept = []
        for item in interleave(items, by_source):
            cap = by_source[item.source].max_items
            if cap is not None and per_source[item.source] >= cap:
                rejected.append(Rejected(item, f"超出 {item.source_name} 每天 {cap} 条的上限"))
                continue
            per_source[item.source] += 1
            kept.append(item)
        qualified[key] = kept

    # 跨板块比较时用“在本板块的相对名次”
    ordered = sorted(
        ((pos / section.limit, item) for section in config.sections for pos, item in enumerate(qualified[section.key])),
        key=lambda p: (p[0], -p[1].rank),
    )
    limits = {section.key: section.limit for section in config.sections}
    total = min(config.max_items, sum(min(limits[k], len(v)) for k, v in qualified.items()))
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
        if per_section[item.section] >= section.limit:
            rejected.append(Rejected(item, f"超出“{section.name}”板块上限 {section.limit} 条"))
        else:
            rejected.append(Rejected(item, f"超出全天总数上限 {config.max_items} 条"))

    sections = []
    for section in config.sections:
        # 板块内的展示顺序沿用交替排列后的名次
        items = [i for i in qualified[section.key] if i.key in chosen_keys]
        if items:
            sections.append((section, items))
    rejected.sort(key=lambda r: r.item.rank, reverse=True)
    return Selection(sections=sections, rejected=rejected, candidates=total_candidates)
