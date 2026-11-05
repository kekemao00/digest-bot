from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from digest.markets import select
from digest.markets.kalshi import parse_event as parse_kalshi_event
from digest.markets.polymarket import Book
from digest.markets.polymarket import parse_event as parse_gamma_event
from digest.markets.schedule import BJT
from digest.markets.settings import KalshiSettings, MarketsSettings, Movers, PolymarketSettings, Releases
from digest.markets.sources import Close, Nowcast
from digest.markets.types import Line

# 全是编造的合约代码、问题、价格和日期（2030 年），字段名照 contracts 里的接口文档
NOW = datetime(2030, 3, 4, 7, 30, tzinfo=BJT)  # 北京时间周一早上
NOW_TS = int(NOW.timestamp())
CLOSE_CPI = "2030-03-06T13:29:00Z"  # 美东 08:29 收盘，08:30 公布 = 北京时间 3 月 6 日 21:30
CLOSE_JOBS = "2030-03-08T13:29:00Z"
CLOSE_FED = "2030-03-20T17:59:00Z"  # 美东 13:59 收盘，14:00 声明 = 北京时间 3 月 21 日 02:00
CPI = "KXCPI-30FEB"
CORE = "KXCPICORE-30FEB"
JOBS = "KXPAYROLLS-30FEB"
FED = "KXFEDDECISION-30MAR"
INX = "KXINXY-30DEC31H1600"
REC = "KXRECSSNBER-31"
CPI_QUOTES = [(0.0, 0.96, 0.98), (0.1, 0.87, 0.89), (0.2, 0.61, 0.63), (0.3, 0.20, 0.22), (0.4, 0.05, 0.07)]
JOB_QUOTES = [(0, 0.89, 0.91), (50000, 0.64, 0.66), (100000, 0.34, 0.36), (150000, 0.09, 0.11)]
FED_LEGS = [
    ("C26", {"Cut": ">25"}, None, 0.02),
    ("C25", {"Cut": "25"}, 0.22, 0.24),
    ("H0", {"Hike": "0"}, 0.70, 0.72),
    ("H25", {"Hike": "25"}, 0.03, 0.05),
    ("H26", {"Hike": ">25"}, None, 0.01),
]
COND_HI = "0x" + "a1" * 32
COND_LO = "0x" + "b2" * 32
COND_MOVE = "0x" + "c3" * 32
COND_SAY = "0x" + "d4" * 32
TOKEN_HI = "7" * 70 + "0000001"
TOKEN_LO = "7" * 70 + "0000002"
TOKEN_MOVE = "7" * 70 + "0000003"
TOKEN_SAY = "7" * 70 + "0000004"
Q_HI = "Will the Example Council approve the widget levy by April?"
Q_LO = "Will the Example Bank of Nowhere raise rates to 9% by May?"
Q_MOVE = "Will the Example Treasury issue a centennial bond in 2030?"
Q_SAY = "Will the Example Chair mention widgets at the briefing?"


def settings(**changes) -> MarketsSettings:
    """测试用的规则：默认值，日程表为空（公布时间从合约收盘日推算），Kalshi 地址是假的。"""
    base = MarketsSettings(kalshi=replace(KalshiSettings(), hosts=("https://api.example-kalshi.test/trade-api/v2",)))
    return replace(base, **changes)


def movers_mode(s: MarketsSettings, mode: str) -> MarketsSettings:
    return replace(s, polymarket=replace(s.polymarket, movers=replace(s.polymarket.movers, mode=mode)))


# ---- 编造的接口数据 ----


def kmarket(ticker, event, close, bid, ask, *, last=None, strike_type="greater", floor=None, cap=None,
             custom=None, oi=3000, status="active") -> dict:
    d = {
        "ticker": ticker,
        "event_ticker": event,
        "status": status,
        "close_time": close,
        "yes_sub_title": "编造",
        "open_interest_fp": f"{oi:.2f}",
        "yes_bid_dollars": f"{bid:.4f}" if bid is not None else "0.0000",
        "yes_ask_dollars": f"{ask:.4f}" if ask is not None else "1.0000",
        "last_price_dollars": f"{last:.4f}" if last is not None else "0.0000",
    }
    if strike_type:
        d["strike_type"] = strike_type
    if floor is not None:
        d["floor_strike"] = floor
    if cap is not None:
        d["cap_strike"] = cap
    if custom:
        d["custom_strike"] = custom
    return d


def kevent(ticker: str, series: str, markets: list[dict], exclusive: bool = False) -> dict:
    return {"event_ticker": ticker, "series_ticker": series, "title": "编造的事件", "mutually_exclusive": exclusive,
            "markets": markets}


