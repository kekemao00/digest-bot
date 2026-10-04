from __future__ import annotations

from pathlib import Path

import yaml
from conftest import NOW

from digest.cli import edition_for_cron
from digest.models import Highlight
from digest.pipeline import select
from digest.render import archive, dingtalk

ROOT = Path(__file__).resolve().parent.parent


def test_evening_config_keeps_daytime_sections(config):
    evening = config.for_evening()
    assert evening.is_evening and not config.is_evening
    assert [s.key for s in evening.sections] == ["world", "tech", "labs"]
    assert evening.max_items == 8 and evening.llm.highlights == 2
    assert {opts["section"] for opts in evening.sources} == {"world", "tech", "labs"}
    # 只看最近 14 小时；原来更短的窗口保持不变
    windows = {opts["id"]: opts["window_hours"] for opts in evening.sources}
    assert windows["bbc-world"] == 14 and windows["openai"] == 14
    assert all(w <= 14 for w in windows.values())
    # 早间版的配置不受影响
    assert next(o for o in config.sources if o["id"] == "bbc-world")["window_hours"] == 36
    assert config.edition_title == "每日简报" and evening.edition_title == "晚间简报"
    assert evening.title == "每日简报"  # 归档目录页等不分版的地方仍用原标题


def test_github_crons_map_to_editions():
    # 工作流里 GitHub 自带的定时任务带不了参数，靠 cron 的钟点区分早晚两版
    workflow = yaml.safe_load((ROOT / ".github/workflows/daily.yml").read_text(encoding="utf-8"))
    crons = [entry["cron"] for entry in workflow[True]["schedule"]]
    editions = [edition_for_cron(cron, "Asia/Shanghai") for cron in crons]
    assert editions == ["morning"] * 3 + ["evening"] * 3
    # README 里“中午 12 点前后收到”的示例仍是早间版
    assert {edition_for_cron(c, "Asia/Shanghai") for c in ("47 3 * * *", "37 4 * * *", "37 5 * * *")} == {"morning"}
    assert edition_for_cron("bad", "Asia/Shanghai") == "morning"


def test_evening_render(config, hn_source, hn_items):
    evening = config.for_evening()
    selection = select(hn_items, [hn_source], evening)
    selection.highlights = [Highlight(i.key, f"要点 {n}") for n, i in enumerate(selection.items[:2], 1)]
    message = dingtalk.render(selection, evening, NOW)
    assert message.title.startswith(f"晚间简报 {len(selection.items)} 条｜要点 1")
    assert message.text.startswith("### 晚间简报 · ") and "**今晚要点**" in message.text
    assert "今日要点" not in message.text
    full = archive.render(selection, evening, NOW)
    assert full.startswith("# 晚间简报 · 2026-10-03") and "## 今晚要点" in full
