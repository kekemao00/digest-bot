from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from digest.markets.kalshi import (
    MAX_PERIODS,
    Candle,
    Kalshi,
    KMarket,
    fed_outcome,
    parse_candles,
    parse_event,
    parse_market,
    period_of,
    quote_at,
    settled_value,
)
from digest.markets.net import Budget, BudgetExceeded, FetchError, Fetcher
from digest.markets.types import Quote

# 全是编造的数字、月份和地址，字段名和格式照接口文档
PRIMARY_HOST = "kalshi-primary.example-market.test"
BACKUP_HOST = "kalshi-backup.example-market.test"
PRIMARY = f"https://{PRIMARY_HOST}/trade-api/v2"
BACKUP = f"https://{BACKUP_HOST}/trade-api/v2"
PREFIX = "/trade-api/v2"
CLOSE = datetime(2029, 4, 11, 12, 29, tzinfo=timezone.utc)
CLOSE_TS = int(CLOSE.timestamp())


class FakeClock:
    """假的单调时钟：sleep 不真睡，只把时间往前拨。"""

    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_kalshi(handler, *, requests=50, hosts=(PRIMARY, BACKUP)):
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(record))
    fetcher = Fetcher(client, Budget(600.0, requests, clock=clock), sleep=clock.sleep, jitter=lambda: 0.0)
    return Kalshi(fetcher, hosts), calls