def ladder_event(ticker, series, quotes, close, *, oi=3000, last=None) -> dict:
    markets = [kmarket(f"{ticker}-T{k}", ticker, close, b, a, floor=k, oi=oi, last=last) for k, b, a in quotes]
    return kevent(ticker, series, markets)


def cpi_event(**kw) -> dict:
    return ladder_event(CPI, "KXCPI", CPI_QUOTES, CLOSE_CPI, **kw)


def jobs_event(**kw) -> dict:
    return ladder_event(JOBS, "KXPAYROLLS", JOB_QUOTES, CLOSE_JOBS, **kw)


def fed_event(ticker=FED, close=CLOSE_FED, legs=FED_LEGS) -> dict:
    markets = [kmarket(f"{ticker}-{suffix}", ticker, close, b, a, strike_type="custom", custom=custom, oi=4000)
               for suffix, custom, b, a in legs]
    return kevent(ticker, "KXFEDDECISION", markets, exclusive=True)


def inx_event(ticker=INX) -> dict:
    close = "2030-12-31T20:59:00Z"
    markets = [
        kmarket(f"{ticker}-T5000", ticker, close, 0.04, 0.06, strike_type="less", cap=5000),
        kmarket(f"{ticker}-B5100", ticker, close, 0.19, 0.21, strike_type="between", floor=5000, cap=5199.99),
        kmarket(f"{ticker}-B5300", ticker, close, 0.39, 0.41, strike_type="between", floor=5200, cap=5399.99),
        kmarket(f"{ticker}-B5500", ticker, close, 0.24, 0.26, strike_type="between", floor=5400, cap=5599.99),
        kmarket(f"{ticker}-T5600", ticker, close, 0.09, 0.11, strike_type="greater", floor=5600),
    ]
    return kevent(ticker, "KXINXY", markets, exclusive=True)


def rec_event(ticker=REC) -> dict:
    market = kmarket(ticker, ticker, "2032-01-31T13:25:00Z", 0.18, 0.20, strike_type=None, oi=9000)
    return kevent(ticker, "KXRECSSNBER", [market])


def gamma_market(cond, question, token, bid, ask, *, last=None, change=None, liquidity=20000.0,
                 end="2030-04-03T00:00:00Z", **extra) -> dict:
    d = {
        "id": cond[-6:],
        "conditionId": cond,
        "question": question,
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{token}", "{token[:-1]}9"]',
        "bestBid": bid,
        "bestAsk": ask,
        "lastTradePrice": last if last is not None else round((bid + ask) / 2, 4),
        "liquidityNum": liquidity,
        "volume24hr": 1234.5,
        "endDate": end,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "description": "编造的规则",
        "resolutionSource": "",
    }
    if change is not None:
        d["oneDayPriceChange"] = change
    d.update(extra)
    return d


def gamma_event(event_id: str, markets: list[dict], tags=("finance",)) -> dict:
    return {"id": event_id, "title": "编造的事件 " + event_id, "endDate": "2030-06-30T00:00:00Z",
            "tags": [{"id": str(900 + i), "slug": t} for i, t in enumerate(tags)], "markets": markets}


def gamma_events() -> list[dict]:
    """四个事件：一个很高（91%），一个很低（6%），一个一天涨了 12 个百分点，一个“会不会提到”（要排除）。"""
    return [
        gamma_event("8101", [gamma_market(COND_HI, Q_HI, TOKEN_HI, 0.90, 0.92, liquidity=40000.0)]),
        gamma_event("8102", [gamma_market(COND_LO, Q_LO, TOKEN_LO, 0.05, 0.07, end="2030-04-23T00:00:00Z")]),
        gamma_event("8103", [gamma_market(COND_MOVE, Q_MOVE, TOKEN_MOVE, 0.60, 0.62, change=0.12,
                                          liquidity=80000.0, end="2030-05-03T00:00:00Z")]),
        gamma_event("8104", [gamma_market(COND_SAY, Q_SAY, TOKEN_SAY, 0.90, 0.91, change=0.2, liquidity=90000.0)]),
    ]


def pmarkets(events=None) -> list:
    return [m for e in (events or gamma_events()) for m in parse_gamma_event(e)]


def kevents(*events) -> list:
    return [parse_kalshi_event(e) for e in events]


# ---- 关键数据 ----


def rule(series: str):
    return next(r for r in settings().kalshi.key_data if r.series == series)


