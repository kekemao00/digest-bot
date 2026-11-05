from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from digest.markets.net import Budget, BudgetExceeded, FetchError, Fetcher
from digest.markets.polymarket import (
    EVENTS_PAGE,
    Book,
    PMarket,
    Polymarket,
    Resolution,
    Trade,
    excluded,
    parse_event,
    wallet_stats,
)
from digest.markets.settings import PolymarketSettings

# 全是编造的问题、编号、价格和钱包，字段名和格式照接口文档
COND_A = "0x" + "a1" * 32
COND_B = "0x" + "b2" * 32
COND_C = "0x" + "c3" * 32
TOKEN_YES = "9" * 40 + "1" * 36 + "7"
TOKEN_NO = "9" * 40 + "2" * 36 + "3"
TOKEN_V2_YES = "8" * 75 + "10"
TOKEN_V2_NO = "8" * 75 + "11"
QUESTION = "Will the Example Board cut its policy rate by June 2031?"
WALLETS = [f"0x{i:040x}" for i in range(1, 9)]
GAMMA_HOST = "gamma-api.polymarket.com"
CLOB_HOST = "clob.polymarket.com"
DATA_HOST = "data-api.polymarket.com"


class FakeClock:
    """假的单调时钟：sleep 不真睡，只把时间往前拨。"""

    def __init__(self) -> None:
        self.now = 7000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_poly(handler, *, requests=50):
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(record))
    fetcher = Fetcher(client, Budget(600.0, requests, clock=clock), sleep=clock.sleep, jitter=lambda: 0.0)
    return Polymarket(fetcher), calls


def routes(table: dict):
    """按 (主机, 路径) 分发；值是响应、JSON、request -> 响应的函数，或依次返回的列表。没有的路径返回 404。"""
    queues = {key: list(value) for key, value in table.items() if isinstance(value, list)}

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.url.host, request.url.path)
        if key in queues:
            queue = queues[key]
            item = queue.pop(0) if len(queue) > 1 else queue[0]
        else:
            item = table.get(key)
        if item is None:
            return httpx.Response(404, json={"error": "not found"})
        if callable(item):
            item = item(request)
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    return handler


def gamma_market(condition=COND_A, **fields) -> dict:
    d = {
        "id": "5550001",
        "question": QUESTION,
        "conditionId": condition,
        "slug": "will-the-example-board-cut-by-june-2031",
        "description": "Resolves Yes if the Example Board announces a cut.",
        "resolutionSource": "https://example.invalid/board",
        "endDate": "2031-06-30T12:00:00Z",
        "outcomes": "[\"Yes\", \"No\"]",
        "outcomePrices": "[\"0.415\", \"0.585\"]",
        "clobTokenIds": json.dumps([TOKEN_YES, TOKEN_NO]),
        "version": "v1",
        "active": True, "closed": False, "acceptingOrders": True, "archived": False,
        "negRisk": False,
        "groupItemTitle": "June",
        "umaResolutionStatuses": "[]",
        "bestBid": 0.41, "bestAsk": 0.42, "spread": 0.01, "lastTradePrice": 0.42,
        "oneDayPriceChange": -0.065,
        "volume": "765432.1", "volumeNum": 765432.1, "volume24hr": 43210.5,
        "liquidity": "65432.25", "liquidityNum": 65432.25,
    }
    d.update(fields)
    return d


def gamma_event(markets, **fields) -> dict:
    d = {
        "id": "424242",
        "slug": "example-board-decision-2031",
        "title": "Example Board decision in 2031",
        "endDate": "2031-07-01T00:00:00Z",
        "active": True, "closed": False,
        "tags": [{"id": "120", "label": "Finance", "slug": "Finance"}, {"id": "777001", "label": "Made Up", "slug": "made-up"}],
        "series": [],
        "markets": markets,
    }
    d.update(fields)
    return d


def pm(question=QUESTION, tags=(), event_title="Example Board decision in 2031", group_title="") -> PMarket:
    return PMarket(
        id="1", condition_id=COND_A, question=question, outcomes=("Yes", "No"), token_ids=(TOKEN_YES, TOKEN_NO),
        best_bid=0.4, best_ask=0.41, last=0.41, one_day_change=None, liquidity=1.0, volume_24h=1.0, end=None,
        closed=False, active=True, uma_status="", group_title=group_title, event_id="2", event_title=event_title,
        tags=tuple(tags), description_hash="",
    )


