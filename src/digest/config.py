from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Section:
    key: str
    name: str
    limit: int
    icon: str = ""  # 板块标题前的 emoji


@dataclass(frozen=True)
class Focus:
    key: str
    name: str
    weight: float
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Diversity:
    """防止某一领域刷屏。依据大模型给的领域标签，没有大模型时不生效。"""

    max_topic_share: float = 0.4  # 单一领域每天最多占总条数的比例
    min_topics: int = 4  # 每天至少覆盖几个领域（候选里有的话）
    topic_decay: float = 0.85  # 同一来源里同领域每多一条，排序分乘以这个系数


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool = True
    temperature: float = 0.2
    batch_size: int = 12
    timeout: float = 90.0
    time_budget: float = 300.0
    max_tokens: int = 4096
    json_mode: bool = False
    min_score: int = 5
    reject_kinds: tuple[str, ...] = ("营销", "招聘", "活动")
    highlights: int = 3


@dataclass(frozen=True)
class Evening:
    """晚间版：只看白天会更新的板块，早上推过的不再出现，条数少；达标的太少就不发。"""

    enabled: bool = True
    title: str = "晚间简报"
    max_items: int = 8
    min_items: int = 3  # 达标的少于这么多条，当晚不推送
    sections: tuple[str, ...] = ("world", "tech", "labs")
    window_hours: float = 14  # 只看最近多少小时内发布的内容，大致从早间版推送前后算起
    highlights: int = 2
    latest_hour: int = 23  # 定时触发到这个钟点还没推送，当晚就不再推，避免深夜打扰