def test_key_events_window_morning_and_evening():
    s = settings()
    events = kevents(cpi_event(), jobs_event())
    assert [e.event_ticker for e, _ in select.key_events(rule("KXCPI"), events[:1], s, NOW, "morning")] == [CPI]
    # 晚间版只看 12 小时内：周一晚上看不到周三的 CPI
    evening = datetime(2030, 3, 4, 19, 30, tzinfo=BJT)
    assert select.key_events(rule("KXCPI"), events[:1], s, evening, "evening") == []
    tonight = datetime(2030, 3, 6, 19, 30, tzinfo=BJT)
    (event, release), = select.key_events(rule("KXCPI"), events[:1], s, tonight, "evening")
    assert release == datetime(2030, 3, 6, 13, 30, tzinfo=timezone.utc)
    # 已经公布了的不算
    assert select.key_events(rule("KXCPI"), events[:1], s, datetime(2030, 3, 6, 22, 0, tzinfo=BJT), "evening") == []


def test_key_events_uses_release_table_and_skips_mismatch():
    table = replace(Releases(), cpi={(2030, 2): date(2030, 3, 6)})
    s = settings(releases=table)
    (_, release), = select.key_events(rule("KXCPI"), kevents(cpi_event()), s, NOW, "morning")
    assert release == datetime(2030, 3, 6, 13, 30, tzinfo=timezone.utc)
    wrong = settings(releases=replace(Releases(), cpi={(2030, 2): date(2030, 3, 7)}))
    assert select.key_events(rule("KXCPI"), kevents(cpi_event()), wrong, NOW, "morning") == []


def test_next_fomc_shown_in_the_morning_even_beyond_seven_days():
    s = settings()
    later = fed_event("KXFEDDECISION-30APR", "2030-04-30T17:59:00Z")
    found = select.key_events(rule("KXFEDDECISION"), kevents(later, fed_event()), s, NOW, "morning")
    assert [e.event_ticker for e, _ in found] == [FED]
    assert select.key_events(rule("KXFEDDECISION"), kevents(fed_event()), s, NOW, "evening") == []
    # 其他系列没有这个例外
    far_cpi = ladder_event("KXCPI-30MAR", "KXCPI", CPI_QUOTES, "2030-04-10T12:29:00Z")
    assert select.key_events(rule("KXCPI"), kevents(far_cpi), s, NOW, "morning") == []


def test_ladder_line_text_rows_and_nowcast():
    s = settings()
    (event,) = kevents(cpi_event())
    release = datetime(2030, 3, 6, 13, 30, tzinfo=timezone.utc)
    line = select.key_line(rule("KXCPI"), event, release, NOW, "morning", s, nowcast=0.28, sub="回测还没跑")
    assert line.text == ("· 3月6日 周三 21:30 美国2月CPI 环比：中位数 0.3%（一半把握 0.2% 到 0.3%）"
                         "；克利夫兰联储模型 0.28%")
    assert line.sub == "回测还没跑"
    assert line.section == "key"
    # 收盘（08:29）比公布（08:30）早，过期时间取更早的那个
    assert line.expires == datetime(2030, 3, 6, 13, 29, tzinfo=timezone.utc)
    (row,) = line.rows
    assert row["type"] == "shown" and row["venue"] == "kalshi" and row["key"] == CPI
    assert row["series"] == "KXCPI" and row["period"] == "2030-02" and row["kind"] == "ladder"
    assert row["expires"] == "2030-03-06T13:29:00Z" and row["lead"] == 2
    dist = row["dist"]
    assert dist["ks"] == [0.0, 0.1, 0.2, 0.3, 0.4] and dist["grid"] == 0.1
    assert dist["median"] == {"value": 0.3, "open_side": None}
    assert dist["q25"] == {"value": 0.2, "open_side": None} and dist["q75"] == {"value": 0.3, "open_side": None}
    assert dist["mass50"] == pytest.approx(0.79 - 0.12)
    assert [s_["ticker"] for s_ in row["strikes"]][0] == f"{CPI}-T0.0"


def test_payroll_line_uses_ten_thousands():
    (event,) = kevents(jobs_event())
    release = datetime(2030, 3, 8, 13, 30, tzinfo=timezone.utc)
    line = select.key_line(rule("KXPAYROLLS"), event, release, NOW, "morning", settings())
    assert line.text == "· 3月8日 周五 21:30 美国2月非农新增就业：中位数 +7.5 万（一半把握 +3 万 到 +12 万）"
    assert line.sub is None