def trade_row(wallet, side, size, ts, outcome_index=0, price=0.5) -> dict:
    return {
        "proxy_wallet": wallet, "side": side, "token_id": TOKEN_YES, "condition_id": COND_A, "size": size,
        "price": price, "timestamp": ts, "title": QUESTION, "slug": "s", "icon": "", "event_slug": "e",
        "outcome": "Yes", "outcome_index": outcome_index, "name": "Fake Person", "pseudonym": "Fake-Pseudonym",
        "bio": "fake bio", "profile_image": "", "profile_image_optimized": "", "transaction_hash": "0x" + "cd" * 32,
    }


def page(rows, next_cursor=None, has_more=None) -> dict:
    more = next_cursor is not None if has_more is None else has_more
    return {"data": rows, "pagination": {"limit": 1000, "offset": 0, "has_more": more, "next_cursor": next_cursor}}


# ---------- Gamma 事件解析 ----------


def test_parse_event_v1_json_string_fields():
    [m] = parse_event(gamma_event([gamma_market()]))
    assert m.id == "5550001" and m.condition_id == COND_A and m.question == QUESTION
    assert m.outcomes == ("Yes", "No")
    assert m.token_ids == (TOKEN_YES, TOKEN_NO)  # 字符串，不转成数字
    assert (m.best_bid, m.best_ask, m.last) == (0.41, 0.42, 0.42)
    assert m.one_day_change == -0.065
    assert m.liquidity == 65432.25 and m.volume_24h == 43210.5
    assert m.end == datetime(2031, 6, 30, 12, 0, tzinfo=timezone.utc)
    assert m.closed is False and m.active is True
    assert m.uma_status == ""
    assert m.group_title == "June"
    assert m.event_id == "424242" and m.event_title == "Example Board decision in 2031"
    assert m.tags == ("finance", "made-up")
    rules = "Resolves Yes if the Example Board announces a cut." + "https://example.invalid/board"
    assert m.description_hash == hashlib.sha256(rules.encode()).hexdigest()


def test_parse_event_v2_uses_position_ids_and_resolution_status():
    raw = gamma_market(
        COND_B, version="v2", clobTokenIds=None, positionIds=[TOKEN_V2_YES, TOKEN_V2_NO], resolutionStatus="Active",
    )
    [m] = parse_event(gamma_event([raw]))
    assert m.token_ids == (TOKEN_V2_YES, TOKEN_V2_NO)
    assert m.uma_status == "active"


def test_parse_event_token_id_fallback_and_rejects():
    # v1 却只有 positionIds：退回去用它
    [m] = parse_event(gamma_event([gamma_market(clobTokenIds=None, positionIds=[TOKEN_YES, TOKEN_NO])]))
    assert m.token_ids == (TOKEN_YES, TOKEN_NO)
    # v2 的 positionIds 缺了，用 clobTokenIds
    [m] = parse_event(gamma_event([gamma_market(version="v2", positionIds=None)]))
    assert m.token_ids == (TOKEN_YES, TOKEN_NO)
    # 个数和结果对不上、有不是数字的 id、都没有：不用
    for fields in (
        {"clobTokenIds": json.dumps([TOKEN_YES])},
        {"clobTokenIds": json.dumps([TOKEN_YES, "abc"])},
        {"clobTokenIds": json.dumps([TOKEN_YES, 0.5])},
        {"clobTokenIds": None},
        {"clobTokenIds": "not json"},
    ):
        [m] = parse_event(gamma_event([gamma_market(**fields)]))
        assert m.token_ids == (), fields


def test_parse_event_skips_unusable_markets():
    markets = [
        gamma_market(COND_A),
        gamma_market(COND_B, version="v3"),  # 不支持的协议版本
        gamma_market(COND_C, negRiskOther=True),  # 占位的“其他”
        gamma_market(""),  # 缺 conditionId
        "not an object",
    ]
    assert [m.condition_id for m in parse_event(gamma_event(markets))] == [COND_A]
    assert parse_event("nope") == [] and parse_event(gamma_event(None)) == []


def test_parse_event_missing_numbers_mean_unknown():
    raw = gamma_market()
    for key in ("bestBid", "bestAsk", "lastTradePrice", "oneDayPriceChange", "liquidityNum", "volume24hr"):
        del raw[key]
    raw["liquidity"] = "1234.5"
    [m] = parse_event(gamma_event([raw]))
    assert (m.best_bid, m.best_ask, m.last, m.one_day_change) == (None, None, None, None)
    assert m.liquidity == 1234.5  # 字符串的 liquidity 也认
    assert m.volume_24h == 0.0
    raw = gamma_market(bestBid=0, bestAsk=1, liquidityNum="bad", liquidity=None, liquidityClob=99.5,
                       volume24hr=None, volume24hrClob=12.0, oneDayPriceChange="x")
    [m] = parse_event(gamma_event([raw]))
    assert (m.best_bid, m.best_ask, m.one_day_change) == (None, None, None)
    assert m.liquidity == 99.5 and m.volume_24h == 12.0


