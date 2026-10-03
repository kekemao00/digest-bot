from __future__ import annotations

from datetime import date

from conftest import NOW
from digest.models import Item
from digest.state import State
from digest.urls import canonical_url


def test_canonical_url():
    assert canonical_url("https://www.example.com/a/?utm_source=x&b=2&a=1#frag") == "example.com/a?a=1&b=2"
    assert canonical_url("http://arxiv.org/pdf/2610.01234v2.pdf") == "arxiv.org/abs/2610.01234"
    assert canonical_url("https://huggingface.co/papers/2610.01234") == "arxiv.org/abs/2610.01234"
    assert canonical_url("https://github.com/Acme/Agent-Kit/blob/main/README.md") == "github.com/acme/agent-kit"
    assert canonical_url("https://m.example.com/") == "example.com/"


def item(url: str) -> Item:
    return Item(source="hn", source_name="HN", section="tech", id=url, title="t", url=url, published_at=NOW)


def test_state_round_trip_and_pruning(tmp_path):
    path = tmp_path / "state.json"
    state = State(path, keep_days=30)
    state.mark_sent([item("https://a.test/x")], date(2026, 8, 1))
    state.mark_sent([item("https://b.test/y?utm_source=z")], date(2026, 10, 3))
    state.save(date(2026, 10, 3))

    reloaded = State(path)
    assert not reloaded.was_sent(item("https://a.test/x"))  # 超过 30 天被清理
    assert reloaded.was_sent(item("https://www.b.test/y"))


def test_missing_or_foreign_state_is_first_run(tmp_path):
    assert State(tmp_path / "missing.json").sent == {}
    path = tmp_path / "old.json"
    path.write_text('{"version": 999, "sent": {"x": "2026-10-01"}}', encoding="utf-8")
    assert State(path).sent == {}


def test_blurb_rules():
    base = item("https://a.test")
    base.title = "Toward provably private learning"
    base.summary = "Mobile Systems"
    assert base.blurb is None  # 太短，多半是分类标签
    base.summary = "Toward provably private learning"
    assert base.blurb is None
    base.summary = "A toolkit for building LLM agents and tools, with batteries included."
    assert base.blurb == base.summary
    base.summary = "x" * 200
    assert base.blurb is None  # 长摘要不进消息
    base.one_liner = "一句话"
    assert base.blurb == "一句话"