def test_open_tail_and_last_trade_flag():
    # 价差太大的合约用成交价，要标（成交价）；最低一档就过半时写“不高于”
    quotes = [(0.1, 0.30, 0.50), (0.2, 0.20, 0.22), (0.3, 0.05, 0.07)]
    event = parse_kalshi_event(ladder_event(CPI, "KXCPI", quotes, CLOSE_CPI, oi=3000, last=0.40))
    release = datetime(2030, 3, 6, 13, 30, tzinfo=timezone.utc)
    line = select.key_line(rule("KXCPI"), event, release, NOW, "morning", settings())
    assert "中位数 不高于 0.1%" in line.text
    assert line.text.endswith("（成交价）")
    assert line.rows[0]["dist"]["median"] == {"value": 0.1, "open_side": "below"}


def test_one_sided_ladder_tails_use_the_book_not_stale_trades():
    # 最低一档只有买单（0.98）、最高一档只有卖单（0.02），成交价是很久以前的（0.86、0.13）：
    # 按盘口补全成 0.99 和 0.01，不标（成交价），也不让旧成交价把中间的概率拉偏
    markets = [
        kmarket(f"{CPI}-T0.0", CPI, CLOSE_CPI, 0.98, None, floor=0.0, last=0.86),
        kmarket(f"{CPI}-T0.1", CPI, CLOSE_CPI, 0.83, 0.85, floor=0.1),
        kmarket(f"{CPI}-T0.2", CPI, CLOSE_CPI, 0.47, 0.49, floor=0.2),
        kmarket(f"{CPI}-T0.3", CPI, CLOSE_CPI, 0.15, 0.17, floor=0.3),
        kmarket(f"{CPI}-T0.4", CPI, CLOSE_CPI, None, 0.02, floor=0.4, last=0.13),
    ]
    event = parse_kalshi_event(kevent(CPI, "KXCPI", markets))
    release = datetime(2030, 3, 6, 13, 30, tzinfo=timezone.utc)
    line = select.key_line(rule("KXCPI"), event, release, NOW, "morning", settings())
    assert line.text == "· 3月6日 周三 21:30 美国2月CPI 环比：中位数 0.2%（一半把握 0.2% 到 0.3%）"
    dist = line.rows[0]["dist"]
    assert dist["used_last"] is False
    assert dist["s"] == pytest.approx([0.99, 0.84, 0.48, 0.16, 0.01])
    # 账本里记的是原始报价
    assert line.rows[0]["strikes"][-1] == {"ticker": f"{CPI}-T0.4", "k": 0.4, "bid": None, "ask": 0.02, "last": 0.13}


def test_ladder_with_a_missing_middle_strike_is_skipped():
    quotes = [(0.0, 0.94, 0.96), (0.1, 0.70, 0.72), (0.3, 0.20, 0.22), (0.4, 0.05, 0.07)]  # 没有 0.2 那一档
    event = parse_kalshi_event(ladder_event(CPI, "KXCPI", quotes, CLOSE_CPI))
    release = datetime(2030, 3, 6, 13, 30, tzinfo=timezone.utc)
    assert select.key_line(rule("KXCPI"), event, release, NOW, "morning", settings()) is None


def test_ladder_quality_gates():
    release = datetime(2030, 3, 6, 13, 30, tzinfo=timezone.utc)
    thin = parse_kalshi_event(cpi_event(oi=500))  # 5 个合约共 2500 张，不到 5000
    assert select.key_line(rule("KXCPI"), thin, release, NOW, "morning", settings()) is None
    closed = parse_kalshi_event(ladder_event(CPI, "KXCPI", CPI_QUOTES, CLOSE_CPI))
    closed = replace(closed, markets=tuple(replace(m, status="closed") for m in closed.markets))
    assert select.key_line(rule("KXCPI"), closed, release, NOW, "morning", settings()) is None
    one = parse_kalshi_event(ladder_event(CPI, "KXCPI", CPI_QUOTES[:1], CLOSE_CPI, oi=9000))
    assert select.key_line(rule("KXCPI"), one, release, NOW, "morning", settings()) is None


def test_fed_line_normalises_and_shows_second_outcome():
    (event,) = kevents(fed_event())
    release = datetime(2030, 3, 20, 18, 0, tzinfo=timezone.utc)
    line = select.key_line(rule("KXFEDDECISION"), event, release, NOW, "morning", settings(), sub="x")
    assert line.text == "· 3月21日 周四 02:00 美联储议息：维持不变 较可能（71%），降息 25 个基点 23%"
    (row,) = line.rows
    assert row["key"] == f"{FED}-H0" and row["event"] == FED and row["p"] == 71 and row["kind"] == "fed"
    assert row["period"] == "2030-03"
    assert sum(o["p"] for o in row["outcomes"]) == pytest.approx(1.0)
    # 第二个结果不到 5% 时不写
    legs = [("C25", {"Cut": "25"}, 0.02, 0.04), ("H0", {"Hike": "0"}, 0.95, 0.97), ("H25", {"Hike": "25"}, 0.01, 0.02)]
    (lopsided,) = kevents(fed_event(legs=legs))
    text = select.key_line(rule("KXFEDDECISION"), lopsided, release, NOW, "morning", settings()).text
    assert text.endswith("维持不变 几乎确定（96%）")