@pytest.mark.parametrize(
    "fields, active, closed",
    [
        ({}, True, False),
        ({"acceptingOrders": False}, False, False),
        ({"archived": True}, False, False),
        ({"active": False}, False, False),
        ({"closed": True}, True, True),
        ({"acceptingOrders": None}, True, False),
    ],
)
def test_parse_event_active_and_closed(fields, active, closed):
    [m] = parse_event(gamma_event([gamma_market(**fields)]))
    assert (m.active, m.closed) == (active, closed)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2031-06-30T12:00:00Z", datetime(2031, 6, 30, 12, 0, tzinfo=timezone.utc)),
        ("2031-06-30T12:00:00.5Z", datetime(2031, 6, 30, 12, 0, 0, 500000, tzinfo=timezone.utc)),
        ("2031-06-30 07:30:39+00", datetime(2031, 6, 30, 7, 30, 39, tzinfo=timezone.utc)),
        ("2031-06-30T12:00:00.1234567+00:00:00", datetime(2031, 6, 30, 12, 0, 0, 123456, tzinfo=timezone.utc)),
        ("2031-06-30T08:00:00-04:00", datetime(2031, 6, 30, 12, 0, tzinfo=timezone.utc)),
        ("2031-06-30T12:00", datetime(2031, 6, 30, 12, 0, tzinfo=timezone.utc)),
        ("2031-06-30", datetime(2031, 6, 30, 0, 0, tzinfo=timezone.utc)),
    ],
)
def test_parse_event_lenient_dates(text, expected):
    [m] = parse_event(gamma_event([gamma_market(endDate=text)]))
    assert m.end == expected


def test_parse_event_bad_end_date_falls_back_to_event():
    [m] = parse_event(gamma_event([gamma_market(endDate="sometime soon")]))
    assert m.end == datetime(2031, 7, 1, tzinfo=timezone.utc)
    [m] = parse_event(gamma_event([gamma_market(endDate=None)], endDate=None))
    assert m.end is None


def test_parse_event_uma_status_v1_and_history():
    [m] = parse_event(gamma_event([gamma_market(umaResolutionStatus="Resolved")]))
    assert m.uma_status == "resolved"
    [m] = parse_event(gamma_event([gamma_market(umaResolutionStatuses="[\"proposed\", \"disputed\"]")]))
    assert m.uma_status == "disputed"


def test_parse_event_derives_exclusion_tags():
    cases = [
        ({"tags": [{"id": "1", "label": "Sports"}]}, {}, "sports"),
        ({"gameId": 31337}, {}, "sports"),
        ({}, {"sportsMarketType": "moneyline"}, "sports"),
        ({}, {"feeType": "sports_fees_v9"}, "sports"),
        ({"tags": [{"id": "9", "slug": "Example-Mentions"}]}, {}, "mention-markets"),
        ({"seriesSlug": "example-show-mentions"}, {}, "mention-markets"),
        ({"slug": "what-will-the-example-chair-say-at-the-hearing"}, {}, "mention-markets"),
        ({"slug": "xyz-updown-15m-1900000000"}, {}, "up-or-down"),
        ({"series": [{"slug": "xyz-up-or-down-hourly", "recurrence": "hourly"}]}, {}, "up-or-down"),
        ({"tags": [{"id": "102892", "slug": "5M"}]}, {}, "up-or-down"),
        ({"tags": [{"id": "64", "slug": "esports"}]}, {}, "esports"),
    ]
    for event_fields, market_fields, tag in cases:
        [m] = parse_event(gamma_event([gamma_market(**market_fields)], **event_fields))
        assert tag in m.tags, (event_fields, market_fields)
        assert excluded(m), (event_fields, market_fields)
    [m] = parse_event(gamma_event([gamma_market()], series=[{"slug": "example-board", "recurrence": "monthly"}]))
    assert m.tags == ("finance", "made-up") and not excluded(m)


# ---------- 排除规则 ----------


def test_excluded_by_tag_case_insensitive():
    assert excluded(pm(tags=("finance", "Mention-Markets")))
    assert not excluded(pm(tags=("finance", "economy")))


