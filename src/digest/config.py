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
class Config:
    title: str
    timezone: str
    max_items: int
    archive_base_url: str
    sections: tuple[Section, ...]
    sources: dict[str, dict[str, Any]]
    focus: tuple[Focus, ...] = ()
    min_outside_focus: float = 0.0
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
    sources = _require(raw, "sources", "sources.yaml") or {}
    for name, opts in sources.items():
        if opts.get("enabled", True) and opts.get("section") not in section_keys:
            raise ConfigError(f"信息源 {name!r} 的 section 不在 sections 里")

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

    return Config(
        title=str(digest.get("title", "每日简报")),
        timezone=str(digest.get("timezone", "Asia/Shanghai")),
        max_items=int(digest.get("max_items", 15)),
        archive_base_url=str(digest.get("archive_base_url") or ""),
        sections=sections,
        sources=sources,
        focus=focus,
        min_outside_focus=min_outside,
    )
