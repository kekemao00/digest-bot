from __future__ import annotations

import json

import httpx

from conftest import NOW, fetch_with, fixture_text, source_options
from digest.sources.feeds import Feed
from digest.sources.github_trending import GitHubTrending
from digest.sources.hf_papers import HFDailyPapers
from digest.sources.lobsters import Lobsters
from digest.sources.pages import NewsPage
from digest.state import State


def test_lobsters(config):
    source = Lobsters(source_options(config, "lobsters"))
    items = fetch_with(source, fixture_text("lobsters.json"))
    titles = [i.title for i in items]
    assert "An old story" not in titles  # 超出时间窗
    fed = items[0]
    assert fed.extras == ["#finance", "#news"]
    assert fed.discussion_url == "https://lobste.rs/s/abc123/the_fed_signals"
    ask = next(i for i in items if i.title.startswith("Ask"))
    assert ask.url == ask.discussion_url
    assert "热度不足" in source.reject_reason(ask)


def test_github_trending(config):
    source = GitHubTrending(source_options(config, "github-trending"))
    items = fetch_with(source, fixture_text("github_trending.html"))
    assert [i.title for i in items] == ["acme/agent-kit", "someone/dotfiles"]
    kit = items[0]
    assert kit.url == "https://github.com/acme/agent-kit"
    assert kit.summary == "A toolkit for building LLM agents & tools, with batteries included."
    assert kit.extras == ["Python", "12,345 star"]
    assert kit.score == 1024 and kit.score_text == "今日 +1,024"
    assert source.reject_reason(kit) is None
    assert "热度不足" in source.reject_reason(items[1])


def test_github_trending_layout_change_is_an_error(config):
    source = GitHubTrending(source_options(config, "github-trending"))
    try:
        fetch_with(source, "<html>new layout</html>")
    except ValueError as exc:
        assert "结构" in str(exc)
    else:
        raise AssertionError("应当报错")


def test_nature_keeps_research_articles_only(config):
    source = Feed(source_options(config, "nature"))
    items = fetch_with(source, fixture_text("nature.rss"))
    assert [i.title for i in items] == [
        "A language model that discovers trading strategies",
        "Ancient DNA reveals early farming in the Sahara",
    ]
    assert items[0].summary == "We show that..."  # 去掉了期刊名和 DOI 的固定前缀
    assert items[1].summary is None


def test_arxiv_atom(config):
    source = Feed(source_options(config, "arxiv-qfin"))
    items = fetch_with(source, fixture_text("arxiv.atom"))
    assert len(items) == 1
    paper = items[0]
    assert paper.title == "Order Book Dynamics Under Latency Arbitrage"
    assert paper.extras == ["q-fin.TR"]
    assert paper.url == "https://arxiv.org/abs/2610.01234"
    assert paper.canonical == "arxiv.org/abs/2610.01234"


def test_hf_papers(config):
    source = HFDailyPapers(source_options(config, "hf-papers"))
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=json.loads(fixture_text("hf_papers.json")))

    items = fetch_with(source, handler)
    assert seen["params"]["date"] == "2026-10-02"  # 北京时间 10 月 3 日早上取前一天（UTC）的榜单
    first = items[0]
    assert first.url == "https://arxiv.org/abs/2610.01234"
    assert first.discussion_url == "https://huggingface.co/papers/2610.01234"
    assert first.links == [("代码", "https://github.com/quant/obd")]
    assert first.score == 45 and first.score_unit == "赞"
    assert "热度不足" in source.reject_reason(items[2])


def anthropic_handler(article_titles: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/news":
            return httpx.Response(200, text=fixture_text("news_list.html"))
        title = article_titles[request.url.path]
        return httpx.Response(
            200,
            text=f'<html><head><meta property="og:title" content="{title}">'
            f'<meta name="description" content="Short summary &amp; more."></head></html>',
        )

    return handler


def test_page_source_first_run_only_records(config, tmp_path):
    source = NewsPage(source_options(config, "anthropic"))
    state = State(tmp_path / "state.json")
    assert fetch_with(source, anthropic_handler({}), state) == []
    assert state.pages["anthropic"] == [
        "https://www.anthropic.com/news/claude-frontier-academy",
        "https://www.anthropic.com/news/barclays-scales-claude",
    ]


def test_page_source_reports_new_articles(config):
    source = NewsPage(source_options(config, "anthropic"))
    state = State(None)
    state.pages["anthropic"] = ["https://www.anthropic.com/news/barclays-scales-claude"]
    items = fetch_with(source, anthropic_handler({"/news/claude-frontier-academy": "Frontier Academy"}), state)
    assert len(items) == 1
    assert items[0].title == "Frontier Academy"
    assert items[0].summary == "Short summary & more."
    assert items[0].published_at == NOW
    # 客户案例被排除规则挡掉
    customer = source.item(id="x", title="Barclays scales Claude", url="https://x.test", published_at=NOW)
    assert source.reject_reason(customer) == "标题命中排除规则"


def test_science_keeps_research_articles_only(config):
    source = Feed(source_options(config, "science"))
    items = fetch_with(source, fixture_text("science.rss"))
    assert [i.title for i in items] == ["A topological p-wave superconductor", "Market crashes and the speed of trading"]
    assert items[0].summary == "We report a superconductor that is topological in nature and robust."
    assert items[1].summary is None


def test_science_online_article_boilerplate(config):
    from digest.sources.feeds import _BOILERPLATE

    text = "Science, Volume 394, Issue 6819, October 2026. Lipid cycling links clock and diet."
    assert _BOILERPLATE.sub("", text) == "Lipid cycling links clock and diet."


def test_empty_feed_is_reported(config):
    source = Feed(source_options(config, "nature"))
    try:
        fetch_with(source, "<html><body>Access denied</body></html>")
    except ValueError as exc:
        assert "被拦截" in str(exc)
    else:
        raise AssertionError("应当报错")


def test_crossref_requires_abstract(config):
    from digest.sources.crossref import Crossref

    source = Crossref(source_options(config, "pnas"))
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["filter"] = request.url.params["filter"]
        return httpx.Response(200, text=fixture_text("crossref_pnas.json"))

    items = fetch_with(source, handler)
    assert seen["path"] == "/journals/0027-8424/works"
    assert seen["filter"] == "from-pub-date:2026-09-30,type:journal-article"
    assert len(items) == 1
    assert items[0].title == "Liquidity spirals in algorithmic markets"
    assert items[0].url == "https://doi.org/10.1073/pnas.2601234123"
    assert items[0].summary == "We show how algorithmic traders amplify liquidity shocks."


def test_journal_notices_excluded(config):
    source = Feed(source_options(config, "nature"))
    notice = source.item(id="n", title="Retraction Note: Something", url="https://x.test", published_at=NOW)
    assert source.reject_reason(notice) == "标题命中排除规则"