@pytest.mark.parametrize(
    "question, hit",
    [
        ("Will the Example Chair say \"soft landing\" this week?", True),
        ("What will the Example Chair say?", True),
        ("Will an essay prize go to the Example Board?", False),
        ("Example Coin Up or Down on May 3?", True),
        ("Will someone be KILLED in the example contest?", True),
        ("Will the example mention count exceed 5?", True),
        ("Will the Example Board raise rates?", False),
        # 生死类的词按整词算：diesel、studies、diet、skilled 不算
        ("Will the Example Port import more diesel in May?", False),
        ("Will the Example Studies index rise?", False),
        ("Will the Example Diet Co list shares?", False),
        ("Will skilled-worker visas rise in Example Land?", False),
        ("Will the example founder die before 2031?", True),
        ("Who dies first in the example story?", True),
        ("Has the example tortoise died?", True),
        ("Will the Example Envoy be assassinated?", True),
    ],
)
def test_excluded_words(question, hit):
    assert excluded(pm(question=question)) is hit


def test_excluded_checks_event_title_and_group_title_and_custom_settings():
    assert excluded(pm(event_title="What will the example host say tonight"))
    assert excluded(pm(group_title="Tweet count 100+"))
    custom = PolymarketSettings(exclude_tags=("made-up",), exclude_words=("board", "  ", "!!"))
    assert excluded(pm(), custom)  # "board" 在问题里
    assert excluded(pm(question="x", event_title="y", tags=("MADE-UP",)), custom)
    assert not excluded(pm(question="x", event_title="y"), custom)  # 空白和纯标点的词不算，不会匹配一切


# ---------- 盘口 ----------


def test_book_sorts_client_side_and_parses_strings():
    payload = {
        "market": COND_A, "asset_id": TOKEN_YES, "timestamp": "1900000000123", "hash": "f" * 40,
        # 实际接口买单从低到高、卖单从高到低，最优价在最后
        "bids": [{"price": "0.30", "size": "1000"}, {"price": "0.36", "size": "50.5"}, {"price": "0.38", "size": "200"},
                 {"price": "bad", "size": "1"}, {"price": "0.2", "size": "0"}, {"price": "1.5", "size": "3"}],
        "asks": [{"price": "0.70", "size": "900"}, {"price": "0.44", "size": "300"}, {"price": "0.40", "size": "125"}],
        "min_order_size": "5", "tick_size": "0.01", "neg_risk": False, "last_trade_price": "0.39",
    }
    poly, calls = make_poly(routes({(CLOB_HOST, "/book"): payload}))
    book = poly.book(TOKEN_YES)
    assert calls[0].url.params["token_id"] == TOKEN_YES
    assert book.bids == ((0.38, 200.0), (0.36, 50.5), (0.30, 1000.0))
    assert book.asks == ((0.40, 125.0), (0.44, 300.0), (0.70, 900.0))
    assert (book.best_bid, book.best_ask) == (0.38, 0.40)
    assert book.spread == 0.02
    assert book.mid == pytest.approx(0.39)


def test_book_depth():
    book = Book(
        bids=((0.50, 100.0), (0.55, 200.0), (0.60, 40.0), (0.49, 1000.0)),
        asks=((0.65, 10.0), (0.62, 100.0), (0.67, 50.0), (0.68, 1000.0)),
    )
    assert book.best_bid == 0.60 and book.best_ask == 0.62
    up, down = book.depth()
    # 往上：0.62、0.65、0.67 三档（0.67 正好是最优卖价 + 0.05，包含在内）
    assert up == pytest.approx(0.62 * 100 + 0.65 * 10 + 0.67 * 50)
    # 往下：0.60、0.55 两档（0.55 正好是最优买价 - 0.05），按 1 - 价格计
    assert down == pytest.approx(0.40 * 40 + 0.45 * 200)
    up, down = book.depth(0.10)
    assert up == pytest.approx(0.62 * 100 + 0.65 * 10 + 0.67 * 50 + 0.68 * 1000)
    assert down == pytest.approx(0.40 * 40 + 0.45 * 200 + 0.50 * 100)


def test_book_empty_and_crossed():
    empty = Book((), ())
    assert (empty.best_bid, empty.best_ask, empty.spread, empty.mid) == (None, None, None, None)
    assert empty.depth() == (0.0, 0.0)
    one_side = Book(((0.3, 10.0),), ())
    assert one_side.spread is None and one_side.mid is None and one_side.depth() == (0.0, 7.0)
    crossed = Book(((0.55, 10.0),), ((0.50, 10.0),))
    assert crossed.spread is None and crossed.mid is None


