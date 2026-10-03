from __future__ import annotations

import math
from dataclasses import dataclass, field

from digest.config import Config, Section
from digest.interests import InterestMatcher
from digest.models import Item
from digest.sources.base import Source


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


def select(candidates: list[Item], sources: list[Source], config: Config) -> Selection:
    """把候选条目压成当天的简报：门槛、去重、排序、板块配额、总量上限、侧重领域之外的保底名额。"""
    by_source = {s.name: s for s in sources}
    matcher = InterestMatcher(config.focus)
    rejected: list[Rejected] = []

    seen: set[str] = set()
    qualified: dict[str, list[Item]] = {s.key: [] for s in config.sections}
    for item in candidates:
        if item.key in seen:
            continue
        seen.add(item.key)
        source = by_source[item.source]
        if reason := source.reject_reason(item):
            rejected.append(Rejected(item, reason))
            continue
        item.focus = matcher.match(item)
        item.rank = source.base_score(item) * matcher.weight(item.focus)
        qualified[item.section].append(item)

    for items in qualified.values():
        items.sort(key=lambda i: i.rank, reverse=True)

    # 跨板块比较时用“在本板块的相对名次”：不同来源的热度数值不能直接比较
    ordered = sorted(
        ((pos / section.limit, item) for section in config.sections for pos, item in enumerate(qualified[section.key])),
        key=lambda p: (p[0], -p[1].rank),
    )
    limits = {section.key: section.limit for section in config.sections}
    total = min(config.max_items, sum(min(limits[k], len(v)) for k, v in qualified.items()))
    outside_available = sum(min(limits[k], sum(1 for i in v if not i.focus)) for k, v in qualified.items())
    reserve = min(outside_available, math.ceil(config.min_outside_focus * total)) if config.focus else 0

    chosen: list[Item] = []
    per_section = {key: 0 for key in limits}

    def take(item: Item) -> None:
        chosen.append(item)
        per_section[item.section] += 1

    # 先给侧重领域之外的内容留出保底名额，再按名次填满
    for _, item in ordered:
        if sum(1 for i in chosen if not i.focus) >= reserve:
            break
        if not item.focus and per_section[item.section] < limits[item.section]:
            take(item)
    chosen_keys = {i.key for i in chosen}
    for _, item in ordered:
        if len(chosen) >= total:
            break
        if item.key not in chosen_keys and per_section[item.section] < limits[item.section]:
            take(item)
            chosen_keys.add(item.key)

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
        items = sorted((i for i in chosen if i.section == section.key), key=lambda i: i.rank, reverse=True)
        if items:
            sections.append((section, items))
    rejected.sort(key=lambda r: r.item.rank or r.item.score, reverse=True)
    return Selection(sections=sections, rejected=rejected, candidates=len(seen))
