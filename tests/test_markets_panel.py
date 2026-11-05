from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import httpcore
import httpx
import pytest

import digest.markets as markets
from digest.markets import backtest, ledger, panel, score
from digest.markets.ledger import Ledger, keys_from_env
from digest.markets.net import Budget, Fetcher
from digest.markets.schedule import BJT
from digest.markets.select import GATE_NAMES
from digest.http import USER_AGENT, make_client
from digest.markets.net import FetchError
from digest.markets.settings import DISABLED_STATUS, BudgetSettings, MarketsSettings, NowcastSettings, VixSettings
from digest.markets.types import Line
from digest.models import Item
from test_markets_select import (
    COND_HI,
    COND_LO,
    COND_MOVE,
    COND_SAY,
    CPI,
    FED,
    INX,
    JOBS,
    NOW,
    NOW_TS,
    Q_HI,
    Q_LO,
    Q_MOVE,
    Q_SAY,
    REC,
    TOKEN_HI,
    TOKEN_LO,
    TOKEN_MOVE,
    TOKEN_SAY,
    cpi_event,
    fed_event,
    gamma_events,
    history,
    inx_event,
    jobs_event,
    movers_mode,
    rec_event,
    settings,
)

# 全是编造的地址、合约、问题和数字；Polymarket 的真实主机名只被 MockTransport 截住，不会联网
KALSHI_HOST = "api.example-kalshi.test"
CBOE_HOST = "cboe.example.test"
CLEVELAND_HOST = "nowcast.example.test"
VIX_URL = f"https://{CBOE_HOST}/daily/VIX_History.csv"
VIX9D_URL = f"https://{CBOE_HOST}/daily/VIX9D_History.csv"
NOWCAST_URL = f"https://{CLEVELAND_HOST}/webcharts/nowcast_month.json"
POLY_HOSTS = {"gamma-api.polymarket.com", "clob.polymarket.com", "data-api.polymarket.com"}
FAKE_KEY = "fake-ledger-key-for-tests-only-0123456789abcdef"
ENV = {"MARKETS_LEDGER_KEY": FAKE_KEY}
TUESDAY = datetime(2030, 3, 5, 7, 30, tzinfo=BJT)
MONDAY_EVENING = datetime(2030, 3, 4, 19, 30, tzinfo=BJT)
CPI_EVENING = datetime(2030, 3, 6, 19, 30, tzinfo=BJT)
CPI_CLOSED = datetime(2030, 3, 6, 13, 29, tzinfo=timezone.utc)

ZH = {
    COND_HI: "示例议会四月前批准小部件税",
    COND_LO: "示例银行五月前把利率加到 9%",
    COND_MOVE: "示例财政部今年会发行百年期国债吗",
}
# 状态行和日志里绝不能出现的东西：问题、合约代码、condition id、token id、价格、地址
SECRETS = (
    Q_HI, Q_LO, Q_MOVE, Q_SAY, *ZH.values(), CPI, JOBS, FED, INX, REC, "KX",
    COND_HI, COND_LO, COND_MOVE, COND_SAY, "0x", TOKEN_HI, TOKEN_LO, TOKEN_MOVE, TOKEN_SAY,
    "%", "15.87", "example", "http", "示例",
)

LINE_CPI = "· 3月6日 周三 21:30 美国2月CPI 环比：中位数 0.3%（一半把握 0.2% 到 0.3%）；克利夫兰联储模型 0.28%"
LINE_JOBS = "· 3月8日 周五 21:30 美国2月非农新增就业：中位数 +7.5 万（一半把握 +3 万 到 +12 万）"
LINE_FED = "· 3月21日 周四 02:00 美联储议息：维持不变 较可能（71%），降息 25 个基点 23%"
LINE_HI = "· 示例议会四月前批准小部件税：很可能（91%）· 4月3日结算"
LINE_LO = "· 示例银行五月前把利率加到 9%：很不可能（6%）· 4月23日结算；便宜合约历来被高估"
LINE_MOVE = "· 示例财政部今年会发行百年期国债吗：61%（一天 +11 个百分点）"
LINE_VIX = "· VIX 15.87（3月1日收盘，长期平均约 20）：标普500 一天的涨跌约有三分之二的机会在 ±1.0% 以内"
LINE_VIX9D = "· 未来 9 天的预期波动（17.20）高于未来 30 天，市场觉得近期风险更大"


def panel_settings(**changes) -> MarketsSettings:
    s = settings(
        vix=replace(VixSettings(), vix_url=VIX_URL, vix9d_url=VIX9D_URL),
        nowcast=replace(NowcastSettings(), url=NOWCAST_URL),
    )
    return replace(s, **changes)


def show(s: MarketsSettings | None = None) -> MarketsSettings:
    return movers_mode(s or panel_settings(), "show")


# ---- 编造的接口 ----


def level(price: float, size: float) -> dict:
    return {"price": f"{price:.3f}", "size": f"{size:.2f}"}


def book(bid: float, ask: float, size: float = 20000.0) -> dict:
    return {"market": "编造", "bids": [level(bid, size)], "asks": [level(ask, size)]}


def trade_rows(sizes=(10, 10, 10, 10, 10, 10)) -> list[dict]:
    return [
        {"proxy_wallet": "0x" + f"{i + 1:040x}", "side": "BUY", "size": size, "price": 0.6,
         "timestamp": NOW_TS - 600 - i, "outcome_index": 0, "condition_id": COND_MOVE}
        for i, size in enumerate(sizes)
    ]


CLEAN = {"status": "initialized", "was_disputed": False, "extended_review": False, "new_version_q": False}