def test_book_bad_payload_is_empty_and_404_raises():
    poly, _ = make_poly(routes({(CLOB_HOST, "/book"): ["not an object", {"bids": None, "asks": "x"}]}))
    assert poly.book(TOKEN_YES) == Book((), ())
    assert poly.book(TOKEN_YES) == Book((), ())
    poly, _ = make_poly(routes({}))
    with pytest.raises(FetchError) as info:
        poly.book(TOKEN_YES)
    assert info.value.status == 404


# ---------- 价格历史 ----------


def test_history_params_cursor_loop_and_sorting():
    first = page(
        [{"timestamp": 1900003600, "price": 0.52, "resolution_seconds": 3600},
         {"timestamp": 1900000000, "price": 0.50, "resolution_seconds": 3600},
         {"timestamp": "x", "price": 0.4}, {"timestamp": 1900001000, "price": 1.7}],
        next_cursor="c-two",
    )
    second = page([{"timestamp": 1900007200, "price": 0.57, "resolution_seconds": 3600},
                   {"timestamp": 1900008123, "price": 0.58, "resolution_seconds": 0}])
    poly, calls = make_poly(routes({(DATA_HOST, "/v2/prices-history"): [first, second]}))
    points = poly.history(TOKEN_YES, 1899990000)
    assert points == [(1900000000, 0.50), (1900003600, 0.52), (1900007200, 0.57), (1900008123, 0.58)]
    assert len(calls) == 2
    params = calls[0].url.params
    assert params["token_id"] == TOKEN_YES and params["start"] == "1899990000" and params["bucket_seconds"] == "3600"
    assert "offset" not in params and "cursor" not in params
    assert calls[1].url.params["cursor"] == "c-two" and calls[1].url.params["token_id"] == TOKEN_YES
    assert all("offset" not in c.url.params for c in calls)


def test_history_stops_on_null_cursor_and_rejects_zero_start():
    poly, calls = make_poly(routes({(DATA_HOST, "/v2/prices-history"): page([], next_cursor=None)}))
    assert poly.history(TOKEN_YES, 1900000000, bucket=1800) == []
    assert len(calls) == 1 and calls[0].url.params["bucket_seconds"] == "1800"
    with pytest.raises(ValueError):
        poly.history(TOKEN_YES, 0)


# ---------- 成交 ----------


def test_trades_params_pages_and_stops_at_since():
    since = 1900000000
    first = page([trade_row(WALLETS[0], "BUY", 100, since + 500), trade_row(WALLETS[1], "SELL", 40, since + 400)],
                 next_cursor="t-two")
    second = page([trade_row(WALLETS[2], "BUY", 30, since + 10), trade_row(WALLETS[3], "BUY", 99, since - 5)],
                  next_cursor="t-three")
    third = page([trade_row(WALLETS[4], "BUY", 1, since - 100)])
    poly, calls = make_poly(routes({(DATA_HOST, "/v2/trades"): [first, second, third]}))
    trades = poly.trades(COND_A, since, max_pages=5)
    assert [t.size for t in trades] == [100.0, 40.0, 30.0]  # 早于 since 的不要，从新到旧
    assert len(calls) == 2  # 第二页已经翻到 since 之前，第三页不再请求
    params = calls[0].url.params
    assert params["condition"] == COND_A and params["taker_only"] == "true" and params["limit"] == "1000"
    assert "offset" not in params and "market" not in params
    assert calls[1].url.params["cursor"] == "t-two" and calls[1].url.params["condition"] == COND_A
    assert "limit" not in calls[1].url.params
    assert trades[1].side == "SELL" and trades[1].ts == since + 400 and trades[1].outcome_index == 0


def test_trades_respects_max_pages(caplog):
    since = 1900000000
    full = page([trade_row(WALLETS[0], "BUY", 5, since + 100)], next_cursor="again-1")
    other = page([trade_row(WALLETS[1], "BUY", 5, since + 50)], next_cursor="again-2")
    poly, calls = make_poly(routes({(DATA_HOST, "/v2/trades"): [full, other]}))
    with caplog.at_level(logging.INFO, logger="digest.markets"):
        trades = poly.trades(COND_A, since)
    assert len(calls) == 2 and len(trades) == 2
    assert "超过 2 页" in caplog.text


