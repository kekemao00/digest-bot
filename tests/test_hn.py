from __future__ import annotations

import httpx

from conftest import NOW


def test_query_uses_time_window(hn_source, hn_payload):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=hn_payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hn_source.fetch(client, NOW)
    since = int(NOW.timestamp()) - 24 * 3600
    assert seen["params"]["tags"] == "story"
    assert f"created_at_i>{since}" in seen["params"]["numericFilters"]


def test_skips_hiring_threads(hn_items):
    assert not any("Who is hiring" in i.title for i in hn_items)
    assert len(hn_items) == 11


def test_ask_hn_links_to_discussion(hn_items):
    ask = next(i for i in hn_items if i.title.startswith("Ask HN"))
    assert ask.url == ask.discussion_url == f"https://news.ycombinator.com/item?id={ask.id}"
    assert ask.domain is None


def test_threshold_accepts_points_or_comments(hn_source, hn_items):
    by_title = {i.title: i for i in hn_items}
    assert hn_source.reject_reason(by_title["Ask HN: What's your favorite underrated book?"]) is None
    assert "热度不足" in hn_source.reject_reason(by_title["Small startup postmortem"])
