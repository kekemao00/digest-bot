from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Focus:
    key: str
    name: str
    weight: float
    keywords: tuple[str, ...]


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
class Config:
    title: str
    timezone: str
    max_items: int
    archive_base_url: str
    sections: tuple[Section, ...]
    sources: tuple[dict[str, Any], ...]
    focus: tuple[Focus, ...] = ()
    min_outside_focus: float = 0.0
    llm: LLMSettings = LLMSettings()
    section_by_key: dict[str, Section] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "section_by_key", {s.key: s for s in self.sections})


def _require(data: dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ConfigError(f"{where} 缺少字段 {key!r}")
    return data[key]


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    raw = yaml.safe_load((config_dir / "sources.yaml").read_text(encoding="utf-8")) or {}
    digest = _require(raw, "digest", "sources.yaml")
    sections = tuple(
        Section(key=str(s["key"]), name=str(s["name"]), limit=int(s["limit"]))
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

    return Config(
        title=str(digest.get("title", "每日简报")),
        timezone=str(digest.get("timezone", "Asia/Shanghai")),
        max_items=int(digest.get("max_items", 15)),
        archive_base_url=str(digest.get("archive_base_url") or ""),
        sections=sections,
        sources=sources,
        focus=focus,
        min_outside_focus=min_outside,
        llm=llm,
    )