def test_trades_since_reports_whether_the_window_is_complete():
    since = 1900000000
    newer = [trade_row(WALLETS[0], "BUY", 5, since + 100)]
    # 翻满页数还没翻到 since 之前：不完整
    poly, calls = make_poly(routes({(DATA_HOST, "/v2/trades"): [page(newer, "c-1"), page(newer, "c-2")]}))
    trades, complete = poly.trades_since(COND_A, since, max_pages=2)
    assert len(trades) == 2 and complete is False and len(calls) == 2
    # 翻到了 since 之前的记录：完整
    older = [trade_row(WALLETS[1], "BUY", 5, since - 1)]
    poly, _ = make_poly(routes({(DATA_HOST, "/v2/trades"): [page(newer, "c-1"), page(older, "c-2")]}))
    assert poly.trades_since(COND_A, since, max_pages=2)[1] is True
    # 接口说没有下一页、或者干脆没有成交：完整
    for last in (page(newer, has_more=False), page([], "c-3")):
        poly, _ = make_poly(routes({(DATA_HOST, "/v2/trades"): last}))
        assert poly.trades_since(COND_A, since, max_pages=1)[1] is True
    # 只有一页而且还有下一页：不完整
    poly, _ = make_poly(routes({(DATA_HOST, "/v2/trades"): page(newer, "c-4")}))
    assert poly.trades_since(COND_A, since, max_pages=1) == (poly.trades(COND_A, since, max_pages=1), False)


def test_trades_parse_drops_bad_rows_and_hides_addresses():
    since = 1900000000
    rows = [
        trade_row(WALLETS[0].upper().replace("0X", "0x"), "buy", 10, since + 9),
        trade_row(WALLETS[0], "SELL", 4, since + 8, outcome_index=1),
        trade_row("", "BUY", 1, since + 7),
        trade_row(WALLETS[1], "HOLD", 1, since + 6),
        trade_row(WALLETS[1], "BUY", 0, since + 5),
        trade_row(WALLETS[1], "BUY", 1, since + 4, price=1.4),
        {**trade_row(WALLETS[1], "BUY", 2, since + 3), "outcome_index": None},
    ]
    poly, _ = make_poly(routes({(DATA_HOST, "/v2/trades"): page(rows)}))
    trades = poly.trades(COND_A, since)
    assert [(t.side, t.size, t.outcome_index) for t in trades] == [("BUY", 10.0, 0), ("SELL", 4.0, 1), ("BUY", 2.0, 999)]
    # 大小写不同的同一个地址是同一个钱包；保存的不是地址本身，也没有个人资料
    assert trades[0].wallet == trades[1].wallet
    text = repr(trades)
    assert all(w.lower()[2:] not in text.lower() for w in WALLETS)
    assert "Fake" not in text and "bio" not in text


# ---------- 钱包集中度 ----------


def test_wallet_stats_net_signed_shares():
    trades = [
        Trade("w1", "BUY", 100.0, 0.5, 1, 0),
        Trade("w1", "SELL", 30.0, 0.5, 2, 0),   # w1 净 +70
        Trade("w2", "SELL", 50.0, 0.5, 3, 0),   # w2 净 -50
        Trade("w3", "BUY", 20.0, 0.5, 4, 0),
        Trade("w4", "BUY", 10.0, 0.5, 5, 0),
        Trade("w5", "BUY", 5.0, 0.5, 6, 0),
        Trade("w9", "BUY", 1000.0, 0.5, 7, 1),  # 另一个结果，不算
        Trade("w9", "BUY", 1000.0, 0.5, 8, 999),
    ]
    n, share = wallet_stats(trades[:6], 0)
    assert n == 5
    assert share == pytest.approx(70 / (70 + 50 + 20 + 10 + 5))
    # 买 NO 等于卖 YES：w9 在另一个结果上买了 1000 份，按 YES 算是净 -1000；没标结果（999）的不算
    n, share = wallet_stats(trades, 0)
    assert n == 6
    assert share == pytest.approx(1000 / (70 + 50 + 20 + 10 + 5 + 1000))
    # 从 NO 一方看是同一回事，只是方向反过来
    assert wallet_stats(trades, 1) == (n, share)


def test_wallet_stats_whale_buying_no_fails_concentration():
    # 五个小钱包买 YES，一个大钱包买了一大笔 NO（同样会把 YES 的价格往下压）：不能当作“很多人在买”
    trades = [Trade(f"w{i}", "BUY", 10.0, 0.7, i, 0) for i in range(5)]
    trades.append(Trade("whale", "BUY", 8000.0, 0.3, 9, 1))
    n, share = wallet_stats(trades, 0)
    assert n == 6 and share == pytest.approx(8000 / 8050) and share > 0.5
    # 卖 NO 等于买 YES：同一个钱包卖 3 份 NO、买 3 份 YES 是净 +6，不是相抵成 0
    mixed = [Trade("a", "SELL", 3.0, 0.4, 1, 1), Trade("a", "BUY", 3.0, 0.6, 2, 0), Trade("b", "BUY", 2.0, 0.6, 3, 0)]
    assert wallet_stats(mixed, 0) == (2, 0.75)