def nowcast_payload() -> list[dict]:
    labels = ["02/24", "03/02"]
    series = {"CPI Inflation": ["0.27", "0.28"], "Core CPI Inflation": ["0.25", "0.26"]}
    return [{
        "chart": {"caption": "编造的标题", "subcaption": "2030-02"},
        "categories": [{"category": [{"label": label} for label in labels]}],
        "dataset": [{"seriesname": name, "data": [{"value": v} for v in values]} for name, values in series.items()],
    }]


def csv(*rows: tuple[str, float]) -> str:
    return "DATE,OPEN,HIGH,LOW,CLOSE\n" + "".join(f"{day},1,1,1,{close}\n" for day, close in rows)


@dataclass
class Venues:
    """按主机分发的假接口。status 里的主机一律返回那个状态码，explode 里的主机让处理函数直接抛异常。"""

    kalshi: dict = field(default_factory=lambda: {
        "KXCPI": [cpi_event()], "KXPAYROLLS": [jobs_event()], "KXFEDDECISION": [fed_event()],
        "KXINXY": [inx_event()], "KXRECSSNBER": [rec_event()],
    })
    gamma: list = field(default_factory=gamma_events)
    books: dict = field(default_factory=lambda: {
        TOKEN_HI: book(0.90, 0.92, 100.0), TOKEN_LO: book(0.05, 0.07, 100.0), TOKEN_MOVE: book(0.60, 0.62),
    })
    prices: list = field(default_factory=history)
    trades: list = field(default_factory=trade_rows)
    resolution: dict = field(default_factory=lambda: dict(CLEAN))
    vix: str = csv(("02/28/2030", 15.10), ("03/01/2030", 15.87))
    vix9d: str = csv(("02/28/2030", 16.00), ("03/01/2030", 17.20))
    nowcast: list = field(default_factory=nowcast_payload)
    status: dict = field(default_factory=dict)
    explode: set = field(default_factory=set)
    calls: list = field(default_factory=list)
    trades_more: bool = False  # 成交接口总说“还有下一页”（一天的成交多得翻不完）

    def hosts(self) -> set[str]:
        return {host for host, _ in self.calls}

    def series(self) -> list[str]:
        return [params["series_ticker"] for host, params in self.calls if host == KALSHI_HOST]

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path, params = request.url.host, request.url.path, request.url.params
        self.calls.append((host, dict(params)))
        if host in self.explode or "*" in self.explode:
            raise RuntimeError("编造的故障")
        if host in self.status:
            return httpx.Response(self.status[host], json={})
        if host == KALSHI_HOST and path == "/trade-api/v2/events":
            return httpx.Response(200, json={"events": self.kalshi.get(params["series_ticker"], []), "cursor": ""})
        if host == "gamma-api.polymarket.com" and path == "/events/keyset":
            return httpx.Response(200, json={"events": self.gamma})
        if host == "clob.polymarket.com" and path == "/book":
            found = self.books.get(params["token_id"])
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={})
        if host == "data-api.polymarket.com":
            done = {"has_more": False}
            if path == "/v2/prices-history":
                rows = [{"timestamp": ts, "price": p} for ts, p in self.prices]
                return httpx.Response(200, json={"data": rows, "pagination": done})
            if path == "/v2/trades":
                more = {"has_more": True, "next_cursor": "c-" + str(len(self.calls))} if self.trades_more else done
                return httpx.Response(200, json={"data": self.trades, "pagination": more})
            if path == "/v2/resolutions":
                rows = [{"condition_id": params["condition"], **self.resolution}]
                return httpx.Response(200, json={"data": rows, "pagination": done})
        if host == CBOE_HOST:
            return httpx.Response(200, text=self.vix9d if "VIX9D" in path else self.vix)
        if host == CLEVELAND_HOST:
            return httpx.Response(200, json=self.nowcast)
        return httpx.Response(404, json={})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeLLM:
    """假的大模型：问题给中文译文，价格变化的那个配今天第 news 条新闻。"""

    def __init__(self, news: int | None = 2, error: Exception | None = None) -> None:
        self.news = news
        self.error = error
        self.calls: list[tuple[list, list]] = []

    def annotate(self, markets, news, words=()):
        self.calls.append((list(markets), list(news)))
        if self.error is not None:
            raise self.error
        return {cid: (ZH.get(cid), self.news if cid == COND_MOVE else None) for cid, _ in markets}


class Channel:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list = []

    def send(self, message) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(message)


def news_item(n: int) -> Item:
    return Item(source="example", source_name="编造的来源", section="tech", id=f"n{n}", title=f"Made-up headline {n}",
                url=f"https://news.invalid/{n}", published_at=NOW, title_zh=f"编造的新闻 {n}")


NEWS = [news_item(1), news_item(2)]


def make_ctx(venues: Venues, *, now=NOW, edition="morning", mode="send", s=None, news=None, llm="fake",
             rows=(), summary=None, requests=80) -> panel.Context:
    fetcher = Fetcher(venues.client(), Budget(60.0, requests), sleep=lambda s: None, jitter=lambda: 0.0)
    return panel.Context(
        fetcher=fetcher,
        settings=s or panel_settings(),
        now=now,
        edition=edition,
        mode=mode,
        news=list(NEWS if news is None else news),
        llm=FakeLLM() if llm == "fake" else llm,
        rows=list(rows),
        backtest=summary,
    )


def texts(lines, section=None) -> list[str]:
    return [line.text for line in lines if section is None or line.section == section]


def candidates(result) -> list[dict]:
    return [row for row in result.rows if row.get("type") == "candidate"]


