from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from digest.markets import backtest
from digest.markets.backtest import covers, lead_label, record_clause, run
from digest.markets.ladder import Bound
from digest.markets.net import Budget, Fetcher
from digest.markets.render import render_backtest
from digest.markets.schedule import BJT
from digest.markets.settings import MarketsSettings

# 全是编造的月份（2029 年）、行权价、价格和地址，字段名和格式照接口文档
HOST = "kalshi-replay.example-market.test"
BASE = f"https://{HOST}/trade-api/v2"
PREFIX = "/trade-api/v2"
NOW = datetime(2029, 6, 1, 0, 0, tzinfo=timezone.utc)

# CPI 事件一（实时库，嵌套合约）：收盘 2029-04-11 12:29Z，公布 08:30 EDT = 12:30Z = 北京 20:30
CPI1_CLOSE = datetime(2029, 4, 11, 12, 29, tzinfo=timezone.utc)
# CPI 事件二（已归档）：提前收盘 10:45Z，收盘后、晚上 19:30（11:30Z）之前还有一根报价离谱的 K 线
CPI2_CLOSE = datetime(2029, 3, 13, 10, 45, tzinfo=timezone.utc)
# CPI 事件三：结算说不清（全部 yes，没有结算值）
CPI3_CLOSE = datetime(2029, 2, 13, 13, 29, tzinfo=timezone.utc)
# 议息：收盘 2029-03-21 17:59Z，声明 14:00 EDT = 18:00Z = 北京 3 月 22 日 02:00
FED_CLOSE = datetime(2029, 3, 21, 17, 59, tzinfo=timezone.utc)
# 非农：收盘 2029-04-06 12:29Z
PAY_CLOSE = datetime(2029, 4, 6, 12, 29, tzinfo=timezone.utc)


