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


def test_llm_snapshots(config, hn_source, hn_items):
    """有大模型时的样张：中文标题、原标题放进灰色小字、一句话、今日要点；低分条目进落选表。"""
    import httpx

    from conftest import fake_llm
    from digest.llm import ChatClient, Endpoint, Enricher

    client = httpx.Client(transport=httpx.MockTransport(fake_llm))
    tool = Enricher(ChatClient(client, Endpoint("https://llm.test/v1/chat/completions", "k", "m"), config.llm), config)
    selection = select(hn_items, [hn_source], config, review=tool.review)
    selection.highlights = tool.highlights(selection.items)
    message = dingtalk.render(selection, config, NOW)
    check_snapshot("dingtalk_llm.md", f"<!-- {message.title} -->\n\n{message.text}\n")
    check_snapshot("archive_llm.md", archive.render(selection, config, NOW))
    assert message.title.startswith("每日简报 5 条｜要点：")


def test_highlights_follow_trimming(config, hn_source, hn_items, monkeypatch):
    from digest.models import Highlight

    selection = select(hn_items, [hn_source], config)
    last = selection.items[-1]
    selection.highlights = [Highlight(last.key, "指向最后一条的要点")]
    monkeypatch.setattr(dingtalk, "MAX_BYTES", 1200)
    message = dingtalk.render(selection, config, NOW)
    # 最后一条被裁掉后，指向它的要点也不再出现
    assert "指向最后一条的要点" not in message.text + message.title


def test_topic_tags_and_summary(hn_items):
    a, b, c, d = hn_items[:4]
    a.topic, b.topic, c.topic, d.topic = "金融", "AI", "其他", "AI"
    assert dingtalk.topic_tag(b) == "🤖 AI"
    assert dingtalk.topic_tag(c) is None  # “其他”不显示标签
    # 多的在前，同样多时按 TOPICS 的顺序，“其他”和没判断出领域的放最后
    hn_items[4].topic = None
    assert dingtalk.topic_summary(hn_items[:5]) == "AI 2 · 金融 1 · 其他 2"
    for item in hn_items:
        item.topic = None
    assert dingtalk.topic_summary(hn_items) is None