def routes(table: dict):
    """按路径（去掉 /trade-api/v2 前缀）分发；表里的值是响应、JSON 或 request -> 响应的函数。没有的路径返回 404。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(PREFIX)
        item = table.get(path)
        if item is None:
            return httpx.Response(404, json={"code": "not_found", "message": "not found"})
        if callable(item):
            item = item(request)
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    return handler


def market(ticker: str, **fields) -> dict:
    d = {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-T", 1)[0],
        "status": "active",
        "close_time": "2029-04-11T12:29:00Z",
        "strike_type": "greater",
        "yes_sub_title": "Above",
        "yes_bid_dollars": "0.3100",
        "yes_ask_dollars": "0.3400",
        "last_price_dollars": "0.3300",
        "open_interest_fp": "1000.00",
        "result": "",
        "expiration_value": "",
    }
    d.update(fields)
    return d


def km(ticker: str, floor=None, result="", expiration_value="", strike_type="greater", cap=None,
       custom=None) -> KMarket:
    return KMarket(
        ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0], status="finalized", close_time=CLOSE,
        strike_type=strike_type, floor=floor, cap=cap, custom=custom, yes_sub_title="", quote=Quote(None, None, None),
        open_interest=0.0, result=result, expiration_value=expiration_value, settlement_value=None,
    )


def ladder(results: dict[float, str], expiration_value: str = "", strike_type: str = "greater") -> list[KMarket]:
    return [km(f"KXCPI-29MAR-T{k}", floor=k, result=r, expiration_value=expiration_value, strike_type=strike_type)
            for k, r in results.items()]


# ---------- 解析 ----------


def test_parse_market_decimal_strings_and_fields():
    m = parse_market(market(
        "KXCPI-29MAR-T0.2", floor_strike=0.2, yes_sub_title="Above 0.2%", yes_bid_dollars="0.4100",
        yes_ask_dollars="0.4400", last_price_dollars="0.4300", open_interest_fp="2222.50",
        close_time="2029-04-11T12:29:00.12345Z",
    ))
    assert m.ticker == "KXCPI-29MAR-T0.2" and m.event_ticker == "KXCPI-29MAR"
    assert m.quote == Quote(0.41, 0.44, 0.43)
    assert m.open_interest == 2222.5
    assert m.floor == 0.2 and m.cap is None and m.custom is None
    assert m.strike_type == "greater" and m.status == "active"
    assert m.close_time == CLOSE + timedelta(microseconds=123450)
    assert m.settlement_value is None and m.result == "" and m.expiration_value == ""


def test_parse_market_empty_book_is_none_not_a_price():
    m = parse_market(market("KXCPI-29MAR-T0.5", yes_bid_dollars="0.0000", yes_ask_dollars="1.0000",
                            last_price_dollars="0.0000"))
    assert m.quote == Quote(None, None, None)
    assert m.quote.mid() is None
    # 只有一边没单：另一边照常
    thin = parse_market(market("KXCPI-29MAR-T0.6", yes_bid_dollars="0.0000", yes_ask_dollars="0.0700"))
    assert thin.quote.bid is None and thin.quote.ask == 0.07


def test_parse_market_settled_and_missing_keys():
    d = market("KXU3-29MAR-T4.6", status="finalized", result="no", expiration_value="4.6%",
               settlement_value_dollars="0.0000", yes_bid_dollars="0.0000", yes_ask_dollars="1.0000",
               last_price_dollars="0.0800", open_interest_fp="0.00")
    m = parse_market(d)
    assert m.result == "no" and m.settlement_value == 0.0 and m.expiration_value == "4.6%"
    # 收盘后的报价没有意义：买 0 卖 1 都成了 None，成交价停在收盘前
    assert m.quote == Quote(None, None, 0.08)
    del d["result"]
    del d["settlement_value_dollars"]
    bare = parse_market(d)
    assert bare.result == "" and bare.settlement_value is None


def test_parse_market_bad_values_fall_back():
    m = parse_market(market("KXCPI-29MAR-T0.1", yes_bid_dollars="abc", yes_ask_dollars="NaN",
                            last_price_dollars="1.5000", open_interest_fp="oops", floor_strike="n/a",
                            close_time="yesterday", custom_strike=["Hike"]))
    assert m.quote == Quote(None, None, None)
    assert m.open_interest == 0.0 and m.floor is None and m.close_time is None and m.custom is None
    with pytest.raises(ValueError):
        parse_market({"event_ticker": "KXCPI-29MAR"})
    with pytest.raises(ValueError):
        parse_market(["not", "a", "dict"])  # type: ignore[arg-type]


@pytest.mark.parametrize("text,expected", [
    ("2029-04-11T12:29:00Z", CLOSE),
    ("2029-04-11T12:29:00.5Z", CLOSE + timedelta(microseconds=500000)),
    ("2029-04-11T12:29:00.123Z", CLOSE + timedelta(microseconds=123000)),
    ("2029-04-11T12:29:00.123456Z", CLOSE + timedelta(microseconds=123456)),
    ("2029-04-11T12:29:00.123456789Z", CLOSE + timedelta(microseconds=123456)),
    ("2029-04-11T20:29:00+08:00", CLOSE),
    ("0001-01-01T00:00:00Z", None),  # Go 的零值时间表示没有
    ("", None),
])
def test_parse_market_close_time_formats(text, expected):
    m = parse_market(market("KXCPI-29MAR-T0.3", close_time=text))
    assert m.close_time == expected
    if expected is not None:
        assert m.close_time.tzinfo is not None and m.close_time.utcoffset() == timedelta(0)


def test_parse_event_sorts_ladder_by_floor_strike():
    raw = {
        "event_ticker": "KXCPI-29MAR", "series_ticker": "KXCPI", "title": "CPI in March",
        "mutually_exclusive": False, "last_updated_ts": "0001-01-01T00:00:00Z",
        "markets": [
            market("KXCPI-29MAR-T0.3", floor_strike=0.3, open_interest_fp="100.00"),
            market("KXCPI-29MAR-T-0.2", floor_strike=-0.2, open_interest_fp="200.50"),
            {"no_ticker": True},
            market("KXCPI-29MAR-T0.0", floor_strike=0),
            market("KXCPI-29MAR-T0.1", floor_strike=0.1, open_interest_fp="300.00"),
        ],
    }
    event = parse_event(raw)
    assert [m.floor for m in event.markets] == [-0.2, 0.0, 0.1, 0.3]
    assert event.series_ticker == "KXCPI" and event.mutually_exclusive is False and event.strike_date is None
    assert event.open_interest == pytest.approx(100 + 200.5 + 1000 + 300)


def test_parse_event_core_cpi_cap_equals_floor_still_sorts_by_floor():
    raw = {"event_ticker": "KXCPICORE-29MAR", "series_ticker": "KXCPICORE", "markets": [
        market("KXCPICORE-29MAR-T0.4", floor_strike=0.4, cap_strike=0.4),
        market("KXCPICORE-29MAR-T0.2", floor_strike=0.2, cap_strike=0.2),
        market("KXCPICORE-29MAR-T0.3", floor_strike=0.3, cap_strike=0.3),
    ]}
    assert [m.floor for m in parse_event(raw).markets] == [0.2, 0.3, 0.4]


def test_parse_event_buckets_sort_less_by_cap_first():
    raw = {"event_ticker": "KXINXY-29DEC31H1600", "series_ticker": "KXINXY", "mutually_exclusive": True,
           "strike_date": "2029-12-31T21:00:00Z", "markets": [
               market("KXINXY-29DEC31H1600-T5600", strike_type="greater", floor_strike=5600),
               market("KXINXY-29DEC31H1600-B5300", strike_type="between", floor_strike=5200, cap_strike=5399.99),
               market("KXINXY-29DEC31H1600-T5200", strike_type="less", cap_strike=5200),
               market("KXINXY-29DEC31H1600-B5500", strike_type="between", floor_strike=5400, cap_strike=5600),
           ]}
    event = parse_event(raw)
    assert [m.ticker.rsplit("-", 1)[1] for m in event.markets] == ["T5200", "B5300", "B5500", "T5600"]
    assert event.mutually_exclusive is True
    assert event.strike_date == datetime(2029, 12, 31, 21, 0, tzinfo=timezone.utc)


def test_parse_event_fed_outcomes_sorted_cut_to_hike():
    legs = [("H25", {"Hike": "25"}), ("C26", {"Cut": ">25"}), ("H0", {"Hike": "0"}),
            ("H26", {"Hike": ">25"}), ("C25", {"Cut": "25"})]
    raw = {"event_ticker": "KXFEDDECISION-29JAN", "series_ticker": "KXFEDDECISION", "mutually_exclusive": True,
           "markets": [market(f"KXFEDDECISION-29JAN-{s}", strike_type="custom", custom_strike=c) for s, c in legs]}
    event = parse_event(raw)
    assert [fed_outcome(m) for m in event.markets] == [
        "降息超过 25 个基点", "降息 25 个基点", "维持不变", "加息 25 个基点", "加息超过 25 个基点"]
    assert event.markets[2].custom == {"Hike": "0"}
    with pytest.raises(ValueError):
        parse_event({"series_ticker": "KXCPI"})


def test_parse_candles_live_keys():
    items = [
        {"end_period_ts": 1_900_003_600, "open_interest_fp": "50.00", "volume_fp": "0.00",
         "price": {"previous_dollars": "0.2200"},
         "yes_bid": {"close_dollars": "0.2000", "open_dollars": "0.1900", "high_dollars": "0.2100",
                     "low_dollars": "0.1900"},
         "yes_ask": {"close_dollars": "0.2400", "open_dollars": "0.2400", "high_dollars": "0.2500",
                     "low_dollars": "0.2300"}},
        {"end_period_ts": 1_900_000_000, "open_interest_fp": "40.00", "volume_fp": "12.00",
         "price": {"close_dollars": "0.2100", "previous_dollars": "0.2000", "mean_dollars": "0.2050"},
         "yes_bid": {"close_dollars": "0.2000"}, "yes_ask": {"close_dollars": "0.2300"}},
        {"end_period_ts": "not-a-number", "yes_bid": {"close_dollars": "0.5000"}},
    ]
    assert parse_candles(items) == [
        Candle(1_900_000_000, 0.20, 0.23, 0.21, 0.20),
        Candle(1_900_003_600, 0.20, 0.24, None, 0.22),
    ]


def test_parse_candles_historical_keys_and_empty_book():
    items = [
        {"end_period_ts": 1_900_000_000, "open_interest": "40.00", "volume": "0.00",
         "price": {"close": None, "high": None, "low": None, "mean": None, "open": None, "previous": "0.6100"},
         "yes_bid": {"close": "0.6000", "open": "0.5900"}, "yes_ask": {"close": "0.6300", "open": "0.6400"}},
        # 收盘后的那根：买 0 卖 1 是空盘口，不是价格
        {"end_period_ts": 1_900_003_600, "open_interest": "40.00", "volume": "3.00",
         "price": {"close": "0.6200", "previous": "0.6100"},
         "yes_bid": {"close": "0.0000"}, "yes_ask": {"close": "1.0000"}},
        {"end_period_ts": 1_900_007_200, "price": None, "yes_bid": None, "yes_ask": {}},
    ]
    assert parse_candles(items) == [
        Candle(1_900_000_000, 0.60, 0.63, None, 0.61),
        Candle(1_900_003_600, None, None, 0.62, 0.61),
        Candle(1_900_007_200, None, None, None, None),
    ]
    assert parse_candles([]) == [] and parse_candles(None) == []  # type: ignore[arg-type]


def test_quote_at_never_uses_candles_after_close():
    candles = [
        Candle(CLOSE_TS - 3600, 0.30, 0.34, 0.32, 0.31),
        Candle(CLOSE_TS, 0.31, 0.35, None, 0.33),  # 正好在收盘时刻结束：有效
        Candle(CLOSE_TS + 1860, None, None, 0.90, 0.33),  # 收盘后的空盘口
    ]
    # 发送时刻在收盘之后也只看收盘前的；成交价没有时用之前的最后成交价
    assert quote_at(candles, CLOSE_TS + 7200, CLOSE_TS) == (Quote(0.31, 0.35, 0.33), 0)
    assert quote_at(candles, CLOSE_TS - 60, CLOSE_TS) == (Quote(0.30, 0.34, 0.32), 3540)
    assert quote_at(list(reversed(candles)), CLOSE_TS, CLOSE_TS) == (Quote(0.31, 0.35, 0.33), 0)
    assert quote_at(candles, CLOSE_TS - 7200, CLOSE_TS) is None
    assert quote_at([candles[2]], CLOSE_TS + 9999, CLOSE_TS) is None
    assert quote_at([], CLOSE_TS, CLOSE_TS) is None


def test_quote_at_minute_candle_ending_exactly_at_close():
    close = CLOSE_TS - 240  # 收盘在 :25 这样的整分钟
    candles = [Candle(close - 60, 0.44, 0.47, None, 0.45), Candle(close, 0.45, 0.48, 0.46, 0.45),
               Candle(close + 60, None, None, None, 0.46)]
    assert quote_at(candles, close + 3600, close) == (Quote(0.45, 0.48, 0.46), 0)


@pytest.mark.parametrize("ticker,expected", [
    ("KXCPI-26OCT", (2026, 10)),
    ("KXFEDDECISION-26DEC", (2026, 12)),
    ("KXINXY-26DEC31H1600", (2026, 12)),
    ("KXRECSSNBER-27", (2027, 12)),
    ("KXINXY-29DEC31", (2029, 12)),
    ("KXPAYROLLS-29MAR-T-25000", (2029, 3)),
    ("kxu3-29jan", (2029, 1)),
    ("KXCPI-29XYZ", None),
    ("KXCPI", None),
    ("KXCPI-ABC", None),
    ("", None),
])
def test_period_of(ticker, expected):
    assert period_of(ticker) == expected


def test_fed_outcome_custom_strike_then_ticker_suffix():
    assert fed_outcome(km("KXFEDDECISION-29JAN-X1", strike_type="custom", custom={"Cut": ">25"})) == "降息超过 25 个基点"
    assert fed_outcome(km("KXFEDDECISION-29JAN-X2", strike_type="custom", custom={"Cut": "25"})) == "降息 25 个基点"
    assert fed_outcome(km("KXFEDDECISION-29JAN-X3", strike_type="custom", custom={"Hike": "0"})) == "维持不变"
    assert fed_outcome(km("KXFEDDECISION-29JAN-X4", strike_type="custom", custom={"hike": " 25 "})) == "加息 25 个基点"
    assert fed_outcome(km("KXFEDDECISION-29JAN-X5", strike_type="custom", custom={"Hike": ">25"})) == "加息超过 25 个基点"
    # 没有 custom_strike：看后缀
    assert fed_outcome(km("KXFEDDECISION-29JAN-C26", strike_type=None)) == "降息超过 25 个基点"
    assert fed_outcome(km("KXFEDDECISION-29JAN-C25", strike_type=None)) == "降息 25 个基点"
    assert fed_outcome(km("KXFEDDECISION-29JAN-H0", strike_type=None)) == "维持不变"
    assert fed_outcome(km("KXFEDDECISION-29JAN-H25", strike_type=None)) == "加息 25 个基点"
    assert fed_outcome(km("KXFEDDECISION-29JAN-H26", strike_type=None)) == "加息超过 25 个基点"
    # custom_strike 认不出时也退回后缀
    assert fed_outcome(km("KXFEDDECISION-29JAN-H25", strike_type="custom", custom={"Pause": "?"})) == "加息 25 个基点"
    assert fed_outcome(km("KXFEDDECISION-29JAN-H50", strike_type=None)) is None
    assert fed_outcome(km("KXFED-29JAN-T3.25", floor=3.25)) is None


# ---------- 结算值 ----------


def test_settled_value_ladder_numeric_expiration_value():
    results = {-0.1: "yes", 0.0: "yes", 0.1: "yes", 0.2: "no", 0.3: "no"}
    for text in ("0.2", "0.20", "0.2%", ".2%", " 0.2 "):
        assert settled_value("ladder", ladder(results, text)) == pytest.approx(0.2)
    jobs = {-20000.0: "yes", 0.0: "yes", 30000.0: "yes", 60000.0: "no", 90000.0: "no"}
    assert settled_value("ladder", ladder(jobs, "41,000")) == 41000.0
    assert settled_value("ladder", ladder({4.4: "yes", 4.5: "no"}, "4.5%")) == 4.5
    negative = {-0.3: "yes", -0.2: "no", -0.1: "no"}
    assert settled_value("ladder", ladder(negative, "-.2")) == pytest.approx(-0.2)


def test_settled_value_label_style_uses_strike_results():
    # 核心 CPI 的结算值是 "Above 0.1%" 这样的标签：0.1 档 yes、0.2 档 no，公布值只能是 0.2
    results = {0.0: "yes", 0.1: "yes", 0.2: "no", 0.3: "no", 0.4: "no"}
    markets = [km(f"KXCPICORE-29MAR-T{k}", floor=k, cap=k, result=r, expiration_value="Above 0.1%")
               for k, r in results.items()]
    assert settled_value("ladder", markets) == pytest.approx(0.2)
    # 结算值为空也一样
    assert settled_value("ladder", ladder(results)) == pytest.approx(0.2)
    # 大于等于类的合约：X >= 高的 yes 档且 X < 低的 no 档
    assert settled_value("ladder", ladder({0.1: "yes", 0.2: "yes", 0.3: "no"}, strike_type="greater_or_equal")) == \
        pytest.approx(0.2)


@pytest.mark.parametrize("results", [
    {0.0: "yes", 0.2: "no", 0.4: "no"},  # 两档之间隔了 0.2，可能是 0.1 也可能是 0.2
    {0.0: "no", 0.1: "no", 0.2: "no"},  # 全 no：在阶梯下面
    {0.0: "yes", 0.1: "yes"},  # 全 yes：在阶梯上面
    {0.0: "yes", 0.1: "no", 0.2: "yes"},  # 不单调
    {0.0: "yes", 0.1: "", 0.2: "no"},  # 还有没出结果的
    {0.05: "yes", 0.15: "no"},  # 行权价不在一位小数上
    {25000.0: "yes", 50000.0: "no"},  # 非农人数是连续的，只靠结果推不出来
])
def test_settled_value_ambiguous_results_return_none(results):
    assert settled_value("ladder", ladder(results)) is None


def test_settled_value_rejects_inconsistent_or_conflicting_values():
    # 结算值 0.2，可 0.2 档却是 yes（严格大于时应为 no）
    assert settled_value("ladder", ladder({0.1: "yes", 0.2: "yes", 0.3: "no"}, "0.2")) is None
    mixed = ladder({0.1: "yes", 0.2: "no"}, "0.2")
    mixed[0] = km("KXCPI-29MAR-T0.1", floor=0.1, result="yes", expiration_value="0.3")
    assert settled_value("ladder", mixed) is None
    assert settled_value("ladder", []) is None
    assert settled_value("unknown", ladder({0.1: "yes", 0.2: "no"}, "0.2")) is None


def test_settled_value_fed_binary_buckets():
    legs = [km(f"KXFEDDECISION-29JAN-{s}", strike_type="custom", custom=c, result=r, expiration_value="Cut 25bps")
            for s, c, r in (("C26", {"Cut": ">25"}, "no"), ("C25", {"Cut": "25"}, "yes"), ("H0", {"Hike": "0"}, "no"))]
    assert settled_value("fed", legs) == "降息 25 个基点"
    assert settled_value("fed", [legs[0], legs[2]]) is None
    two = [legs[1], km("KXFEDDECISION-29JAN-H0", strike_type="custom", custom={"Hike": "0"}, result="yes")]
    assert settled_value("fed", two) is None

    assert settled_value("binary", [km("KXRECSSNBER-29", strike_type=None, result="no", expiration_value="No")]) == 0.0
    assert settled_value("binary", [km("KXRECSSNBER-29", strike_type=None, result="yes")]) == 1.0
    assert settled_value("binary", [km("KXRECSSNBER-29", strike_type=None, result="")]) is None

    buckets = [
        km("KXINXY-29DEC31H1600-T5200", strike_type="less", cap=5200, result="no", expiration_value="5321.5"),
        km("KXINXY-29DEC31H1600-B5300", strike_type="between", floor=5200, cap=5399.99, result="yes",
           expiration_value="5321.5"),
        km("KXINXY-29DEC31H1600-T5400", floor=5400, result="no", expiration_value="5321.5"),
    ]
    assert settled_value("buckets", buckets) == 5321.5
    assert settled_value("buckets", [replace(b, result="") if i else b for i, b in enumerate(buckets)]) == 5321.5
    wrong = [replace(buckets[0], result="yes"), *buckets[1:]]
    assert settled_value("buckets", wrong) is None
    assert settled_value("buckets", [replace(b, expiration_value="") for b in buckets]) is None


# ---------- 客户端 ----------


def event_payload(month: str, strikes: list[float]) -> dict:
    return {
        "event_ticker": f"KXCPI-{month}", "series_ticker": "KXCPI", "title": "CPI", "mutually_exclusive": False,
        "collateral_return_type": "DIRECNET", "strike_period": "",
        "markets": [market(f"KXCPI-{month}-T{k}", floor_strike=k) for k in reversed(strikes)],
    }


def test_events_paginates_by_cursor_until_empty_string():
    pages = {
        "": {"events": [event_payload("29MAY", [0.1, 0.2]), event_payload("29APR", [0.0])], "cursor": "page-two",
             "milestones": []},
        "page-two": {"events": [event_payload("29MAR", [0.2, 0.3]), event_payload("29APR", [0.0])], "cursor": "",
                     "milestones": []},
    }
    kalshi, calls = make_kalshi(routes({"/events": lambda r: pages[r.url.params.get("cursor", "")]}))
    events = kalshi.events("KXCPI")
    assert [e.event_ticker for e in events] == ["KXCPI-29MAY", "KXCPI-29APR", "KXCPI-29MAR"]
    assert [m.floor for m in events[0].markets] == [0.1, 0.2]
    assert len(calls) == 2
    first = calls[0].url.params
    assert first["series_ticker"] == "KXCPI" and first["status"] == "open"
    assert first["with_nested_markets"] == "true" and first["limit"] == "200" and "cursor" not in first
    assert calls[1].url.params["cursor"] == "page-two"
    assert all(c.url.host == PRIMARY_HOST for c in calls)


def test_events_stops_at_max_pages_and_omits_empty_status(caplog):
    caplog.set_level(logging.INFO, logger="digest.markets")
    counter = iter(range(100))
    kalshi, calls = make_kalshi(routes({"/events": lambda r: {
        "events": [event_payload(f"2{next(counter)}JAN", [0.1])], "cursor": "more"}}))
    events = kalshi.events("KXCPI", status="", nested=False, max_pages=2)
    assert len(calls) == 2 and len(events) == 2
    assert "status" not in calls[0].url.params and calls[0].url.params["with_nested_markets"] == "false"
    assert "KXCPI" not in caplog.text


def test_events_missing_cursor_or_bad_shape_stops():
    kalshi, calls = make_kalshi(routes({"/events": {"events": "oops"}}))
    assert kalshi.events("KXCPI") == [] and len(calls) == 1


def test_host_fallback_on_connect_error_then_sticks(caplog):
    caplog.set_level(logging.INFO, logger="digest.markets")
    ok = routes({"/historical/cutoff": {"market_settled_ts": "2029-03-01T00:00:00Z"}})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == PRIMARY_HOST:
            raise httpx.ConnectError("refused", request=request)
        return ok(request)

    kalshi, calls = make_kalshi(handler)
    assert kalshi.cutoff() == datetime(2029, 3, 1, tzinfo=timezone.utc)
    # 主地址：第一次加重试一次，然后同一路径发到备用地址
    assert [c.url.host for c in calls] == [PRIMARY_HOST, PRIMARY_HOST, BACKUP_HOST]
    assert all(c.url.path == f"{PREFIX}/historical/cutoff" for c in calls)
    # 之后的请求直接走备用地址
    assert kalshi.cutoff() is not None
    assert [c.url.host for c in calls[3:]] == [BACKUP_HOST]
    assert kalshi.fetcher.requests == 4
    assert "cutoff" not in caplog.text and "trade-api" not in caplog.text


def test_host_fallback_on_5xx():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == PRIMARY_HOST:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"market_settled_ts": "2029-03-01T00:00:00.000Z"})

    kalshi, calls = make_kalshi(handler)
    assert kalshi.cutoff() == datetime(2029, 3, 1, tzinfo=timezone.utc)
    assert [c.url.host for c in calls] == [PRIMARY_HOST, PRIMARY_HOST, BACKUP_HOST]


def test_no_host_fallback_on_4xx():
    kalshi, calls = make_kalshi(lambda r: httpx.Response(403, json={"code": "forbidden"}))
    with pytest.raises(FetchError) as info:
        kalshi.cutoff()
    assert info.value.kind == "4xx" and info.value.status == 403
    assert [c.url.host for c in calls] == [PRIMARY_HOST]


def test_both_hosts_down_raises_fetch_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    kalshi, calls = make_kalshi(handler)
    with pytest.raises(FetchError) as info:
        kalshi.events("KXCPI")
    assert info.value.kind == "connect" and info.value.host == BACKUP_HOST
    assert len(calls) == 4


def test_unreachable_kalshi_stops_sending_requests(caplog):
    """两个地址都连不上：第一次请求把两个地址各试两次，之后的请求不再发出，直接报“连不上”。"""
    caplog.set_level(logging.INFO, logger="digest.markets")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    kalshi, calls = make_kalshi(handler)
    with pytest.raises(FetchError) as info:
        kalshi.events("KXCPI")
    assert info.value.kind == "connect" and len(calls) == 4 and kalshi.down
    for series in ("KXPAYROLLS", "KXU3"):
        with pytest.raises(FetchError) as info:
            kalshi.events(series)
        assert info.value.kind == "down"
    assert len(calls) == 4  # 没有再发请求
    # Summary 里看到的仍是最近一次真正的结果
    assert kalshi.fetcher.hosts == {PRIMARY_HOST: "ConnectTimeout", BACKUP_HOST: "ConnectTimeout"}
    assert "KXPAYROLLS" not in caplog.text and "trade-api" not in caplog.text


def test_timed_out_host_is_skipped_until_a_request_succeeds():
    """主地址读超时（不换地址重试）：下一个请求直接走备用地址；任何一次成功后两个地址都恢复可用。"""
    state = {"primary_ok": False}
    ok = routes({"/historical/cutoff": {"market_settled_ts": "2029-03-01T00:00:00Z"}})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == PRIMARY_HOST and not state["primary_ok"]:
            raise httpx.ReadTimeout("slow", request=request)
        return ok(request)

    kalshi, calls = make_kalshi(handler)
    with pytest.raises(FetchError) as info:
        kalshi.cutoff()
    assert info.value.kind == "timeout" and [c.url.host for c in calls] == [PRIMARY_HOST, PRIMARY_HOST]
    assert kalshi.dead == {PRIMARY} and not kalshi.down
    assert kalshi.cutoff() is not None
    assert [c.url.host for c in calls[2:]] == [BACKUP_HOST]
    assert kalshi.dead == set()
    # 共用一份记录的另一个客户端：一开始就知道哪些地址连不上
    other, other_calls = make_kalshi(handler)
    other.dead = {PRIMARY, BACKUP}
    with pytest.raises(FetchError) as info:
        other.cutoff()
    assert info.value.kind == "down" and other_calls == []


def test_budget_exceeded_propagates_unwrapped():
    kalshi, calls = make_kalshi(routes({"/events": {"events": [], "cursor": "again"}}), requests=1)
    with pytest.raises(BudgetExceeded):
        kalshi.events("KXCPI")
    assert len(calls) == 1


def test_markets_live_first_sorted():
    table = {"/markets": {"markets": [market("KXU3-29MAR-T4.5", floor_strike=4.5),
                                      market("KXU3-29MAR-T4.3", floor_strike=4.3),
                                      market("KXU3-29MAR-T4.4", floor_strike=4.4)], "cursor": ""}}
    kalshi, calls = make_kalshi(routes(table))
    assert [m.floor for m in kalshi.markets("KXU3-29MAR")] == [4.3, 4.4, 4.5]
    assert len(calls) == 1
    assert calls[0].url.params["event_ticker"] == "KXU3-29MAR" and calls[0].url.params["limit"] == "1000"


def test_markets_falls_back_to_historical_and_candles_follow():
    hist_markets = [market("KXU3-28MAR-T4.4", floor_strike=4.4, status="finalized", result="no"),
                    market("KXU3-28MAR-T4.3", floor_strike=4.3, status="finalized", result="yes")]
    table = {
        "/markets": {"markets": [], "cursor": ""},
        "/historical/markets": {"markets": hist_markets, "cursor": ""},
        "/historical/markets/KXU3-28MAR-T4.3/candlesticks": {"ticker": "KXU3-28MAR-T4.3", "candlesticks": [
            {"end_period_ts": 1_850_000_400, "yes_bid": {"close": "0.7000"}, "yes_ask": {"close": "0.7300"},
             "price": {"close": None, "previous": "0.7100"}, "volume": "0.00", "open_interest": "9.00"}]},
    }
    kalshi, calls = make_kalshi(routes(table))
    found = kalshi.markets("KXU3-28MAR")
    assert [m.floor for m in found] == [4.3, 4.4]
    assert [c.url.path for c in calls] == [f"{PREFIX}/markets", f"{PREFIX}/historical/markets"]
    # 只在历史库里的合约，K 线直接走历史接口
    candles = kalshi.candles("KXU3", "KXU3-28MAR-T4.3", 1_850_000_000, 1_850_003_600)
    assert candles == [Candle(1_850_000_400, 0.70, 0.73, None, 0.71)]
    assert calls[-1].url.path == f"{PREFIX}/historical/markets/KXU3-28MAR-T4.3/candlesticks"
    assert len(calls) == 3


def test_markets_paginates():
    pages = {"": {"markets": [market("KXCPI-29MAR-T0.2", floor_strike=0.2)], "cursor": "next-page"},
             "next-page": {"markets": [market("KXCPI-29MAR-T0.1", floor_strike=0.1),
                                       market("KXCPI-29MAR-T0.2", floor_strike=0.2)], "cursor": ""}}
    kalshi, calls = make_kalshi(routes({"/markets": lambda r: pages[r.url.params.get("cursor", "")]}))
    assert [m.ticker for m in kalshi.markets("KXCPI-29MAR")] == ["KXCPI-29MAR-T0.1", "KXCPI-29MAR-T0.2"]
    assert len(calls) == 2


def test_market_live_else_historical_else_none():
    live = market("KXCPI-29MAR-T0.2", floor_strike=0.2)
    archived = market("KXCPI-28MAR-T0.2", floor_strike=0.2, status="finalized", result="yes")

    def lookup(store):
        def respond(request: httpx.Request):
            ticker = request.url.params["tickers"]
            return {"markets": [m for m in store if m["ticker"] == ticker], "cursor": ""}
        return respond

    kalshi, calls = make_kalshi(routes({"/markets": lookup([live]), "/historical/markets": lookup([archived])}))
    assert kalshi.market("KXCPI-29MAR-T0.2").status == "active"
    assert len(calls) == 1
    found = kalshi.market("KXCPI-28MAR-T0.2")
    assert found is not None and found.result == "yes"
    assert [c.url.path for c in calls[1:]] == [f"{PREFIX}/markets", f"{PREFIX}/historical/markets"]
    assert kalshi.market("KXCPI-27MAR-T0.2") is None


def test_market_404_counts_as_missing():
    kalshi, calls = make_kalshi(routes({}))
    assert kalshi.market("KXCPI-29MAR-T0.2") is None
    assert len(calls) == 2


def test_cutoff_bad_payload():
    kalshi, _ = make_kalshi(routes({"/historical/cutoff": ["unexpected"]}))
    assert kalshi.cutoff() is None


def candle_handler(seen: list[tuple[int, int, int]]):
    """记下每次请求的范围，返回范围内第一个和最后一个周期边界上的 K 线（最后一根重复一次，检查去重）。"""

    def respond(request: httpx.Request):
        start = int(request.url.params["start_ts"])
        end = int(request.url.params["end_ts"])
        period = int(request.url.params["period_interval"])
        seen.append((start, end, period))
        step = period * 60
        first = -(-start // step) * step
        last = end // step * step
        return {"ticker": "x", "candlesticks": [
            {"end_period_ts": ts, "yes_bid": {"close_dollars": "0.4000"}, "yes_ask": {"close_dollars": "0.4500"},
             "price": {}, "volume_fp": "0.00", "open_interest_fp": "1.00"}
            for ts in (first, last, last) if start <= ts <= end]}

    return respond


def test_candles_chunk_to_max_periods_per_request():
    seen: list[tuple[int, int, int]] = []
    path = "/series/KXCPI/markets/KXCPI-29MAR-T0.2/candlesticks"
    kalshi, calls = make_kalshi(routes({path: candle_handler(seen)}))
    start = 1_900_000_020  # 不在整分钟上
    end = start + 10_000 * 60
    candles = kalshi.candles("KXCPI", "KXCPI-29MAR-T0.2", start, end, period=1)
    assert len(seen) == 3
    for lo, hi, period in seen:
        assert period == 1
        # 两端都含，周期边界不超过 MAX_PERIODS 个
        assert hi // 60 - (-(-lo // 60)) + 1 <= MAX_PERIODS
    assert seen[0][0] == start and seen[-1][1] == end
    assert all(seen[i + 1][0] == seen[i][1] + 1 for i in range(len(seen) - 1))
    stamps = [c.end_ts for c in candles]
    assert stamps == sorted(set(stamps)) and len(stamps) == 6
    assert all(start <= ts <= end for ts in stamps)
    assert candles[0] == Candle(stamps[0], 0.40, 0.45, None, None)


def test_candles_hourly_eight_days_is_one_request_and_live_path():
    seen: list[tuple[int, int, int]] = []
    path = "/series/KXCPI/markets/KXCPI-29MAR-T0.2/candlesticks"
    kalshi, calls = make_kalshi(routes({path: candle_handler(seen)}))
    kalshi.candles("KXCPI", "KXCPI-29MAR-T0.2", CLOSE_TS - 9 * 86400, CLOSE_TS)
    assert seen == [(CLOSE_TS - 9 * 86400, CLOSE_TS, 60)]
    assert calls[0].url.path == PREFIX + path


def test_candles_historical_flag_and_long_hourly_range():
    seen: list[tuple[int, int, int]] = []
    path = "/historical/markets/KXCPI-28MAR-T0.2/candlesticks"
    kalshi, calls = make_kalshi(routes({path: candle_handler(seen)}))
    start = 1_800_000_000
    kalshi.candles("KXCPI", "KXCPI-28MAR-T0.2", start, start + 5000 * 3600, period=60, historical=True)
    assert len(seen) == 2 and all(c.url.path == PREFIX + path for c in calls)
    assert seen[0][1] - seen[0][0] == (MAX_PERIODS - 1) * 3600


def test_candles_switch_tier_once_on_404():
    hist = "/historical/markets/KXCPI-28MAR-T0.3/candlesticks"
    kalshi, calls = make_kalshi(routes({hist: {"candlesticks": [
        {"end_period_ts": 1_800_003_600, "yes_bid": {"close": "0.1000"}, "yes_ask": {"close": "0.1200"},
         "price": {"close": "0.1100", "previous": "0.1000"}}]}}))
    first = kalshi.candles("KXCPI", "KXCPI-28MAR-T0.3", 1_800_000_000, 1_800_007_200)
    assert first == [Candle(1_800_003_600, 0.10, 0.12, 0.11, 0.10)]
    live = f"{PREFIX}/series/KXCPI/markets/KXCPI-28MAR-T0.3/candlesticks"
    assert [c.url.path for c in calls] == [live, PREFIX + hist]
    kalshi.candles("KXCPI", "KXCPI-28MAR-T0.3", 1_800_000_000, 1_800_007_200)
    assert calls[-1].url.path == PREFIX + hist and len(calls) == 3


def test_candles_other_errors_and_bad_input():
    kalshi, calls = make_kalshi(lambda r: httpx.Response(429, json={"error": "too many requests"}))
    with pytest.raises(FetchError) as info:
        kalshi.candles("KXCPI", "KXCPI-29MAR-T0.2", 1_900_000_000, 1_900_003_600)
    assert info.value.status == 429
    with pytest.raises(ValueError):
        kalshi.candles("KXCPI", "KXCPI-29MAR-T0.2", 1_900_000_000, 1_900_003_600, period=5)
    before = len(calls)
    assert kalshi.candles("KXCPI", "KXCPI-29MAR-T0.2", 1_900_003_600, 1_900_000_000) == []
    assert len(calls) == before


def test_kalshi_requires_a_host_and_strips_slash():
    fetcher = Fetcher(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
                      Budget(10.0, 5))
    with pytest.raises(ValueError):
        Kalshi(fetcher, [])
    assert Kalshi(fetcher, [PRIMARY + "/", " "]).hosts == (PRIMARY,)


def test_no_market_content_in_logs_or_errors(caplog):
    # httpx 自己的请求日志由 cli 调到 WARNING 才输出，这里只看本项目的日志
    caplog.set_level(logging.DEBUG, logger="digest")
    secret_ticker = "KXCPI-29MAR-T0.7"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == PRIMARY_HOST:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(500, text="oops")

    kalshi, _ = make_kalshi(handler)
    with pytest.raises(FetchError) as info:
        kalshi.candles("KXCPI", secret_ticker, 1_900_000_000, 1_900_003_600)
    assert secret_ticker not in str(info.value) and "candlesticks" not in str(info.value)
    text = json.dumps([r.getMessage() for r in caplog.records if r.name.startswith("digest")], ensure_ascii=False)
    assert "不可用" in text  # 换地址的那条日志确实写了
    for leak in (secret_ticker, "KXCPI", "1900000000", "trade-api", "candlesticks"):
        assert leak not in text