class FakeClock:
    """假的单调时钟：sleep 不真睡，只把时间往前拨。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def ts(year, month, day, hour, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def market(ticker: str, close: datetime, result: str, **fields) -> dict:
    d = {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "status": "finalized",
        "close_time": iso(close),
        "result": result,
        "expiration_value": "",
        # 结算后的盘口冻结成买 0 卖 1，不能当价格用
        "yes_bid_dollars": "0.0000",
        "yes_ask_dollars": "1.0000",
        "last_price_dollars": "0.5000",
        "open_interest_fp": "0.00",
        "settlement_value_dollars": "1.0000" if result == "yes" else "0.0000",
    }
    d.update(fields)
    return d


def ladder_market(event: str, k: float, close: datetime, result: str, expiration: str = "") -> dict:
    return market(f"{event}-T{k}", close, result, strike_type="greater", floor_strike=k, expiration_value=expiration)


def fed_market(suffix: str, custom: dict, result: str) -> dict:
    return market(f"KXFEDDECISION-29MAR-{suffix}", FED_CLOSE, result, strike_type="custom", custom_strike=custom)


def event(ticker: str, series: str, markets: list[dict]) -> dict:
    return {"event_ticker": ticker, "series_ticker": series, "title": "Example", "mutually_exclusive": series ==
            "KXFEDDECISION", "markets": markets}


def live_candle(end: int, bid: str | None, ask: str | None, last: str | None = None) -> dict:
    price = {"close_dollars": last} if last else {}
    return {"end_period_ts": end, "yes_bid": {"close_dollars": bid or "0.0000"},
            "yes_ask": {"close_dollars": ask or "1.0000"}, "price": price, "volume_fp": "1.00",
            "open_interest_fp": "100.00"}


def hist_candle(end: int, bid: str, ask: str) -> dict:
    return {"end_period_ts": end, "yes_bid": {"close": bid}, "yes_ask": {"close": ask},
            "price": {"close": None, "previous": None}, "volume": "0.00", "open_interest": "100.00"}


# 事件一的三段报价：c1（M7..M3）、c2（M2..M0）、c3（E0）
CPI1_CANDLES = {
    0.1: [("0.8900", "0.9100"), ("0.7900", "0.8100"), ("0.8900", "0.9100")],
    0.2: [("0.2900", "0.3100"), ("0.3900", "0.4100"), ("0.5700", "0.5900")],
    0.3: [("0.0400", "0.0600"), ("0.0900", "0.1100"), ("0.1600", "0.1800")],
}
CPI1_TIMES = [ts(2029, 4, 3, 14), ts(2029, 4, 8, 12), ts(2029, 4, 11, 11)]
# 事件二：一段正常报价，加一根收盘之后的离谱报价（不能用）
CPI2_CANDLES = {0.1: ("0.89", "0.91"), 0.2: ("0.69", "0.71"), 0.3: ("0.29", "0.31")}
# 议息：M7..M1 一段，E0 一段。H26 没有买价、卖价贴着 0：和面板一样按买价 0 补上，取 (0 + 卖价) / 2，
# 不用那个离谱的成交价（用了的话两段的中间价总和都是 1.05，结果的概率会不一样）
FED_LEGS = [
    ("C26", {"Cut": ">25"}, "no", [("0.0400", "0.0600", None), ("0.0100", "0.0300", None)]),
    ("C25", {"Cut": "25"}, "no", [("0.4900", "0.5100", None), ("0.0900", "0.1100", None)]),
    ("H0", {"Hike": "0"}, "yes", [("0.3900", "0.4100", None), ("0.8400", "0.8600", None)]),
    ("H25", {"Hike": "25"}, "no", [("0.0200", "0.0400", None), ("0.0100", "0.0300", None)]),
    ("H26", {"Hike": ">25"}, "no", [(None, "0.0400", "0.0700"), (None, "0.0200", "0.0600")]),
]
FED_TIMES = [ts(2029, 3, 14, 0), ts(2029, 3, 21, 11)]


def cpi_events() -> list[dict]:
    """接口给的顺序是旧到新，还混着一个旧时代的事件，代码要自己排、自己筛。"""
    unclear = [ladder_market("KXCPI-29JAN", k, CPI3_CLOSE, "yes") for k in (0.1, 0.2, 0.3)]
    legacy = [ladder_market("CPI-28DEC", 0.2, CPI3_CLOSE, "yes", "0.3")]
    first = [ladder_market("KXCPI-29MAR", 0.1, CPI1_CLOSE, "yes"), ladder_market("KXCPI-29MAR", 0.3, CPI1_CLOSE, "no"),
             ladder_market("KXCPI-29MAR", 0.2, CPI1_CLOSE, "yes")]
    return [
        event("CPI-28DEC", "KXCPI", legacy),
        event("KXCPI-29JAN", "KXCPI", unclear),
        event("KXCPI-29MAR", "KXCPI", first),
        event("KXCPI-29FEB", "KXCPI", []),  # 已归档：嵌套的合约是空的
    ]


def fed_events() -> list[dict]:
    return [event("KXFEDDECISION-29MAR", "KXFEDDECISION",
                  [fed_market(suffix, custom, result) for suffix, custom, result, _ in FED_LEGS])]


def cpi2_markets() -> list[dict]:
    return [ladder_market("KXCPI-29FEB", k, CPI2_CLOSE, "no", "0.1") for k in (0.1, 0.2, 0.3)]


def live_candles(ticker: str) -> list[dict]:
    series, rest = ticker.split("-", 1)
    if series == "KXCPI":
        k = float(rest.rsplit("-T", 1)[1])
        quotes = CPI1_CANDLES[k]
        found = [live_candle(t, bid, ask) for t, (bid, ask) in zip(CPI1_TIMES, quotes)]
        # 收盘之后那根：买 0 卖 1，只有成交价
        return found + [live_candle(ts(2029, 4, 11, 13), None, None, "0.9900")]
    if series == "KXFEDDECISION":
        suffix = rest.rsplit("-", 1)[1]
        legs = {leg[0]: leg[3] for leg in FED_LEGS}
        return [live_candle(t, bid, ask, last) for t, (bid, ask, last) in zip(FED_TIMES, legs[suffix])]
    if series == "KXPAYROLLS":
        k = float(rest.rsplit("-T", 1)[1])
        bid, ask = PAY_QUOTES[k]
        return [live_candle(ts(2029, 3, 30, 0), bid, ask)]
    return []


def hist_candles(ticker: str) -> list[dict]:
    k = float(ticker.rsplit("-T", 1)[1])
    bid, ask = CPI2_CANDLES[k]
    return [hist_candle(ts(2029, 3, 5, 13), bid, ask), hist_candle(ts(2029, 3, 13, 11), "0.97", "0.99")]


PAY_QUOTES = {50000.0: ("0.7900", "0.8100"), 100000.0: ("0.4900", "0.5100"), 150000.0: ("0.1900", "0.2100")}


def kalshi_handler(*, events: dict | None = None, throttle: tuple[str, ...] = ()):
    """按路径和查询参数分发。throttle 里的前缀（live_candles / hist_candles）一律返回 429。"""
    events = events if events is not None else {"KXCPI": cpi_events(), "KXFEDDECISION": fed_events()}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(PREFIX)
        params = request.url.params
        if path == "/events":
            assert params["status"] == "settled" and params["with_nested_markets"] == "true"
            return httpx.Response(200, json={"events": events.get(params["series_ticker"], []), "cursor": ""})
        if path == "/markets":
            return httpx.Response(200, json={"markets": [], "cursor": ""})
        if path == "/historical/markets":
            found = cpi2_markets() if params.get("event_ticker") == "KXCPI-29FEB" else []
            return httpx.Response(200, json={"markets": found, "cursor": ""})
        if path.startswith("/series/") and path.endswith("/candlesticks"):
            if "live_candles" in throttle:
                return httpx.Response(429, json={"error": "too many requests"})
            assert params["period_interval"] == "60"
            ticker = path.split("/")[4]
            return httpx.Response(200, json={"ticker": ticker, "candlesticks": live_candles(ticker)})
        if path.startswith("/historical/markets/") and path.endswith("/candlesticks"):
            if "hist_candles" in throttle:
                return httpx.Response(429, json={"error": "too many requests"})
            ticker = path.split("/")[3]
            return httpx.Response(200, json={"ticker": ticker, "candlesticks": hist_candles(ticker)})
        return httpx.Response(404, json={"code": "not_found"})

    return handler


def make_fetcher(handler, *, requests: int = 500, min_interval: float = 0.0):
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(record))
    fetcher = Fetcher(client, Budget(3000.0, requests, clock=clock), timeout=15.0, sleep=clock.sleep,
                      jitter=lambda: 0.0, min_interval=min_interval)
    return fetcher, calls, clock


def settings() -> MarketsSettings:
    base = MarketsSettings()
    return replace(base, kalshi=replace(base.kalshi, hosts=(BASE,)))


def labels(summary: dict, series: str) -> dict:
    return summary["series"][series]["labels"]


# ---------- 快照时刻 ----------


def test_snapshots_cpi_summer_and_fomc_skips_same_day_morning():
    # CPI 夏令时 08:30 ET = 北京 20:30：当晚 19:30 和当天早上 07:30 都在公布之前
    release = datetime(2029, 4, 11, 12, 30, tzinfo=timezone.utc)
    snaps = dict(backtest._snapshots(release))
    assert list(snaps) == ["E0", "M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7"]
    assert datetime.fromtimestamp(snaps["E0"], BJT) == datetime(2029, 4, 11, 19, 30, tzinfo=BJT)
    assert datetime.fromtimestamp(snaps["M0"], BJT) == datetime(2029, 4, 11, 7, 30, tzinfo=BJT)
    assert datetime.fromtimestamp(snaps["M7"], BJT) == datetime(2029, 4, 4, 7, 30, tzinfo=BJT)
    # 议息 14:00 EST = 北京第二天 03:00：E0 是前一天 19:30，北京当天早上的 M0 在公布之后，不存在
    fomc = datetime(2029, 1, 31, 19, 0, tzinfo=timezone.utc)
    snaps = dict(backtest._snapshots(fomc))
    assert "M0" not in snaps and len(snaps) == 8
    assert datetime.fromtimestamp(snaps["E0"], BJT) == datetime(2029, 1, 31, 19, 30, tzinfo=BJT)
    assert datetime.fromtimestamp(snaps["M1"], BJT) == datetime(2029, 1, 31, 7, 30, tzinfo=BJT)
    # 正好 19:30 公布时 E0 取前一天
    exact = datetime(2029, 1, 10, 19, 30, tzinfo=BJT)
    assert datetime.fromtimestamp(dict(backtest._snapshots(exact))["E0"], BJT) == exact - timedelta(days=1)


def test_lead_label():
    assert lead_label("evening", 3) == "E0"
    assert lead_label("morning", 0) == "M0"
    assert lead_label("morning", 3) == "M3"
    assert lead_label("morning", 12) == "M7"
    assert lead_label("morning", -1) == "M0"


def test_covers_and_median_error_with_open_bounds():
    assert covers(Bound(0.2), Bound(0.3), 0.3) and covers(Bound(0.2), Bound(0.3), 0.2)
    assert not covers(Bound(0.2), Bound(0.3), 0.1) and not covers(Bound(0.2), Bound(0.3), 0.4)
    assert covers(Bound(0.1, "below"), Bound(0.3), -0.5)  # 下端开口：没有下限
    assert covers(Bound(0.2), Bound(0.4, "above"), 9.9)  # 上端开口：没有上限
    assert not covers(Bound(0.4, "above"), Bound(0.4, "above"), 0.4)  # “高于 0.4”不含 0.4
    assert covers(Bound(0.30000000000000004), Bound(0.3), 0.3)  # 浮点误差
    assert backtest._median_error(Bound(0.2), 0.5, 0.1) == pytest.approx(0.3)
    assert backtest._median_error(Bound(0.1, "below"), 0.0, 0.1) == 0.0
    assert backtest._median_error(Bound(0.1, "below"), 0.3, 0.1) == pytest.approx(0.2)
    assert backtest._median_error(Bound(0.4, "above"), 0.2, 0.1) == pytest.approx(0.3)  # X 至少 0.5
    assert backtest._median_error(Bound(0.4, "above"), 0.6, 0.1) == 0.0
    assert backtest._median_error(Bound(150000.0, "above"), 120000.0, None) == pytest.approx(30000.0)


# ---------- 完整回放 ----------


def test_replay_exact_metrics(caplog):
    caplog.set_level(logging.DEBUG, logger="digest.markets")
    fetcher, calls, _ = make_fetcher(kalshi_handler())
    summary = run(fetcher, settings(), NOW, dry=False)

    assert summary["partial"] is False and summary["dry"] is False
    assert summary["series_count"] == 5
    assert summary["events"] == 4  # 三个 CPI（旧时代的不算）加一次议息
    assert summary["scored"] == 3
    assert summary["skipped"] == 1  # 全部 yes、没有结算值的那次
    assert summary["errors"] == 0
    # 5 个事件列表 + 归档事件的两次合约列表 + 3 + 3 + 5 个合约的 K 线
    assert summary["requests"] == len(calls) == 18
    cpi = labels(summary, "KXCPI")
    assert list(cpi) == ["E0", "M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7"]
    assert cpi["E0"] == {"n": 2, "mae_median": 0.1, "covered50": 1, "mass50_mean": 0.815, "rps_mean": 0.80265}
    for label in ("M0", "M1", "M2"):
        assert cpi[label] == {"n": 2, "mae_median": 0.15, "covered50": 1, "mass50_mean": 0.8, "rps_mean": 0.9}
    for label in ("M3", "M4", "M5", "M6", "M7"):
        assert cpi[label] == {"n": 2, "mae_median": 0.15, "covered50": 1, "mass50_mean": 0.875, "rps_mean": 0.94625}
    fed = labels(summary, "KXFEDDECISION")
    assert list(fed) == ["E0", "M1", "M2", "M3", "M4", "M5", "M6", "M7"]  # 议息没有 M0
    assert fed["E0"] == {"n": 1, "top_hit": 1, "p_actual_mean": 0.85}
    assert fed["M4"] == {"n": 1, "top_hit": 0, "p_actual_mean": 0.4}
    assert summary["series"]["KXCPI"] | {"labels": None} == {
        "name": "CPI 环比", "kind": "ladder", "unit": "pct", "events": 3, "scored": 2, "skipped": 1, "labels": None}
    assert summary["series"]["KXPAYROLLS"]["labels"] == {} and summary["series"]["KXPAYROLLS"]["events"] == 0
    # 每个合约只算一次（最早一个有买卖报价的快照）：CPI 两个 0.90 一个 yes；0.05、0.05、0.03 都没发生
    assert summary["bands"] == {"band_hi": {"n": 2, "yes": 1}, "band_lo": {"n": 3, "yes": 0}}

    # 说不清的事件和旧时代的事件不取 K 线；归档事件走历史接口
    paths = [c.url.path for c in calls]
    assert not any("29JAN" in p or "CPI-28DEC" in p for p in paths)
    assert sum(p.startswith(PREFIX + "/historical/markets/KXCPI-29FEB") for p in paths) == 3
    # K 线从公布前 8 天取到收盘
    first = next(c for c in calls if c.url.path.endswith("KXCPI-29MAR-T0.1/candlesticks"))
    assert int(first.url.params["start_ts"]) == ts(2029, 4, 3, 12, 30)
    assert int(first.url.params["end_ts"]) == int(CPI1_CLOSE.timestamp())

    json.dumps(summary, allow_nan=False)  # 能原样加密保存
    for record in caplog.records:
        text = record.getMessage()
        assert "KXCPI-" not in text and "KXFEDDECISION-" not in text and "0.89" not in text


def test_replay_never_uses_candles_after_close():
    """事件二提前收盘：19:30 之前、收盘之后的那根 K 线（买 0.97 卖 0.99）绝不能用。"""
    events = {"KXCPI": [event("KXCPI-29FEB", "KXCPI", [])]}
    fetcher, _, _ = make_fetcher(kalshi_handler(events=events))
    summary = run(fetcher, settings(), NOW, dry=False)
    e0 = labels(summary, "KXCPI")["E0"]
    # 只能是 0.90 / 0.70 / 0.30 那组报价：中位数 0.3，区间 [0.2, 高于 0.3]，公布值 0.1 不在里面
    assert e0 == {"n": 1, "mae_median": 0.2, "covered50": 0, "mass50_mean": 0.9, "rps_mean": 1.39}


def test_replay_payrolls_in_jobs_and_record_clause():
    pay = [market(f"KXPAYROLLS-29MAR-T{int(k)}", PAY_CLOSE, "yes" if k < 120000 else "no", strike_type="greater",
                  floor_strike=k, expiration_value="120,000") for k in PAY_QUOTES]
    events = {"KXPAYROLLS": [event("KXPAYROLLS-29MAR", "KXPAYROLLS", pay)]}
    fetcher, _, _ = make_fetcher(kalshi_handler(events=events))
    summary = run(fetcher, settings(), NOW, dry=False)
    m3 = labels(summary, "KXPAYROLLS")["M3"]
    # 连续的数：中位数插值到 100,000，比公布值少 20,000 人；区间 [58,333, 141,667] 含 120,000，概率 0.5
    assert m3 == {"n": 1, "mae_median": 20000.0, "covered50": 1, "mass50_mean": 0.5, "rps_mean": 0.33}
    assert record_clause(summary, "KXPAYROLLS", "M3") == "过去 1 次同一时刻（提前 3 天早上）：中位数平均差 2 万人"


def test_dry_run_takes_latest_two_events_per_series():
    fetcher, calls, _ = make_fetcher(kalshi_handler())
    summary = run(fetcher, settings(), NOW, dry=True)
    assert summary["dry"] is True
    # 最新的两次是 3 月和 2 月；1 月那次（说不清）根本没处理
    assert summary["series"]["KXCPI"]["events"] == 2 and summary["series"]["KXCPI"]["skipped"] == 0
    assert (summary["series_count"], summary["events"], summary["scored"]) == (5, 3, 3)
    assert summary["requests"] == len(calls) == 18


def test_two_consecutive_429_stop_and_keep_partial_results():
    """议息先跑完；CPI 第一个事件算完；归档事件的 K 线连续被限流，停下，已有的结果照样汇总。"""
    fetcher, calls, clock = make_fetcher(kalshi_handler(throttle=("hist_candles",)))
    summary = run(fetcher, settings(), NOW, dry=False)
    assert summary["partial"] is True
    assert labels(summary, "KXFEDDECISION")["E0"]["n"] == 1
    assert labels(summary, "KXCPI")["E0"] == {"n": 1, "mae_median": 0.0, "covered50": 1, "mass50_mean": 0.73,
                                              "rps_mean": 0.2153}
    assert summary["series_count"] == 2  # 后面三个系列没有再请求
    assert not any("KXCPICORE" in str(c.url) or "KXU3" in str(c.url) for c in calls)
    throttled = [c for c in calls if c.url.path.startswith(PREFIX + "/historical/markets/")]
    assert len(throttled) == 4  # 两个合约，各自被 Fetcher 重试一次
    assert backtest.THROTTLE_PAUSE in clock.slept  # 第一次被限流后歇了一会儿
    assert render_backtest(summary, NOW).text.count("部分完成") == 1


def test_throttle_counter_resets_after_success():
    """不连续的 429 不会让回测停下。"""
    state = {"n": 0}
    inner = kalshi_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/candlesticks"):
            state["n"] += 1
            if state["n"] in (1, 2, 5, 6):  # 第一个和第三个合约各自（连同重试）被限流一次
                return httpx.Response(429, json={"error": "too many requests"})
        return inner(request)

    fetcher, _, _ = make_fetcher(handler)
    summary = run(fetcher, settings(), NOW, dry=False)
    assert summary["partial"] is False and summary["errors"] == 2


def test_requests_are_spaced_by_min_interval():
    fetcher, calls, clock = make_fetcher(kalshi_handler(), min_interval=1.0)
    run(fetcher, settings(), NOW, dry=True)
    assert sum(s for s in clock.slept if s == 1.0) >= len(calls) - 1


def test_budget_exhausted_marks_partial():
    fetcher, calls, _ = make_fetcher(kalshi_handler(), requests=6)
    summary = run(fetcher, settings(), NOW, dry=False)
    assert summary["partial"] is True and len(calls) == 6 == summary["requests"]


def test_fed_tails_without_bids_are_filled_like_the_panel():
    """议息两端的冷门结果没有买单、也没有成交：和面板一样按买价 0 补上再归一化，不能因此整次跳过。"""
    legs = [("C26", {"Cut": ">25"}, "no", None, "0.0200"), ("C25", {"Cut": "25"}, "no", "0.1400", "0.1600"),
            ("H0", {"Hike": "0"}, "yes", "0.8000", "0.8200"), ("H25", {"Hike": "25"}, "no", None, "0.0300"),
            ("H26", {"Hike": ">25"}, "no", None, "0.0100")]
    quotes = {f"KXFEDDECISION-29MAR-{suffix}": (bid, ask) for suffix, _, _, bid, ask in legs}
    events = {"KXFEDDECISION": [event("KXFEDDECISION-29MAR", "KXFEDDECISION",
                                      [fed_market(suffix, custom, result) for suffix, custom, result, _, _ in legs])]}
    inner = kalshi_handler(events=events)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/candlesticks"):
            bid, ask = quotes[request.url.path.removeprefix(PREFIX).split("/")[4]]
            return httpx.Response(200, json={"candlesticks": [live_candle(ts(2029, 3, 14, 0), bid, ask)]})
        return inner(request)

    fetcher, _, _ = make_fetcher(handler)
    summary = run(fetcher, settings(), NOW, dry=False)
    fed = labels(summary, "KXFEDDECISION")
    # 中间价 0.01、0.15、0.81、0.015、0.005，总和 0.99
    assert fed["E0"] == {"n": 1, "top_hit": 1, "p_actual_mean": round(0.81 / 0.99, 6)}
    assert list(fed) == ["E0", "M1", "M2", "M3", "M4", "M5", "M6", "M7"] and summary["scored"] == 1


def test_ladder_tails_without_bids_are_filled_like_the_panel():
    """阶梯最高一档只有卖单（0.02）、成交价是很久以前的 0.45：按盘口算 0.01，不用那个成交价。"""
    candles = {0.1: ("0.8900", "0.9100"), 0.2: ("0.4900", "0.5100"), 0.3: (None, "0.0200")}
    pay = [ladder_market("KXCPI-29MAR", k, CPI1_CLOSE, "yes" if k < 0.2 else "no") for k in candles]
    inner = kalshi_handler(events={"KXCPI": [event("KXCPI-29MAR", "KXCPI", pay)]})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/candlesticks"):
            bid, ask = candles[float(request.url.path.removeprefix(PREFIX).split("/")[4].rsplit("-T", 1)[1])]
            return httpx.Response(200, json={"candlesticks": [live_candle(ts(2029, 4, 11, 11), bid, ask, "0.4500")]})
        return inner(request)

    fetcher, _, _ = make_fetcher(handler)
    e0 = labels(run(fetcher, settings(), NOW, dry=False), "KXCPI")["E0"]
    # S = 0.90、0.50、0.01；公布值 0.2：RPS = 0.1² + 0.5² + 0.01² = 0.2601（用成交价 0.45 的话是 0.4625）
    assert e0["n"] == 1 and e0["rps_mean"] == pytest.approx(0.2601)


def test_unreachable_kalshi_stops_the_replay_quickly():
    """两个地址都连不上：第一次请求把两个地址各试两次，然后停下（部分完成），不在每个系列上再等一遍。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    clock = FakeClock()
    calls: list[httpx.Request] = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r) or handler(r)))
    fetcher = Fetcher(client, Budget(3000.0, 500, clock=clock), timeout=15.0, sleep=clock.sleep, jitter=lambda: 0.0)
    base = settings()
    two_hosts = replace(base, kalshi=replace(base.kalshi, hosts=(BASE, "https://kalshi-spare.example-market.test/v2")))
    summary = run(fetcher, two_hosts, NOW, dry=False)
    assert summary["partial"] is True and summary["scored"] == 0
    assert len(calls) == 4 and summary["requests"] == 4