def test_fed_line_skipped_when_quotes_do_not_add_up():
    legs = [("C25", {"Cut": "25"}, 0.50, 0.52), ("H0", {"Hike": "0"}, 0.70, 0.72)]
    (event,) = kevents(fed_event(legs=legs))
    release = datetime(2030, 3, 20, 18, 0, tzinfo=timezone.utc)
    assert select.key_line(rule("KXFEDDECISION"), event, release, NOW, "morning", settings()) is None


def test_replay_sub_and_lead():
    assert select.replay_sub("过去 5 次同一时刻：x", True, 3) == "过去 5 次同一时刻：x"
    assert select.replay_sub(None, False, 3) == "回测还没跑"
    assert select.replay_sub("过去 5 次", True, 8) == "回测只算到提前 7 天"
    assert select.replay_sub(None, True, 2) == "回测里这个时刻的记录不够"
    release = datetime(2030, 3, 6, 13, 30, tzinfo=timezone.utc)
    assert select.lead_of(NOW, release, "morning") == 2
    assert select.lead_of(NOW, release, "evening") == 0


def test_nowcast_value_only_for_cpi_and_fresh():
    casts = {(2030, 2): Nowcast(cpi=0.28, core=0.24, as_of=date(2030, 3, 1))}
    assert select.nowcast_value(rule("KXCPI"), casts, (2030, 2), NOW, 7) == 0.28
    assert select.nowcast_value(rule("KXCPICORE"), casts, (2030, 2), NOW, 7) == 0.24
    assert select.nowcast_value(rule("KXPAYROLLS"), casts, (2030, 2), NOW, 7) is None
    assert select.nowcast_value(rule("KXCPI"), casts, (2030, 1), NOW, 7) is None
    old = {(2030, 2): Nowcast(cpi=0.28, core=None, as_of=date(2030, 2, 20))}
    assert select.nowcast_value(rule("KXCPI"), old, (2030, 2), NOW, 7) is None
    assert select.nowcast_value(rule("KXCPICORE"), old, (2030, 2), datetime(2030, 2, 21, tzinfo=BJT), 7) is None


# ---- 每周一看 ----


def weekly_rule(series: str):
    return next(r for r in settings().kalshi.weekly if r.series == series)


def test_weekly_events_pick_years():
    # 别的系列混进来也不选
    events = kevents(inx_event(), inx_event("KXINXY-31DEC31H1600"), rec_event(), rec_event("KXRECSSNBER-30"))
    assert [e.event_ticker for e in select.weekly_events(weekly_rule("KXINXY"), events, NOW)] == [INX]
    december = datetime(2030, 12, 2, 7, 30, tzinfo=BJT)
    assert [e.event_ticker for e in select.weekly_events(weekly_rule("KXINXY"), events, december)] == [
        INX, "KXINXY-31DEC31H1600"]
    assert [e.event_ticker for e in select.weekly_events(weekly_rule("KXRECSSNBER"), events, NOW)] == [REC]


def test_weekly_bucket_and_binary_lines():
    s = settings()
    (inx,) = kevents(inx_event())
    line = select.weekly_line(weekly_rule("KXINXY"), inx, NOW, s)
    assert line.text == "· 标普500 今年年底：中位数落在 5200–5400 点（一半把握 5000 到 5600 点）"
    (row,) = line.rows
    assert row["key"] == INX and row["section"] == "weekly" and row["kind"] == "buckets"
    assert row["dist"]["q25"] == {"value": 5000, "open_side": None}
    assert row["dist"]["q75"] == {"value": 5599.99, "open_side": None}
    assert row["dist"]["mass50"] == pytest.approx(0.85)
    (rec,) = kevents(rec_event())
    line = select.weekly_line(weekly_rule("KXRECSSNBER"), rec, NOW, s)
    # 合约规则是“前一年或这一年里连续两个季度负增长”：-31 管 2030 和 2031 两年
    assert line.text == "· 2030 到 2031 年美国实际 GDP 连续两季负增长：很不可能（19%）"
    assert line.rows[0]["p"] == 19 and line.rows[0]["key"] == REC
    assert line.rows[0]["question"] == "2030 到 2031 年美国实际 GDP 连续两季负增长"


# ---- 定价一边倒 ----