def assert_private(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, secret


# ---- build ----


def test_build_full_monday_morning():
    venues = Venues()
    ctx = make_ctx(venues)
    result = panel.build(ctx)
    assert texts(result.lines, "key") == [LINE_CPI, LINE_JOBS, LINE_FED]
    # 议息在 17 天后，超过回测记录的 7 天
    assert [line.sub for line in result.lines if line.section == "key"] == ["回测还没跑", "回测还没跑", "回测只算到提前 7 天"]
    weekly = texts(result.lines, "weekly")
    assert weekly == ["· 标普500 今年年底：中位数落在 5200–5400 点（一半把握 5000 到 5600 点）",
                      "· 2030 到 2031 年美国实际 GDP 连续两季负增长：很不可能（19%）"]
    assert texts(result.lines, "lopsided") == [LINE_HI, LINE_LO]
    assert texts(result.lines, "mover") == []  # 默认后台观察，不显示
    assert texts(result.lines, "vix") == [LINE_VIX, LINE_VIX9D]
    assert result.message is not None
    assert all(line.text in result.message.text for line in result.lines)
    assert "克利夫兰联储" in result.message.text  # 页脚注明预测来源
    assert result.status == "市场板块：9 行；Kalshi 正常，Polymarket 正常，Cboe 正常，克利夫兰联储 正常"
    assert result.requests == ctx.fetcher.requests == len(venues.calls)
    # 每个展示的 Kalshi 和 Polymarket 行都带一条记录，价格变化的候选另记
    shown = [row for row in result.rows if row["type"] == "shown"]
    assert {row["key"] for row in shown} >= {CPI, JOBS, INX, f"{COND_HI}:{TOKEN_HI}", f"{COND_LO}:{TOKEN_LO}"}
    (candidate,) = candidates(result)
    assert candidate["key"] == f"{COND_MOVE}:{TOKEN_MOVE}" and candidate["shown"] is False
    assert all(candidate["gates"].values())
    # 克利夫兰联储只取一次；大模型只调一次，翻译和配新闻一起
    assert sum(host == CLEVELAND_HOST for host, _ in venues.calls) == 1
    (call,) = ctx.llm.calls
    assert {cid for cid, _ in call[0]} == {COND_HI, COND_LO, COND_MOVE}
    assert call[1] == ["编造的新闻 1", "编造的新闻 2"]
    # 排除的“会不会提到”市场一次都没查
    assert all(params.get("token_id") != TOKEN_SAY and params.get("condition") != COND_SAY
               for _, params in venues.calls)
    assert_private(result.status)


def test_guard_drops_expired_lines():
    now = datetime(2030, 3, 6, 13, 29, tzinfo=timezone.utc)
    lines = [
        Line("key", "过期了", expires=now - timedelta(minutes=1)),
        Line("key", "正好到点", expires=now),
        Line("key", "还没到", expires=now + timedelta(minutes=1)),
        Line("vix", "不会过期"),
        Line("key", "不带时区的按 UTC", expires=datetime(2030, 3, 6, 13, 30)),
    ]
    assert [line.text for line in panel.guard(lines, now)] == ["还没到", "不会过期", "不带时区的按 UTC"]
    assert panel.guard(lines, datetime(2030, 3, 6, 13, 29)) == panel.guard(lines, now)


def test_weekly_and_score_only_on_monday_morning(monkeypatch):
    calls = []

    def weekly_lines(rows, now):
        calls.append(now)
        return [Line("score", "· 上周 1 件出了结果：编造", rows=({"type": "score"},))]

    monkeypatch.setattr(score, "weekly_lines", weekly_lines)
    monday = panel.build(make_ctx(Venues()))
    assert texts(monday.lines, "score") == ["· 上周 1 件出了结果：编造"]
    assert {"type": "score"} in monday.rows
    for now, edition in ((TUESDAY, "morning"), (MONDAY_EVENING, "evening")):
        venues = Venues()
        result = panel.build(make_ctx(venues, now=now, edition=edition))
        assert texts(result.lines, "weekly") == [] and texts(result.lines, "score") == []
        assert "KXINXY" not in venues.series() and "KXRECSSNBER" not in venues.series()
    assert calls == [NOW]


def test_evening_without_data_tonight_skips_everything_else():
    venues = Venues()
    result = panel.build(make_ctx(venues, now=MONDAY_EVENING, edition="evening"))
    assert result.lines == [] and result.message is None
    assert venues.hosts() == {KALSHI_HOST}  # 消息反正不发，Polymarket 和 Cboe 都没请求
    assert result.status == "市场板块：0 行；Kalshi 正常"


def test_evening_with_cpi_tonight():
    venues = Venues()
    result = panel.build(make_ctx(venues, now=CPI_EVENING, edition="evening"))
    (key,) = [line for line in result.lines if line.section == "key"]
    assert key.text.startswith("· 3月6日 周三 21:30 美国2月CPI 环比") and key.rows[0]["lead"] == 0
    assert texts(result.lines, "lopsided") and texts(result.lines, "vix")
    assert result.message is not None


def test_evening_preview_still_shows_movers():
    venues = Venues()
    result = panel.build(make_ctx(venues, now=MONDAY_EVENING, edition="evening", mode="preview"))
    (mover,) = [line for line in result.lines if line.section == "mover"]
    assert mover.sub == "各项核实都通过了；相关新闻：今晚第 2 条"


def test_audience_group_only_polymarket():
    venues = Venues()
    result = panel.build(make_ctx(venues, s=panel_settings(audience="group")))
    assert venues.hosts() <= POLY_HOSTS
    assert {line.section for line in result.lines} == {"lopsided"}
    assert result.status == "市场板块：2 行；Polymarket 正常"


def test_kalshi_failure_does_not_stop_other_sections():
    venues = Venues(status={KALSHI_HOST: 500})
    result = panel.build(make_ctx(venues))
    assert {line.section for line in result.lines} == {"lopsided", "vix"}
    assert result.status == ("市场板块：4 行；Kalshi HTTP 500，Polymarket 正常，Cboe 正常；"
                             "出错：关键数据 FetchError，每周一看 FetchError")


def test_vix_comes_before_the_polymarket_checks():
    # 波动率只要两个请求，排在 Polymarket（十几个核实请求）前面：预算只够关键数据和波动率时，波动率照样有
    venues = Venues()
    result = panel.build(make_ctx(venues, requests=10))  # 5 类关键数据 + 克利夫兰联储 + 2 个每周一看 + 2 个 Cboe
    hosts = [host for host, _ in venues.calls]
    assert texts(result.lines, "vix") == [LINE_VIX, LINE_VIX9D]
    assert "gamma-api.polymarket.com" not in hosts and len(hosts) == 10
    assert result.status.endswith("出错：Polymarket 列表 BudgetExceeded")
    # 消息里的顺序仍按部分排：波动率在定价一边倒后面
    full = panel.build(make_ctx(Venues()))
    assert full.message.text.index("定价一边倒") < full.message.text.index("波动率")


def test_mover_trades_must_cover_the_whole_day():
    # 成交多得翻满两页还没到 24 小时前：只有最近几个小时的，钱包数和集中度两项算没通过
    venues = Venues(trades_more=True)
    result = panel.build(make_ctx(venues, s=show()))
    (candidate,) = candidates(result)
    assert [name for name, ok in candidate["gates"].items() if not ok] == ["wallets", "top_share"]
    assert candidate["values"]["trades_complete"] is False and texts(result.lines, "mover") == []
    trades = [params for host, params in venues.calls if params.get("taker_only") == "true"]
    assert len(trades) == panel.TRADES_PAGES == 2 and trades[1]["cursor"].startswith("c-")


def test_raising_sources_are_isolated():
    venues = Venues(explode={"gamma-api.polymarket.com"}, status={CLEVELAND_HOST: 404})
    result = panel.build(make_ctx(venues))
    assert texts(result.lines, "key")[0] == LINE_CPI.split("；克利夫兰联储模型")[0]  # 没有预测就不放这一句
    assert texts(result.lines, "lopsided") == [] and texts(result.lines, "vix") == [LINE_VIX, LINE_VIX9D]
    assert result.status == ("市场板块：7 行；Kalshi 正常，Cboe 正常，克利夫兰联储 HTTP 404；"
                             "出错：克利夫兰联储预测 FetchError，Polymarket 列表 RuntimeError")


def test_llm_failure_falls_back_to_english():
    ctx = make_ctx(Venues(), llm=FakeLLM(error=RuntimeError("编造")), s=show())
    result = panel.build(ctx)
    assert texts(result.lines, "lopsided")[0] == f"· {Q_HI}：很可能（91%）· 4月3日结算"
    assert texts(result.lines, "mover") == []  # 配不上新闻，不显示
    assert result.status.endswith("出错：翻译 RuntimeError")
    assert candidates(result)[0]["gates"]["news"] is False


def test_vix_skipped_when_stale_and_vix9d_not_requested():
    venues = Venues(vix=csv(("02/20/2030", 15.0)))
    result = panel.build(make_ctx(venues))
    assert texts(result.lines, "vix") == []
    assert [params for host, params in venues.calls if host == CBOE_HOST] == [{}]


def test_replay_clause_from_backtest_summary(monkeypatch):
    seen = []

    def clause(summary, series, label):
        seen.append((series, label))
        return "过去 9 次同一时刻（提前 2 天早上）：中位数平均差 0.05 个百分点" if series == "KXCPI" else None

    monkeypatch.setattr(backtest, "record_clause", clause)
    result = panel.build(make_ctx(Venues(), summary={"version": 1}))
    subs = {line.rows[0]["series"]: line.sub for line in result.lines if line.section == "key"}
    assert subs["KXCPI"] == "过去 9 次同一时刻（提前 2 天早上）：中位数平均差 0.05 个百分点"
    assert subs["KXPAYROLLS"] == "回测里这个时刻的记录不够"  # 提前 4 天，回测里没有这一项
    assert subs["KXFEDDECISION"] == "回测只算到提前 7 天"  # 17 天后的议息，超过 7 天
    assert ("KXCPI", "M2") in seen and ("KXPAYROLLS", "M4") in seen
    assert all(series != "KXFEDDECISION" for series, _ in seen)


# ---- 价格变化：后台观察、显示、预览，以及每一道门槛 ----


def test_mover_shadow_show_and_preview():
    shadow = panel.build(make_ctx(Venues()))
    assert texts(shadow.lines, "mover") == [] and candidates(shadow)[0]["shown"] is False

    shown = panel.build(make_ctx(Venues(), s=show()))
    (line,) = [line for line in shown.lines if line.section == "mover"]
    assert line.text == LINE_MOVE and line.sub == "相关新闻：今天第 2 条"
    assert candidates(shown)[0]["shown"] is True and candidates(shown)[0]["movers_mode"] == "show"

    preview = panel.build(make_ctx(Venues(), mode="preview"))
    (line,) = [line for line in preview.lines if line.section == "mover"]
    assert line.text == "· 【后台观察】" + LINE_MOVE[2:]
    assert line.sub == "各项核实都通过了；相关新闻：今天第 2 条"
    assert "预览" in preview.message.title

    preview_show = panel.build(make_ctx(Venues(), mode="preview", s=show()))
    assert texts(preview_show.lines, "mover") == [LINE_MOVE]


MOVE_FAILURES = {
    "change": dict(books={TOKEN_MOVE: book(0.605, 0.615)}, prices=history(p24=0.58, recent=(0.60, 0.60))),
    "spread": dict(books={TOKEN_MOVE: book(0.58, 0.64)}),
    "persist": dict(prices=history(recent=(0.58, 0.49))),
    "depth": dict(books={TOKEN_MOVE: book(0.60, 0.62, 100.0)}),
    "wallets": dict(trades=trade_rows((10, 10, 10))),
    "top_share": dict(trades=trade_rows((100, 10, 10, 10, 10, 10))),
    "clean": dict(resolution={**CLEAN, "status": "proposed"}),
    "news": {},
}


@pytest.mark.parametrize("gate", sorted(MOVE_FAILURES))
def test_each_mover_gate_from_the_network(gate):
    venues = Venues()
    for name, value in MOVE_FAILURES[gate].items():
        current = getattr(venues, name)
        setattr(venues, name, {**current, **value} if isinstance(current, dict) and name == "books" else value)
    llm = FakeLLM(news=None if gate == "news" else 2)
    result = panel.build(make_ctx(venues, s=show(), llm=llm))
    (candidate,) = candidates(result)
    assert [name for name, ok in candidate["gates"].items() if not ok] == [gate]
    assert candidate["shown"] is False and texts(result.lines, "mover") == []
    preview = panel.build(make_ctx(venues, mode="preview", llm=llm))
    (line,) = [line for line in preview.lines if line.section == "mover"]
    assert line.sub.startswith("没通过：" + GATE_NAMES[gate])


def test_mover_without_book_records_the_failed_step():
    venues = Venues()
    venues.books = {k: v for k, v in venues.books.items() if k != TOKEN_MOVE}
    result = panel.build(make_ctx(venues, s=show()))
    (candidate,) = candidates(result)
    assert candidate["errors"] == ["book:FetchError"]
    assert candidate["gates"]["change"] is False and texts(result.lines, "mover") == []
    # 盘口没取到就不再查历史、成交和结算
    assert not any(host == "data-api.polymarket.com" and {COND_MOVE, TOKEN_MOVE} & set(params.values())
                   for host, params in venues.calls)


# ---- run_panel：推送、记账、预览、试运行、从不抛异常 ----


@pytest.fixture
def wired(monkeypatch, config):
    """把 run 的外部依赖换成测试用的：规则、等待、大模型。返回一个可以改的设置。"""
    state = {"settings": panel_settings(), "llm": FakeLLM()}
    monkeypatch.setattr(panel, "load_settings", lambda: state["settings"])
    monkeypatch.setattr(panel, "_sleep", lambda seconds: None)
    monkeypatch.setattr(panel, "_llm", lambda client, cfg, env: state["llm"])
    state["config"] = replace(config, edition="morning")
    return state


def run(wired, tmp_path, venues=None, channel=None, mode="send", now_fn=None, env=ENV, news=None):
    venues = venues or Venues()
    return markets.run_panel(
        venues.client(), wired["config"], now_fn or (lambda: NOW), tmp_path / ".state",
        NEWS if news is None else news, channel, mode=mode, env=env,
    )


def ledger_rows(tmp_path) -> list[dict]:
    return Ledger.open(tmp_path / ".state" / "markets", keys_from_env(ENV)).rows()


def test_run_sends_then_records(wired, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    channel = Channel()
    status = run(wired, tmp_path, channel=channel)
    assert status == "市场板块：已推送 9 行（内容不公开）；Kalshi 正常，Polymarket 正常，Cboe 正常，克利夫兰联储 正常"
    (message,) = channel.sent
    assert LINE_CPI in message.text and LINE_HI in message.text
    # 第一次推送时账本还是空的
    assert message.text.endswith("｜账本此前为空</font>")
    rows = ledger_rows(tmp_path)
    types = [row["type"] for row in rows]
    assert types.count("shown") == 7 and types.count("candidate") == 1
    assert set(types) <= {"rules", "shown", "candidate"}
    assert all(row["mode"] == "send" and row["edition"] == "morning" and row["ts"] for row in rows)
    assert {row["key"] for row in rows if row["type"] == "shown"} >= {CPI, JOBS, f"{COND_LO}:{TOKEN_LO}"}
    # 公开的文件里没有明文
    assert ledger.assert_no_plaintext(tmp_path / ".state", [CPI, COND_HI, TOKEN_HI]) == []
    for path in (tmp_path / ".state").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert CPI.encode() not in data and COND_HI.encode() not in data and Q_HI.encode() not in data
    assert_private(status)
    assert_private(caplog.text)

    # 第二次：页脚带上发送前的账本链头；规则指纹没变就不再记
    head16 = Ledger.open(tmp_path / ".state" / "markets", keys_from_env(ENV)).head16()
    channel = Channel()
    run(wired, tmp_path, channel=channel)
    assert f"账本 #{head16}" in channel.sent[0].text
    assert [row["type"] for row in ledger_rows(tmp_path)].count("rules") == types.count("rules") == 1


def test_run_records_shown_mover_only_in_show_mode(wired, tmp_path):
    wired["settings"] = show()
    status = run(wired, tmp_path, channel=Channel())
    assert status.startswith("市场板块：已推送 10 行")
    rows = ledger_rows(tmp_path)
    (candidate,) = [row for row in rows if row["type"] == "candidate"]
    assert candidate["shown"] is True
    assert any(row["type"] == "shown" and row.get("section") == "mover" for row in rows)


def test_run_failed_send_records_nothing_shown(wired, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    status = run(wired, tmp_path, channel=Channel(RuntimeError("编造的发送故障")))
    assert status.startswith("市场板块：未推送：RuntimeError；Kalshi 正常")
    assert ledger_rows(tmp_path) == []
    assert not (tmp_path / ".state" / "markets").exists()
    assert_private(caplog.text)


def test_run_failed_send_still_records_checks(wired, tmp_path, monkeypatch):
    witness = {"type": "witness", "series": "KXCPI", "period": "2030-01", "value": 0.2, "source": "bls"}
    monkeypatch.setattr(score, "collect_witnesses", lambda fetcher, s, rows, now: [witness])
    run(wired, tmp_path, channel=Channel(RuntimeError("编造")))
    (row,) = ledger_rows(tmp_path)
    assert row["type"] == "witness" and row["value"] == 0.2 and row["mode"] == "send"


def test_run_score_failures_are_isolated(wired, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("编造")

    monkeypatch.setattr(score, "collect_witnesses", boom)
    monkeypatch.setattr(score, "collect_settlements", boom)
    status = run(wired, tmp_path, channel=Channel())
    assert status.startswith("市场板块：已推送 9 行")
    assert "出错：官方数据核对 RuntimeError，结算核对 RuntimeError" in status


def test_run_without_key_sends_but_does_not_record(wired, tmp_path):
    channel = Channel()
    status = run(wired, tmp_path, channel=channel, env={})
    assert status.startswith("市场板块：已推送 9 行") and "账本未记录（没有账本钥匙）" in status
    assert channel.sent and not (tmp_path / ".state").exists()
    assert "账本" in channel.sent[0].text  # 页脚说明没有账本


def test_run_drops_lines_that_expire_before_sending(wired, tmp_path):
    times = iter([NOW])
    channel = Channel()
    status = run(wired, tmp_path, channel=channel, now_fn=lambda: next(times, CPI_CLOSED))
    assert status.startswith("市场板块：已推送 8 行")
    assert "CPI 环比" not in channel.sent[0].text and LINE_JOBS in channel.sent[0].text
    assert CPI not in {row.get("key") for row in ledger_rows(tmp_path)}


def test_preview_sends_but_never_records(wired, tmp_path):
    channel = Channel()
    status = markets.run_preview(Venues().client(), wired["config"], lambda: NOW, tmp_path / ".state", channel, env=ENV)
    assert status == "市场板块预览：已推送 10 行（内容不公开）；Kalshi 正常，Polymarket 正常，Cboe 正常，克利夫兰联储 正常"
    (message,) = channel.sent
    assert "预览" in message.title and "【后台观察】" in message.text
    assert message.text.endswith("｜账本未记录</font>")  # 预览不记账，不写“账本此前为空”
    assert not (tmp_path / ".state").exists()


def test_dry_run_neither_sends_nor_records(wired, tmp_path):
    venues = Venues()
    status = run(wired, tmp_path, venues=venues, mode="dry")
    assert status == "市场板块：9 行（公开仓库，内容已隐藏）；Kalshi 正常，Polymarket 正常，Cboe 正常，克利夫兰联储 正常"
    assert not (tmp_path / ".state").exists()
    assert_private(status)


def test_unknown_mode_is_dry(wired, tmp_path):
    channel = Channel()
    status = run(wired, tmp_path, channel=channel, mode="bogus")
    assert "内容已隐藏" in status and channel.sent == []


def test_broken_rules_disable_the_panel(wired, tmp_path):
    wired["settings"] = MarketsSettings(error="编造的错误")
    venues = Venues()
    assert run(wired, tmp_path, venues=venues, channel=Channel()) == DISABLED_STATUS
    assert venues.calls == []


def test_rules_warnings_are_counted(wired, tmp_path):
    wired["settings"] = replace(panel_settings(), warnings=("一处", "两处"))
    status = run(wired, tmp_path, mode="dry")
    assert status.endswith("；规则文件有 2 处写错，已用默认值")


@pytest.mark.parametrize("mode", ["send", "preview", "dry"])
def test_run_never_raises_when_every_request_explodes(wired, tmp_path, mode):
    venues = Venues(explode={"*"})
    status = run(wired, tmp_path, venues=venues, channel=Channel(), mode=mode)
    assert isinstance(status, str) and "RuntimeError" in status
    assert "没有联网" in status
    assert_private(status)


KALSHI_SPARE = "spare.example-kalshi.test"


def blackholed_kalshi(wired, monkeypatch):
    """两个 Kalshi 地址都连不上：每次尝试让假时钟走 10 秒（连接超时）；Polymarket、Cboe、克利夫兰联储正常。"""
    s = panel_settings()
    hosts = (f"https://{KALSHI_HOST}/trade-api/v2", f"https://{KALSHI_SPARE}/trade-api/v2")
    wired["settings"] = replace(s, kalshi=replace(s.kalshi, hosts=hosts))
    clock = [0.0]
    monkeypatch.setattr(panel, "_clock", lambda: clock[0])
    monkeypatch.setattr(panel, "_sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    venues = Venues()
    kalshi_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in (KALSHI_HOST, KALSHI_SPARE):
            kalshi_calls.append(request.url.host)
            clock[0] += 10.0
            raise httpx.ConnectTimeout("编造的超时", request=request)
        return venues.handler(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), venues, kalshi_calls, clock


def test_blackholed_kalshi_leaves_the_budget_to_the_others(wired, tmp_path, monkeypatch):
    client, venues, kalshi_calls, clock = blackholed_kalshi(wired, monkeypatch)
    status = markets.run_panel(client, wired["config"], lambda: NOW, tmp_path / ".state", NEWS, None, mode="dry",
                               env=ENV)
    # 两个地址各试两次就认定连不上，后面的系列不再等；Polymarket 和 Cboe 照常
    assert len(kalshi_calls) == 4 and clock[0] < 60
    assert status == ("市场板块：4 行（公开仓库，内容已隐藏）；Kalshi ConnectTimeout，Polymarket 正常，Cboe 正常；"
                      "出错：关键数据 FetchError，每周一看 FetchError")
    assert {"gamma-api.polymarket.com", "clob.polymarket.com", "data-api.polymarket.com", CBOE_HOST} <= venues.hosts()


def test_kalshi_unreachable_during_checks_is_not_retried_by_the_panel(wired, tmp_path, monkeypatch):
    client, venues, kalshi_calls, _ = blackholed_kalshi(wired, monkeypatch)
    monkeypatch.setattr(score, "collect_settlements",
                        lambda kalshi, poly, rows, now, limit: [kalshi.market("KXMADEUP-30JAN-T1")])
    channel = Channel()
    status = markets.run_panel(client, wired["config"], lambda: NOW, tmp_path / ".state", NEWS, channel, env=ENV)
    # 结算核对时已经连不上（两个地址各两次），板块不再请求 Kalshi
    assert len(kalshi_calls) == 4
    assert status.startswith("市场板块：已推送 4 行（内容不公开）；Kalshi ConnectTimeout，Polymarket 正常，Cboe 正常；")
    assert "结算核对 FetchError" in status and "关键数据 FetchError" in status and "BudgetExceeded" not in status
    assert_private(status)


def test_markets_client_has_no_hidden_connect_retries(monkeypatch):
    # 共享客户端的连接层会把连不上的请求自己再试两次；市场数据换成不重试的客户端，重试只由 Fetcher 按预算决定
    attempts = []

    def refuse(self, *args, **kwargs):
        attempts.append(kwargs.get("timeout"))
        raise httpcore.ConnectTimeout("编造的超时")

    monkeypatch.setattr(httpcore._backends.sync.SyncBackend, "connect_tcp", refuse)
    shared = make_client()
    own = panel._markets_client(shared)
    assert own is not None and own.headers["User-Agent"] == USER_AGENT and own.follow_redirects
    fetcher = Fetcher(own, Budget(150.0, 80), timeout=10.0, sleep=lambda s: None, jitter=lambda: 0.0)
    with pytest.raises(FetchError):
        fetcher.get_json("https://api.example-market.test/v1/things")
    assert fetcher.requests == 2 and attempts == [5.0, 5.0]  # 每次尝试只连一次，最多等 5 秒
    own.close()
    shared.close()
    # 测试里的假接口（不是真正的网络连接）原样使用
    assert panel._markets_client(Venues().client()) is None


def test_failed_state_checkout_records_nothing(wired, tmp_path):
    # 工作流没取到 state 分支（.state 是空的）：新建账本会在保存时盖掉分支上的钥匙记录，所以这次不记账
    channel = Channel()
    status = run(wired, tmp_path, channel=channel, env={**ENV, "MARKETS_STATE_OK": "false"})
    assert status.startswith("市场板块：已推送 9 行") and "账本未记录（去重状态没取到，这次不记账）" in status
    assert channel.sent and not (tmp_path / ".state").exists()
    # 取到了（或者本地运行没有这个变量）照常记账
    assert "账本" not in run(wired, tmp_path, channel=Channel(), env={**ENV, "MARKETS_STATE_OK": "true"})
    assert ledger_rows(tmp_path)


@pytest.mark.parametrize("requests", [0, 1, 3])
def test_run_never_raises_when_budget_runs_out(wired, tmp_path, requests):
    wired["settings"] = replace(panel_settings(), budget=replace(BudgetSettings(), requests=requests))
    status = run(wired, tmp_path, channel=Channel())
    assert isinstance(status, str) and "BudgetExceeded" in status
    assert_private(status)


def test_run_panel_catches_everything(wired, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("编造的内部错误 " + CPI)

    monkeypatch.setattr(panel, "run", boom)
    assert run(wired, tmp_path, channel=Channel()) == "市场板块：未推送：ValueError"
    assert run(wired, tmp_path, channel=Channel(), mode="preview") == "市场板块预览：未推送：ValueError"
    assert run(wired, tmp_path, mode="dry") == "市场板块：出错：ValueError"
    monkeypatch.setattr(panel, "run_replay", boom)
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, tmp_path / ".state", None, env=ENV)
    assert status == "市场回测：出错：ValueError"


def test_run_panel_reports_import_failure(wired, tmp_path, monkeypatch):
    monkeypatch.delattr(markets, "panel")
    monkeypatch.setitem(sys.modules, "digest.markets.panel", None)
    assert run(wired, tmp_path, channel=Channel()) == "市场板块：未启用（加载失败：ModuleNotFoundError）"


def test_enabled_flag():
    assert markets.enabled({"MARKETS_ENABLED": " True "}) and markets.enabled({"MARKETS_ENABLED": "1"})
    assert not markets.enabled({}) and not markets.enabled({"MARKETS_ENABLED": "no"})


# ---- 回测 ----


SUMMARY = {"version": 1, "generated": "2030-03-04T00:00:00Z", "dry": False, "partial": False, "requests": 0,
           "series_count": 5, "events": 10, "scored": 9, "skipped": 1, "errors": 0, "series": {}, "bands": {}}


def fake_backtest(monkeypatch, summary=SUMMARY):
    calls = []

    def run_backtest(fetcher, settings, now, dry):
        calls.append(dry)
        return dict(summary, dry=dry)

    monkeypatch.setattr(backtest, "run", run_backtest)
    return calls


def test_backtest_dry_run_only_counts(wired, tmp_path, monkeypatch):
    calls = fake_backtest(monkeypatch)
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, tmp_path / ".state", None, env=ENV)
    assert status == "市场回测（试运行）：5 类合约 10 次事件，算出 9 次；请求 0 次；没有联网"
    assert calls == [True]
    assert not (tmp_path / ".state").exists()


def test_backtest_sends_and_stores_encrypted(wired, tmp_path, monkeypatch):
    calls = fake_backtest(monkeypatch, dict(SUMMARY, partial=True))
    channel = Channel()
    state = tmp_path / ".state"
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, state, channel, env=ENV)
    assert status == ("市场回测：已推送（内容不公开）；5 类合约 10 次事件，算出 9 次；请求 0 次；部分完成；"
                      "结果已加密保存；没有联网")
    assert calls == [False] and len(channel.sent) == 1
    markets_dir = tmp_path / ".state" / "markets"
    (row,) = Ledger.open(markets_dir, keys_from_env(ENV)).rows()
    assert row["type"] == "backtest" and row["events"] == 10 and row["partial"] is True and row["mode"] == "backtest"
    assert ledger.read_blob(markets_dir, "backtest", keys_from_env(ENV))["series_count"] == 5
    # 下一次板块读到回测结果：没有这类合约的成绩时说记录不足
    status = run(wired, tmp_path, mode="dry")
    assert status.startswith("市场板块：9 行")


def test_backtest_failed_send_still_stores(wired, tmp_path, monkeypatch):
    fake_backtest(monkeypatch)
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, tmp_path / ".state",
                                  Channel(RuntimeError("编造")), env=ENV)
    assert status.startswith("市场回测：未推送：RuntimeError；") and "结果已加密保存" in status


def test_backtest_that_scored_nothing_is_neither_sent_nor_stored(wired, tmp_path, monkeypatch):
    state = tmp_path / ".state"
    fake_backtest(monkeypatch)
    markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, state, Channel(), env=ENV)
    # 第二次每个请求都失败，一次都没算出来：不推送空消息，也不盖掉上次的结果
    fake_backtest(monkeypatch, dict(SUMMARY, series_count=0, events=0, scored=0, skipped=0, errors=5))
    channel = Channel()
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, state, channel, env=ENV)
    assert status.startswith("市场回测：没有算出任何事件，没有推送，结果没有保存；0 类合约 0 次事件，算出 0 次")
    assert channel.sent == []
    markets_dir = state / "markets"
    assert ledger.read_blob(markets_dir, "backtest", keys_from_env(ENV))["scored"] == 9
    assert [row["type"] for row in Ledger.open(markets_dir, keys_from_env(ENV)).rows()] == ["backtest"]