@dataclass(frozen=True)
class Config:
    title: str
    timezone: str
    max_items: int
    archive_base_url: str
    sections: tuple[Section, ...]
    sources: tuple[dict[str, Any], ...]
    focus: tuple[Focus, ...] = ()
    min_outside_focus: float = 0.0
    diversity: Diversity = Diversity()
    llm: LLMSettings = LLMSettings()
    evening: Evening = Evening()
    edition: str = "morning"  # morning 早间版，evening 晚间版
    section_by_key: dict[str, Section] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "section_by_key", {s.key: s for s in self.sections})

    @property
    def is_evening(self) -> bool:
        return self.edition == "evening"

    @property
    def edition_title(self) -> str:
        """消息和完整版里显示的标题；title 本身用于归档目录页等不分版的地方。"""
        return self.evening.title if self.is_evening else self.title

    @property
    def highlights_heading(self) -> str:
        return "今晚要点" if self.is_evening else "今日要点"

    def for_evening(self) -> Config:
        """晚间版的配置：只保留晚间版的板块和来源，各来源只看最近 window_hours 小时内的内容。
        没有发布时间的来源（比较列表页的博客）本来就只推新出现的文章，不受影响。"""
        evening = self.evening
        sources = []
        for opts in self.sources:
            if opts.get("section") not in evening.sections:
                continue
            window = min(float(opts.get("window_hours", evening.window_hours)), evening.window_hours)
            sources.append({**opts, "window_hours": window})
        return replace(
            self,
            edition="evening",
            max_items=evening.max_items,
            sections=tuple(s for s in self.sections if s.key in evening.sections),
            sources=tuple(sources),
            llm=replace(self.llm, highlights=evening.highlights),
        )


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ConfigError(f"{where} 缺少字段 {key!r}")
    return data[key]


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    raw = yaml.safe_load((config_dir / "sources.yaml").read_text(encoding="utf-8")) or {}
    digest = _require(raw, "digest", "sources.yaml")
    sections = tuple(
        Section(key=str(s["key"]), name=str(s["name"]), limit=int(s["limit"]), icon=str(s.get("icon") or ""))
        for s in _require(raw, "sections", "sources.yaml")
    )
    if any(s.limit < 1 for s in sections):
        raise ConfigError("板块的 limit 至少为 1")
    section_keys = {s.key for s in sections}
    sources = tuple(_require(raw, "sources", "sources.yaml") or ())
    ids: set[str] = set()
    for opts in sources:
        source_id = str(_require(opts, "id", "sources 里的条目"))
        if source_id in ids:
            raise ConfigError(f"信息源 id {source_id!r} 重复")
        ids.add(source_id)
        _require(opts, "type", f"信息源 {source_id!r}")
        if opts.get("section") not in section_keys:
            raise ConfigError(f"信息源 {source_id!r} 的 section 不在 sections 里")

    focus: tuple[Focus, ...] = ()
    min_outside = 0.0
    diversity = Diversity()
    interests_path = config_dir / "interests.yaml"
    if interests_path.exists():
        interests = yaml.safe_load(interests_path.read_text(encoding="utf-8")) or {}
        focus = tuple(
            Focus(
                key=str(f["key"]),
                name=str(f["name"]),
                weight=float(f.get("weight", 1.0)),
                keywords=tuple(str(k) for k in f.get("keywords", [])),
            )
            for f in interests.get("focus", [])
        )
        min_outside = float(interests.get("min_outside_focus", 0.0))
        if not 0.0 <= min_outside <= 1.0:
            raise ConfigError("min_outside_focus 必须在 0 到 1 之间")
        raw_div = interests.get("diversity") or {}
        diversity = Diversity(
            max_topic_share=float(raw_div.get("max_topic_share", diversity.max_topic_share)),
            min_topics=int(raw_div.get("min_topics", diversity.min_topics)),
            topic_decay=float(raw_div.get("topic_decay", diversity.topic_decay)),
        )
        if not 0.0 < diversity.max_topic_share <= 1.0 or not 0.0 < diversity.topic_decay <= 1.0:
            raise ConfigError("diversity 的 max_topic_share 和 topic_decay 必须在 0 到 1 之间")

    llm = LLMSettings()
    llm_path = config_dir / "llm.yaml"
    if llm_path.exists():
        raw_llm = yaml.safe_load(llm_path.read_text(encoding="utf-8")) or {}
        llm = LLMSettings(
            enabled=bool(raw_llm.get("enabled", llm.enabled)),
            temperature=float(raw_llm.get("temperature", llm.temperature)),
            batch_size=int(raw_llm.get("batch_size", llm.batch_size)),
            timeout=float(raw_llm.get("timeout", llm.timeout)),
            time_budget=float(raw_llm.get("time_budget", llm.time_budget)),
            max_tokens=int(raw_llm.get("max_tokens", llm.max_tokens)),
            json_mode=bool(raw_llm.get("json_mode", llm.json_mode)),
            min_score=int(raw_llm.get("min_score", llm.min_score)),
            reject_kinds=tuple(str(k) for k in raw_llm.get("reject_kinds", llm.reject_kinds)),
            highlights=int(raw_llm.get("highlights", llm.highlights)),
        )
        if llm.batch_size < 1:
            raise ConfigError("llm.yaml 的 batch_size 至少为 1")

    evening = Evening()
    if raw_evening := raw.get("evening"):
        evening = Evening(
            enabled=bool(raw_evening.get("enabled", evening.enabled)),
            title=str(raw_evening.get("title", evening.title)),
            max_items=int(raw_evening.get("max_items", evening.max_items)),
            min_items=int(raw_evening.get("min_items", evening.min_items)),
            sections=tuple(str(k) for k in raw_evening.get("sections", evening.sections)),
            window_hours=float(raw_evening.get("window_hours", evening.window_hours)),
            highlights=int(raw_evening.get("highlights", evening.highlights)),
            latest_hour=int(raw_evening.get("latest_hour", evening.latest_hour)),
        )
    if unknown := [k for k in evening.sections if k not in section_keys]:
        raise ConfigError(f"evening.sections 里的 {', '.join(unknown)} 不在 sections 里")

    return Config(
        title=str(digest.get("title", "每日简报")),
        timezone=str(digest.get("timezone", "Asia/Shanghai")),
        max_items=int(digest.get("max_items", 15)),
        archive_base_url=str(digest.get("archive_base_url") or ""),
        sections=sections,
        sources=sources,
        focus=focus,
        min_outside_focus=min_outside,
        diversity=diversity,
        llm=llm,
        evening=evening,
    )