def test_lopsided_picks_filters_and_order():
    s = settings()
    picks = select.lopsided_picks(pmarkets(), s, NOW)
    # “会不会提到”被排除；涨了 12 个百分点的那个价格不在两个区间里；按流动性排
    assert [m.condition_id for m in picks] == [COND_HI, COND_LO]


def test_lopsided_one_market_per_event_most_lopsided():
    markets = [
        gamma_market("0x" + "e1" * 32, "Option one of an invented race?", "6" * 76 + "1", 0.86, 0.88),
        gamma_market("0x" + "e2" * 32, "Option two of an invented race?", "6" * 76 + "2", 0.04, 0.05),
        gamma_market("0x" + "e3" * 32, "Option three of an invented race?", "6" * 76 + "3", 0.98, 0.99),
    ]
    (pick,) = select.lopsided_picks(pmarkets([gamma_event("8200", markets)]), settings(), NOW)
    # 0.985 太接近 1，不在区间里；剩下两个里离 50% 最远的是 4.5%
    assert pick.condition_id == "0x" + "e2" * 32


@pytest.mark.parametrize(
    "change",
    [
        {"end": "2030-03-05T00:00:00Z"},  # 不到 2 天就结算
        {"end": "2030-06-20T00:00:00Z"},  # 超过 100 天
        {"liquidity": 9000.0},
        {"bid": 0.88, "ask": 0.92},  # 价差 4 个百分点
        {"umaResolutionStatus": "proposed"},
        {"closed": True},
        {"acceptingOrders": False},
        {"question": "Will the Example Chair say widgets twice?"},
    ],
)
def test_lopsided_prefilter_rejects(change):
    params = {"bid": 0.90, "ask": 0.92, "liquidity": 40000.0, "end": "2030-04-03T00:00:00Z"}
    extra = {k: v for k, v in change.items() if k not in ("bid", "ask", "liquidity", "end", "question")}
    params.update({k: v for k, v in change.items() if k in params})
    question = change.get("question", Q_HI)
    market = gamma_market(COND_HI, question, TOKEN_HI, params["bid"], params["ask"], liquidity=params["liquidity"],
                          end=params["end"], **extra)
    assert select.lopsided_picks(pmarkets([gamma_event("8300", [market])]), settings(), NOW) == []


def test_only_yes_no_markets_are_candidates():
    # 结果是两个队名的市场：第 0 个 token 的价格不是“问题成立”的概率，定价一边倒和价格变化都不用
    teams = gamma_market(COND_HI, Q_HI, TOKEN_HI, 0.90, 0.92, liquidity=90000.0, change=0.2,
                         outcomes='["Team Blue", "Team Red"]')
    lower = gamma_market(COND_LO, Q_LO, TOKEN_LO, 0.05, 0.07, outcomes='["yes", "no"]')  # 大小写不要紧
    three = gamma_market(COND_SAY, Q_HI, TOKEN_SAY, 0.90, 0.92, outcomes='["Yes", "No", "Maybe"]')
    markets = pmarkets([gamma_event("8401", [teams]), gamma_event("8402", [lower]), gamma_event("8403", [three])])
    assert [m.condition_id for m in select.lopsided_picks(markets, settings(), NOW)] == [COND_LO]
    assert select.mover_candidates(markets, settings(), NOW) == []
    # 没有结果名单（解析不出来）的也不用
    bare = gamma_market(COND_MOVE, Q_MOVE, TOKEN_MOVE, 0.90, 0.92, outcomes=None)
    assert select.lopsided_picks(pmarkets([gamma_event("8404", [bare])]), settings(), NOW) == []


def test_verify_and_lopsided_line_with_longshot_note():
    s = settings()
    rules = s.polymarket.lopsided
    assert select.verify_lopsided(Book(((0.05, 100.0),), ((0.07, 100.0),)), rules) == pytest.approx(0.06)
    assert select.verify_lopsided(Book(((0.03, 100.0),), ((0.08, 100.0),)), rules) is None  # 价差太大
    assert select.verify_lopsided(Book(((0.40, 100.0),), ((0.41, 100.0),)), rules) is None  # 不再一边倒
    assert select.verify_lopsided(Book((), ((0.07, 100.0),)), rules) is None
    lo = next(m for m in pmarkets() if m.condition_id == COND_LO)
    book = Book(((0.05, 100.0),), ((0.07, 100.0),))
    line = select.lopsided_line(lo, book, 0.06, "示例银行五月前把利率加到 9%", s)
    assert line.text == "· 示例银行五月前把利率加到 9%：很不可能（6%）· 4月23日结算；便宜合约历来被高估"
    (row,) = line.rows
    assert row["key"] == f"{COND_LO}:{TOKEN_LO}" and row["p"] == 6 and row["side"] == "lo"
    assert row["venue"] == "polymarket" and row["section"] == "lopsided"
    assert line.expires == datetime(2030, 4, 23, tzinfo=timezone.utc)
    hi = next(m for m in pmarkets() if m.condition_id == COND_HI)
    text = select.lopsided_line(hi, Book(((0.90, 1.0),), ((0.92, 1.0),)), 0.91, None, s).text
    # 没有译文时用英文原文
    assert text == f"· {Q_HI}：很可能（91%）· 4月3日结算"