def test_wallet_stats_edges():
    assert wallet_stats([], 0) == (0, 1.0)
    # 都买卖相抵：说明不了是很多人在买
    wash = [Trade("w1", "BUY", 5.0, 0.5, 1, 0), Trade("w1", "SELL", 5.0, 0.5, 2, 0),
            Trade("w2", "BUY", 0.1, 0.5, 3, 0), Trade("w2", "SELL", 0.1, 0.5, 4, 0)]
    assert wallet_stats(wash, 0) == (2, 1.0)
    # 正好一半，边界上没有浮点误差
    half = [Trade("w1", "BUY", 0.3, 0.5, 1, 0), Trade("w2", "BUY", 0.1, 0.5, 2, 0), Trade("w3", "SELL", 0.2, 0.5, 3, 0)]
    assert wallet_stats(half, 0) == (3, 0.5)


# ---------- 结算状态和结算价 ----------


def resolution_row(**fields) -> dict:
    d = {"condition_id": COND_A, "status": "posed", "extended_review": False, "was_disputed": False,
         "new_version_q": False, "transaction_hash": "", "log_index": "", "last_update_timestamp": "1900000000"}
    d.update(fields)
    return d


@pytest.mark.parametrize(
    "fields, clean",
    [
        ({}, True),
        ({"status": "initialized"}, True),
        ({"status": "Active"}, True),
        ({"status": "proposed"}, False),
        ({"status": "resolved"}, False),
        ({"status": "settled"}, False),  # 不认识的状态不算干净
        ({"was_disputed": True}, False),
        ({"extended_review": True}, False),
        ({"new_version_q": True}, False),
        ({"new_version_q": None}, False),  # 必有的标志缺了，按有问题处理
    ],
)
def test_resolution_clean(fields, clean):
    poly, calls = make_poly(routes({(DATA_HOST, "/v2/resolutions"): {"data": [resolution_row(**fields)]}}))
    res = poly.resolution(COND_A)
    assert res is not None and res.clean() is clean
    assert calls[0].url.params["condition"] == COND_A


def test_resolution_missing_and_multiple_rows():
    poly, _ = make_poly(routes({(DATA_HOST, "/v2/resolutions"): [{"data": []}, {"oops": 1}]}))
    assert poly.resolution(COND_A) is None
    assert poly.resolution(COND_A) is None
    poly, _ = make_poly(routes({}))
    assert poly.resolution(COND_A) is None  # 404
    rows = [resolution_row(), resolution_row(status="challenged", was_disputed=True), resolution_row(condition_id=COND_B,
                                                                                                    status="resolved")]
    poly, _ = make_poly(routes({(DATA_HOST, "/v2/resolutions"): {"data": rows}}))
    res = poly.resolution(COND_A)
    assert res == Resolution(status="challenged", was_disputed=True, extended_review=False, new_version_q=False)
    assert not res.clean()


def test_resolution_server_error_raises():
    poly, _ = make_poly(routes({(DATA_HOST, "/v2/resolutions"): httpx.Response(400, json={"error": "bad"})}))
    with pytest.raises(FetchError):
        poly.resolution(COND_A)


def test_token_final():
    resolved = {"data": [{"token_id": TOKEN_YES, "condition_id": COND_A, "outcome_index": 0, "resolved": True,
                          "final_price": 1, "closed": True}]}
    open_ = {"data": [{"token_id": TOKEN_YES, "resolved": False, "final_price": None}]}
    other = {"data": [{"token_id": TOKEN_NO, "resolved": True, "final_price": 0}]}
    fraction = {"data": [{"token_id": TOKEN_YES, "resolved": True, "final_price": "0.25"}]}
    poly, calls = make_poly(routes({(DATA_HOST, "/v2/tokens"): [resolved, open_, other, fraction, {"data": None}]}))
    assert poly.token_final(TOKEN_YES) == 1.0
    assert calls[0].url.params["token_id"] == TOKEN_YES
    assert poly.token_final(TOKEN_YES) is None
    assert poly.token_final(TOKEN_YES) is None
    assert poly.token_final(TOKEN_YES) == 0.25
    assert poly.token_final(TOKEN_YES) is None
    poly, _ = make_poly(routes({}))
    assert poly.token_final(TOKEN_YES) is None


# ---------- 事件列表 ----------


