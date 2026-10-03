from __future__ import annotations

import os
from pathlib import Path

from conftest import NOW
from digest.pipeline import select
from digest.render import archive, dingtalk

SNAPSHOTS = Path(__file__).parent / "snapshots"


def check_snapshot(name: str, actual: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("UPDATE_SNAPSHOTS") or not path.exists():
        path.write_text(actual, encoding="utf-8")
    assert actual == path.read_text(encoding="utf-8"), f"{name} 变了；确认无误后用 UPDATE_SNAPSHOTS=1 重新生成"


def test_dingtalk_snapshot(config, hn_source, hn_items):
    message = dingtalk.render(select(hn_items, [hn_source], config), config, NOW)
    check_snapshot("dingtalk.md", f"<!-- {message.title} -->\n\n{message.text}\n")


def test_archive_snapshot(config, hn_source, hn_items):
    check_snapshot("archive.md", archive.render(select(hn_items, [hn_source], config), config, NOW))


def test_message_respects_size_limit(config, hn_source, hn_items, monkeypatch):
    monkeypatch.setattr(dingtalk, "MAX_BYTES", 900)
    message = dingtalk.render(select(hn_items, [hn_source], config), config, NOW)
    assert len(message.text.encode()) <= 900
    assert message.text.count("**") >= 2  # 至少还留着一条


def test_quiet_day_message(config):
    from digest.pipeline import Selection

    message = dingtalk.render(Selection(sections=[]), config, NOW)
    assert "0 条" in message.text


def test_all_sections_snapshot(config, hn_source, hn_items):
    import json

    import httpx

    from conftest import fetch_with, fixture_text, source_options
    from digest.sources.feeds import Feed
    from digest.sources.github_trending import GitHubTrending
    from digest.sources.hf_papers import HFDailyPapers
    from digest.sources.lobsters import Lobsters

    lobsters = Lobsters(source_options(config, "lobsters"))
    trending = GitHubTrending(source_options(config, "github-trending"))
    papers = HFDailyPapers(source_options(config, "hf-papers"))
    nature = Feed(source_options(config, "nature"))
    arxiv = Feed(source_options(config, "arxiv-qfin"))
    candidates = [
        *hn_items,
        *fetch_with(lobsters, fixture_text("lobsters.json")),
        *fetch_with(trending, fixture_text("github_trending.html")),
        *fetch_with(papers, lambda r: httpx.Response(200, json=json.loads(fixture_text("hf_papers.json")))),
        *fetch_with(nature, fixture_text("nature.rss")),
        *fetch_with(arxiv, fixture_text("arxiv.atom")),
    ]
    sources = [hn_source, lobsters, papers, arxiv, nature, trending]
    selection = select(candidates, sources, config)
    message = dingtalk.render(selection, config, NOW)
    check_snapshot("dingtalk_all_sections.md", f"<!-- {message.title} -->\n\n{message.text}\n")
