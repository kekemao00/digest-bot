from __future__ import annotations

from datetime import datetime, timezone

from digest.interests import InterestMatcher
from digest.models import Item


def item(title: str) -> Item:
    return Item(source="hn", source_name="HN", section="tech", id="1", title=title, url="https://x.test",
                published_at=datetime.now(timezone.utc))


def test_focus_matches(config):
    m = InterestMatcher(config.focus)
    assert m.match(item("New LLM beats GPT on reasoning")) == ["ai"]
    assert m.match(item("The Fed raises interest rates")) == ["finance"]
    assert m.match(item("A primer on market making")) == ["trading"]
    assert m.match(item("国内大模型价格战")) == ["ai"]


def test_short_acronyms_are_case_sensitive(config):
    m = InterestMatcher(config.focus)
    assert m.match(item("Fed up with slow builds")) == []
    assert m.match(item("The Fed holds rates steady")) == ["finance"]
    assert m.match(item("Said the aide")) == []
    assert m.match(item("The ai of it all")) == []


def test_no_partial_words(config):
    m = InterestMatcher(config.focus)
    assert m.match(item("Maintainers wanted")) == []  # 不能因为 "ai" 子串命中
    assert m.match(item("Stockholm travel guide")) == []