def test_question_text_escapes_and_truncates():
    text = select.question_text(None, "Will [x] *bold* <b> be | fine " + "z" * 80)
    assert "[" not in text and "*" not in text and "<" not in text and "|" not in text
    assert len(text) == select.QUESTION_CHARS and text.endswith("…")


# ---- 过去一天的变化 ----


def test_mover_prefilter():
    s = settings()
    (candidate,) = select.mover_candidates(pmarkets(), s, NOW)
    assert candidate.condition_id == COND_MOVE
    few = replace(s, polymarket=replace(s.polymarket, movers=replace(Movers(), max_candidates=0)))
    assert select.mover_candidates(pmarkets(), few, NOW) == []
    small = [gamma_event("8400", [gamma_market(COND_MOVE, Q_MOVE, TOKEN_MOVE, 0.60, 0.62, change=0.03,
                                               liquidity=80000.0)])]
    assert select.mover_candidates(pmarkets(small), s, NOW) == []


def history(p24=0.50, recent=(0.58, 0.60), terminal=0.61):
    """整点价格：24 小时前及更早是 p24，之后一路到 recent，最后一个不在整点上的最新观测。"""
    base = NOW_TS // 3600 * 3600
    points = []
    for k in range(25, -1, -1):
        ts = base - k * 3600
        points.append((ts, p24 if ts <= NOW_TS - 86400 else 0.55))
    points[-2:] = [(points[-2][0], recent[0]), (points[-1][0], recent[1])]
    points.append((NOW_TS - 100, terminal))
    return points


def test_price_change_persistence():
    p24, change, persist = select.price_change(history(), 0.61, NOW_TS, 2)
    assert p24 == 0.50 and change == pytest.approx(0.11) and persist is True
    # 最近一个整点掉回去了，方向没维持住
    assert select.price_change(history(recent=(0.58, 0.49)), 0.61, NOW_TS, 2)[2] is False
    # 最后那个不在整点上的观测不算：它往回掉也不影响
    assert select.price_change(history(terminal=0.30), 0.61, NOW_TS, 2)[2] is True
    # 没有 24 小时前的点
    late = [(ts, p) for ts, p in history() if ts > NOW_TS - 86400]
    assert select.price_change(late, 0.61, NOW_TS, 2) == (None, None, False)
    assert select.price_change(history(), None, NOW_TS, 2) == (None, None, None)


def passing_check(**changes) -> select.MoverCheck:
    market = next(m for m in pmarkets() if m.condition_id == COND_MOVE)
    values = dict(mid=0.61, spread=0.02, p24=0.50, change=0.11, persist=True, depth_up=12400.0, depth_down=8000.0,
                  wallets=6, top_share=0.3, clean=True, news=2, zh="示例财政部今年会发行百年期国债吗")
    values.update(changes)
    return select.MoverCheck(market, **values)


GATE_FAILURES = {
    "change": {"change": 0.04},
    "spread": {"spread": 0.06},
    "persist": {"persist": False},
    "depth": {"depth_down": 4000.0},
    "wallets": {"wallets": 4},
    "top_share": {"top_share": 0.6},
    "clean": {"clean": False},
    "news": {"news": None},
}


def test_all_gates_pass():
    gates = select.mover_gates(passing_check(), settings())
    assert set(gates) == set(select.GATES) and all(gates.values())


@pytest.mark.parametrize("gate", sorted(GATE_FAILURES))
def test_each_gate_can_fail_alone(gate):
    gates = select.mover_gates(passing_check(**GATE_FAILURES[gate]), settings())
    assert [name for name, ok in gates.items() if not ok] == [gate]
    assert select.pick_mover([passing_check(**GATE_FAILURES[gate])], settings()) is None


def test_wallet_gates_need_a_whole_day_of_trades():
    # 成交只翻到最近几个小时（翻满页数还没到 24 小时前）：钱包数和集中度说明不了一整天，这两项算没通过
    check = passing_check(trades_complete=False)
    gates = select.mover_gates(check, settings())
    assert [name for name, ok in gates.items() if not ok] == ["wallets", "top_share"]
    row = select.candidate_row(check, settings(), shown=False)
    assert row["values"]["trades_complete"] is False and row["values"]["wallets"] == 6
    assert all(select.mover_gates(passing_check(trades_complete=True), settings()).values())