def test_incomplete_backtest_does_not_replace_a_better_one(wired, tmp_path, monkeypatch):
    state = tmp_path / ".state"
    markets_dir = state / "markets"
    fake_backtest(monkeypatch)
    markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, state, Channel(), env=ENV)
    # 部分完成、算出的比上次少：推送这次的，但留着上次保存的
    fake_backtest(monkeypatch, dict(SUMMARY, partial=True, events=4, scored=3, skipped=1))
    channel = Channel()
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, state, channel, env=ENV)
    assert len(channel.sent) == 1 and "结果没有保存（没跑完整，比上次保存的少，留着上次的）" in status
    assert ledger.read_blob(markets_dir, "backtest", keys_from_env(ENV))["scored"] == 9
    # 有请求出错但算出的不比上次少：照常保存
    fake_backtest(monkeypatch, dict(SUMMARY, errors=2, scored=9))
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, state, Channel(), env=ENV)
    assert "结果已加密保存" in status
    assert [row["type"] for row in Ledger.open(markets_dir, keys_from_env(ENV)).rows()] == ["backtest", "backtest"]


def test_backtest_after_failed_state_checkout_is_not_stored(wired, tmp_path, monkeypatch):
    fake_backtest(monkeypatch)
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, tmp_path / ".state", Channel(),
                                  env={**ENV, "MARKETS_STATE_OK": "false"})
    assert "结果没有保存（去重状态没取到，这次不记账）" in status and not (tmp_path / ".state").exists()


def test_backtest_does_not_run_for_a_group(wired, tmp_path, monkeypatch):
    # 群里有别人：成绩单全是 Kalshi 的数字，不推送、不请求、不保存；CI 的试运行不推送，照常跑
    calls = fake_backtest(monkeypatch)
    wired["settings"] = panel_settings(audience="group")
    channel = Channel()
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, tmp_path / ".state", channel,
                                  env=ENV)
    assert status == "市场回测：没有运行（audience 是 group，回测只有 Kalshi 的数据）"
    assert calls == [] and channel.sent == [] and not (tmp_path / ".state").exists()
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, tmp_path / ".state", None, env=ENV)
    assert status.startswith("市场回测（试运行）：5 类合约") and calls == [True]


def test_backtest_without_key_is_not_stored(wired, tmp_path, monkeypatch):
    fake_backtest(monkeypatch)
    status = markets.run_backtest(Venues().client(), wired["config"], lambda: NOW, tmp_path / ".state", Channel(),
                                  env={})
    assert "结果没有保存（" in status and not (tmp_path / ".state").exists()