# ---------- 灰色小字 ----------


def test_record_clause_texts():
    fetcher, _, _ = make_fetcher(kalshi_handler())
    summary = run(fetcher, settings(), NOW, dry=False)
    assert record_clause(summary, "KXCPI", "M3") == "过去 2 次同一时刻（提前 3 天早上）：中位数平均差 0.15 个百分点"
    assert record_clause(summary, "KXCPI", "M0") == "过去 2 次同一时刻（公布当天早上）：中位数平均差 0.15 个百分点"
    assert record_clause(summary, "KXCPI", "E0") == "过去 2 次同一时刻（公布当晚 19:30）：中位数平均差 0.10 个百分点"
    assert record_clause(summary, "KXFEDDECISION", "E0") == "过去 1 次同一时刻：市场最看好的结果对了 1 次"
    assert record_clause(summary, "KXFEDDECISION", "M2") == "过去 1 次同一时刻：市场最看好的结果对了 0 次"
    # 没有的项都返回 None
    assert record_clause(summary, "KXFEDDECISION", "M0") is None
    assert record_clause(summary, "KXU3", "M1") is None
    assert record_clause(summary, "KXCPI", "M9") is None
    assert record_clause(None, "KXCPI", "M3") is None
    assert record_clause({"series": "oops"}, "KXCPI", "M3") is None
    assert record_clause({"series": {"KXCPI": {"labels": {"M3": {"n": 0, "mae_median": 0.1}}}}}, "KXCPI", "M3") is None
    assert record_clause({"series": {"KXCPI": {"labels": {"M3": {"n": 4, "mae_median": "x"}}}}}, "KXCPI", "M3") is None
    # 系列这一层直接放快照、没有 kind / unit 时按默认规则
    flat = {"series": {"KXPAYROLLS": {"M1": {"n": 5, "mae_median": 25000.0}},
                       "KXU3": {"M2": {"n": 3, "mae_median": 0.125}}}}
    assert record_clause(flat, "KXPAYROLLS", "M1") == "过去 5 次同一时刻（提前 1 天早上）：中位数平均差 2.5 万人"
    assert record_clause(flat, "KXU3", "M2") == "过去 3 次同一时刻（提前 2 天早上）：中位数平均差 0.13 个百分点"


def test_summary_renders_as_backtest_message():
    fetcher, _, _ = make_fetcher(kalshi_handler())
    summary = run(fetcher, settings(), NOW, dry=False)
    text = render_backtest(summary, NOW).text
    assert "CPI 环比（2 次）：公布当晚 19:30 中位数平均差 0.10 个百分点，一半把握区间命中 1 次；提前 3 天早上平均差 0.15 个百分点" in text
    assert "美联储议息（1 次）：公布前一晚 19:30 市场最看好的结果对了 1 次，实际结果的平均价格 85%" in text
    assert "跳过 1 次（结算不清楚）" in text and "请求 18 次" in text
