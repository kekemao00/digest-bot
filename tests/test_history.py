from __future__ import annotations

import json
from datetime import date

from digest import history
from digest.models import Highlight
from digest.pipeline import select


def selection_for(config, hn_source, hn_items, highlights=0):
    selection = select(hn_items, [hn_source], config)
    selection.highlights = [Highlight(i.key, f"要点 {n}") for n, i in enumerate(selection.items[:highlights], 1)]
    return selection


def test_index_and_readme(config, hn_source, hn_items, tmp_path):
    selection = selection_for(config, hn_source, hn_items, highlights=2)
    selection.items[0].topic = "AI"
    history.update(tmp_path, selection, config, date(2026, 10, 4))

    rows = [json.loads(line) for line in (tmp_path / "items.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(selection.items)
    first = rows[0]
    assert first["date"] == "2026-10-04" and first["n"] == 1 and first["section"] == "科技热议"
    assert first["url"] == selection.items[0].url and first["source"] == "HN" and first["topic"] == "AI"
    assert (first["highlight_rank"], first["highlight"]) == (1, "要点 1")

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "## 2026 年 10 月" in readme
    assert f"- [10月4日 周日](2026/10-04.md) · {len(rows)} 条｜AI 1 · 其他 {len(rows) - 1}" in readme
    assert f"[要点 1]({selection.items[0].url})；[要点 2]" in readme


def test_rerun_replaces_the_day_and_keeps_history(config, hn_source, hn_items, tmp_path):
    (tmp_path / "items.jsonl").write_text('{"date": "2026-09-30", "n": 1, "title": "旧条目", "url": "https://old.test"}\n'
                                          "这一行坏了\n", encoding="utf-8")
    selection = selection_for(config, hn_source, hn_items)
    history.update(tmp_path, selection, config, date(2026, 10, 4))
    history.update(tmp_path, selection, config, date(2026, 10, 4))  # 同一天重跑

    lines = (tmp_path / "items.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ['{"date": "2026-09-30", "n": 1, "title": "旧条目", "url": "https://old.test"}', "这一行坏了"]
    assert len(lines) == 2 + len(selection.items)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    # 新的在上；没有要点时用前三条标题
    assert readme.index("10月4日") < readme.index("9月30日")
    assert "[旧条目](https://old.test)" in readme
    assert "共 2 天" in readme