def test_events_params_cursor_and_dedupe():
    first = {"$schema": "x", "events": [gamma_event([gamma_market(COND_A), gamma_market(COND_B)])],
             "next_cursor": "k-two"}
    second = {"events": [gamma_event([gamma_market(COND_B), gamma_market(COND_C)], id="434343"), "junk"]}
    poly, calls = make_poly(routes({(GAMMA_HOST, "/events/keyset"): [first, second]}))
    markets = poly.events(120)
    assert [m.condition_id for m in markets] == [COND_A, COND_B, COND_C]
    assert len(calls) == 2
    params = calls[0].url.params
    assert params["closed"] == "false" and params["tag_id"] == "120" and params["limit"] == str(EVENTS_PAGE)
    assert params.get_list("version") == ["v1", "v2"]
    assert params["order"] == "volume24hr" and params["ascending"] == "false"
    assert "after_cursor" not in params and "offset" not in params and "active" not in params
    second_params = calls[1].url.params
    assert second_params["after_cursor"] == "k-two"
    assert second_params["tag_id"] == "120" and second_params.get_list("version") == ["v1", "v2"]


def test_events_max_pages(caplog):
    full = {"events": [gamma_event([gamma_market(COND_A)])], "next_cursor": "k-more"}
    poly, calls = make_poly(routes({(GAMMA_HOST, "/events/keyset"): full}))
    with caplog.at_level(logging.INFO, logger="digest.markets"):
        poly.events(120, max_pages=1)
    assert len(calls) == 1 and "超过 1 页" in caplog.text


def test_events_falls_back_to_simple_query_on_422():
    def handler(request: httpx.Request) -> httpx.Response:
        if "order" in request.url.params or "version" in request.url.params:
            return httpx.Response(422, json={"type": "validation", "error": "bad order"})
        return httpx.Response(200, json={"events": [gamma_event([gamma_market()])]})

    poly, calls = make_poly(handler)
    assert [m.condition_id for m in poly.events(120)] == [COND_A]
    assert len(calls) == 2
    assert "order" not in calls[1].url.params and calls[1].url.params["closed"] == "false"
    # 之后的标签直接用简单查询
    poly.events(100328)
    assert len(calls) == 3 and "version" not in calls[2].url.params


def test_events_later_page_failure_keeps_earlier_pages(caplog):
    first = {"events": [gamma_event([gamma_market(COND_A)])], "next_cursor": "k-two"}
    poly, calls = make_poly(routes({(GAMMA_HOST, "/events/keyset"): [first, httpx.Response(400, json={})]}))
    with caplog.at_level(logging.INFO, logger="digest.markets"):
        markets = poly.events(120)
    assert [m.condition_id for m in markets] == [COND_A]
    assert "HTTP 400" in caplog.text


def test_events_first_page_failure_raises():
    poly, _ = make_poly(routes({(GAMMA_HOST, "/events/keyset"): httpx.Response(400, json={})}))
    with pytest.raises(FetchError) as info:
        poly.events(120)
    assert str(info.value) == f"{GAMMA_HOST} 400"


def test_budget_exceeded_propagates_unwrapped():
    full = {"events": [gamma_event([gamma_market(COND_A)])], "next_cursor": "k-two"}
    poly, calls = make_poly(routes({(GAMMA_HOST, "/events/keyset"): full}), requests=1)
    with pytest.raises(BudgetExceeded):
        poly.events(120)
    assert len(calls) == 1


def test_no_market_content_in_logs_or_errors(caplog):
    first = {"events": [gamma_event([gamma_market(COND_A)])], "next_cursor": "k-two"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == GAMMA_HOST and "after_cursor" in request.url.params:
            return httpx.Response(503, json={"error": "down"})
        if "order" in request.url.params:
            return httpx.Response(422, json={"error": "x"})
        if request.url.host == GAMMA_HOST:
            return httpx.Response(200, json=first)
        return httpx.Response(500, json={"error": "x"})

    poly, _ = make_poly(handler)
    errors = []
    # 和 cli.setup_logging 一样：httpx 自己在 INFO 级别会记完整地址，运行时被调到 WARNING
    for name in ("httpx", "httpcore"):
        caplog.set_level(logging.WARNING, logger=name)
    with caplog.at_level(logging.DEBUG, logger="digest"):
        poly.events(120, max_pages=3)
        for call in (lambda: poly.book(TOKEN_YES), lambda: poly.trades(COND_A, 1900000000),
                     lambda: poly.resolution(COND_A), lambda: poly.history(TOKEN_YES, 1900000000)):
            with pytest.raises(FetchError) as info:
                call()
            errors.append(str(info.value))
    text = caplog.text + "\n".join(errors)
    assert "改用最简单的查询" in text and "第 2 页没取到" in text and "重试一次" in text
    for secret in (QUESTION, COND_A, TOKEN_YES, "Example", "k-two", "0.41"):
        assert secret not in text