def test_missing_values_fail_gates():
    market = next(m for m in pmarkets() if m.condition_id == COND_MOVE)
    assert not any(select.mover_gates(select.MoverCheck(market), settings()).values())


def test_pick_mover_takes_largest_change():
    small, big = passing_check(change=0.06), passing_check(change=-0.09)
    assert select.pick_mover([small, big], settings()) is big


def test_mover_line_show_and_preview():
    s = settings()
    check = passing_check()
    line = select.mover_line(check, "morning", s)
    assert line.text == "· 示例财政部今年会发行百年期国债吗：61%（一天 +11 个百分点）"
    assert line.sub == "相关新闻：今天第 2 条"
    assert line.rows[0]["key"] == f"{COND_MOVE}:{TOKEN_MOVE}" and line.rows[0]["section"] == "mover"
    assert select.mover_line(check, "evening", s).sub == "相关新闻：今晚第 2 条"
    shadow = select.mover_line(passing_check(news=None, wallets=3), "morning", s, preview=True)
    assert shadow.text.startswith("· 【后台观察】示例财政部")
    assert shadow.sub == "没通过：钱包数、对应新闻"
    shown = select.mover_line(check, "morning", movers_mode(s, "show"), preview=True)
    assert "【后台观察】" not in shown.text and shown.sub == "各项核实都通过了；相关新闻：今天第 2 条"
    down = select.mover_line(passing_check(change=-0.064, zh=None), "morning", s)
    assert down.text == f"· {Q_MOVE}：61%（一天 −6 个百分点）"
    assert select.mover_line(passing_check(mid=None), "morning", s) is None


def test_candidate_row_records_every_gate():
    row = select.candidate_row(passing_check(wallets=3), settings(), shown=False)
    assert row["type"] == "candidate" and row["shown"] is False and row["movers_mode"] == "shadow"
    assert row["gates"]["wallets"] is False and row["gates"]["change"] is True
    assert row["values"]["depth_down"] == 8000.0 and row["values"]["news"] == 2
    assert row["prefilter"]["change"] == 0.12


def test_change_points():
    assert select.change_points(0.115) == "+12"
    assert select.change_points(-0.065) == "−7"
    assert select.change_points(0.004) == "0"


# ---- 波动率 ----


def test_vix_lines():
    s = settings()
    vix = Close(date(2030, 3, 1), 15.87)
    (line,) = select.vix_lines(vix, None, NOW, s)
    assert line.text == "· VIX 15.87（3月1日收盘，长期平均约 20）：标普500 一天的涨跌约有三分之二的机会在 ±1.0% 以内"
    lines = select.vix_lines(vix, Close(date(2030, 3, 1), 17.20), NOW, s)
    assert lines[1].text == "· 未来 9 天的预期波动（17.20）高于未来 30 天，市场觉得近期风险更大"
    lines = select.vix_lines(vix, Close(date(2030, 3, 1), 14.50), NOW, s)
    assert "低于未来 30 天" in lines[1].text
    assert len(select.vix_lines(vix, Close(date(2030, 3, 1), 16.5), NOW, s)) == 1
    # VIX9D 不是同一天的收盘就不比较
    assert len(select.vix_lines(vix, Close(date(2030, 2, 28), 18.0), NOW, s)) == 1
    # 超过 5 天没更新就不放
    assert select.vix_lines(Close(date(2030, 2, 25), 15.0), None, NOW, s) == []
    assert select.vix_lines(None, None, NOW, s) == []


def test_lines_are_line_objects():
    (event,) = kevents(cpi_event())
    release = datetime(2030, 3, 6, 13, 30, tzinfo=timezone.utc)
    line = select.key_line(rule("KXCPI"), event, release, NOW, "morning", settings())
    assert isinstance(line, Line) and line.text.startswith("· ")
    assert select.iso(datetime(2030, 3, 4, 7, 30, tzinfo=BJT)) == "2030-03-03T23:30:00Z"
    assert select.iso(datetime(2030, 3, 4, 7, 30)) == "2030-03-04T07:30:00Z"
    assert select.iso(None) is None


def test_settings_helper_has_empty_tables():
    s = settings()
    assert s.releases.cpi == {} and s.polymarket.movers.mode == "shadow"
    assert isinstance(s.polymarket, PolymarketSettings) and s.kalshi.hosts[0].startswith("https://api.example-kalshi")
    assert NOW - timedelta(hours=1) < datetime(2030, 3, 4, 7, 0, tzinfo=BJT) + timedelta(hours=1)
