from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, timezone

import httpx
import pytest

from digest.markets.kalshi import Kalshi
from digest.markets.net import Budget, Fetcher
from digest.markets.polymarket import Polymarket
from digest.markets.schedule import BJT
from digest.markets.score import collect_settlements, collect_witnesses, weekly_lines
from digest.markets.settings import MarketsSettings, Releases, WitnessSettings

# 全是编造的月份（2029 年）、数值、合约代码和地址，字段名和格式照接口文档
BLS_HOST = "bls.example-source.test"
NYFED_HOST = "nyfed.example-source.test"
KALSHI_HOST = "kalshi-score.example-market.test"
KALSHI_BASE = f"https://{KALSHI_HOST}/trade-api/v2"
DATA_HOST = "data-api.polymarket.com"
COND = "0x" + "d4" * 32
TOKEN = "7" * 70 + "12345"
NOW = datetime(2029, 4, 12, 0, 0, tzinfo=timezone.utc)

CPI_ID, CORE_ID, PAY_ID, U3_ID = "CUSR0000SA0", "CUSR0000SA0L1E", "CES0000000001", "LNS14000000"


class FakeClock:
    def __init__(self) -> None:
        self.now = 3000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_fetcher(handler, *, requests: int = 80):
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(record))
    fetcher = Fetcher(client, Budget(150.0, requests, clock=clock), sleep=clock.sleep, jitter=lambda: 0.0)
    return fetcher, calls


def by_host(table: dict):
    """按 (主机, 路径) 分发；值是响应、JSON 或 request -> 响应的函数。没有的返回 404。"""

    def handler(request: httpx.Request) -> httpx.Response:
        item = table.get((request.url.host, request.url.path))
        if item is None:
            return httpx.Response(404, json={"error": "not found"})
        if callable(item):
            item = item(request)
        return item if isinstance(item, httpx.Response) else httpx.Response(200, json=item)

    return handler


# ---------- 官方首次公布值 ----------


def score_settings(**releases) -> MarketsSettings:
    tables = {
        # 未来的一期（还没公布）和太早的一期（已经过了取首次公布值的时间）都不该去取
        "cpi": {(2029, 3): date(2029, 4, 11), (2029, 4): date(2029, 5, 10)},
        "empsit": {(2029, 1): date(2029, 2, 2), (2029, 3): date(2029, 4, 6)},
        "fomc": (date(2029, 4, 4),),
    }
    tables.update(releases)
    return replace(
        MarketsSettings(),
        releases=Releases(**tables),
        witnesses=WitnessSettings(bls_v1=f"https://{BLS_HOST}/v1/data/",
                                  nyfed=f"https://{NYFED_HOST}/rates/last/10.json"),
    )


def bls(series_id: str, values: dict[tuple[int, int], str], status: str = "REQUEST_SUCCEEDED") -> dict:
    data = [{"year": str(y), "period": f"M{m:02d}", "periodName": "x", "value": v, "footnotes": [{}]}
            for (y, m), v in sorted(values.items(), reverse=True)]
    return {"status": status, "responseTime": 12, "message": [], "Results": {"series": [{"seriesID": series_id,
                                                                                          "data": data}]}}


def nyfed(rows: list[tuple[str, float]]) -> dict:
    return {"refRates": [{"effectiveDate": d, "type": "EFFR", "percentRate": upper - 0.17,
                          "targetRateFrom": upper - 0.25, "targetRateTo": upper, "revisionIndicator": ""}
                         for d, upper in rows]}


BLS_DATA = {
    CPI_ID: bls(CPI_ID, {(2029, 3): "345.678", (2029, 2): "344.567", (2029, 1): "343.001"}),
    CORE_ID: bls(CORE_ID, {(2029, 3): "300.300", (2029, 2): "300.000"}),
    PAY_ID: bls(PAY_ID, {(2029, 3): "160234", (2029, 2): "160111", (2029, 1): "160050", (2028, 12): "159990"}),
    U3_ID: bls(U3_ID, {(2029, 3): "4.6", (2029, 1): "4.4"}),
}
# 新区间从声明（4 月 4 日）的第二天起生效：4.75 -> 4.50，降 25 个基点
NYFED_DATA = nyfed([("2029-04-09", 4.50), ("2029-04-06", 4.50), ("2029-04-05", 4.50), ("2029-04-04", 4.75),
                    ("2029-04-03", 4.75)])


def official_routes(**overrides) -> dict:
    table = {(BLS_HOST, f"/v1/data/{sid}"): payload for sid, payload in BLS_DATA.items()}
    table[(NYFED_HOST, "/rates/last/10.json")] = NYFED_DATA
    for sid, payload in overrides.items():
        if sid == "nyfed":
            table[(NYFED_HOST, "/rates/last/10.json")] = payload
        else:
            table[(BLS_HOST, f"/v1/data/{sid}")] = payload
    return table


def witness(series: str, period: str, value: float) -> dict:
    return {"type": "witness", "ts": "2029-04-01T00:00:00Z", "series": series, "period": period, "value": value,
            "source": "bls_v1", "fetched": "2029-04-01T00:00:00Z", "seq": 1}


def test_witnesses_from_bls_and_nyfed_with_request_cap(caplog):
    caplog.set_level(logging.DEBUG, logger="digest.markets")
    fetcher, calls = make_fetcher(by_host(official_routes()))
    rows = collect_witnesses(fetcher, score_settings(), [], NOW)
    # 按公布时间先后：议息（4 月 4 日）、非农和失业率（4 月 6 日）、CPI（4 月 11 日）；核心 CPI 是第 5 个请求，留到下次
    assert [c.url.path.rsplit("/", 1)[1] for c in calls] == ["10.json", PAY_ID, U3_ID, CPI_ID]
    assert all(not c.url.params for c in calls)  # 不带任何查询参数
    assert [(r["series"], r["period"], r["value"], r["source"]) for r in rows] == [
        ("KXFEDDECISION", "2029-04", -25.0, "nyfed"),
        ("KXPAYROLLS", "2029-03", 123000.0, "bls_v1"),
        ("KXU3", "2029-03", 4.6, "bls_v1"),
        ("KXCPI", "2029-03", 0.3, "bls_v1"),
    ]
    assert all(r["type"] == "witness" and r["ts"] == r["fetched"] == "2029-04-12T00:00:00Z" for r in rows)
    for record in caplog.records:
        assert "KX" not in record.getMessage() and "345" not in record.getMessage()


def test_witnesses_skip_what_the_ledger_already_has():
    fetcher, calls = make_fetcher(by_host(official_routes()))
    have = [witness("KXFEDDECISION", "2029-04", -25.0), witness("KXU3", "2029-03", 4.6)]
    rows = collect_witnesses(fetcher, score_settings(), have, NOW)
    assert [c.url.path.rsplit("/", 1)[1] for c in calls] == [PAY_ID, CPI_ID, CORE_ID]
    assert {(r["series"], r["value"]) for r in rows} == {("KXPAYROLLS", 123000.0), ("KXCPI", 0.3), ("KXCPICORE", 0.1)}


def test_witness_failures_are_retried_next_run():
    fetcher, calls = make_fetcher(by_host(official_routes(**{PAY_ID: httpx.Response(503, text="busy")})))
    rows = collect_witnesses(fetcher, score_settings(cpi={}, fomc=()), [], NOW)
    assert [(r["series"], r["value"]) for r in rows] == [("KXU3", 4.6)]
    assert len(calls) == 3  # 非农的请求连同重试两次，失业率一次


def test_witnesses_wait_until_the_data_is_out():
    overrides = {
        CPI_ID: bls(CPI_ID, {(2029, 2): "344.567"}),  # 3 月还没出来
        CORE_ID: bls(CORE_ID, {}, status="REQUEST_NOT_PROCESSED"),  # 超过每天的次数
        "nyfed": nyfed([("2029-04-04", 4.75), ("2029-04-03", 4.75)]),  # 声明之后的那天还没出来
    }
    fetcher, calls = make_fetcher(by_host(official_routes(**overrides)))
    assert collect_witnesses(fetcher, score_settings(empsit={}), [], NOW) == []
    assert len(calls) == 3


def test_witnesses_stop_when_budget_runs_out():
    fetcher, calls = make_fetcher(by_host(official_routes()), requests=2)
    rows = collect_witnesses(fetcher, score_settings(), [], NOW)
    assert [r["series"] for r in rows] == ["KXFEDDECISION", "KXPAYROLLS"] and len(calls) == 2


# ---------- 平台结算 ----------


DIST = {"ks": [0.1, 0.2, 0.3], "s": [0.9, 0.58, 0.17], "grid": 0.1, "median": [0.3, None], "q25": [0.2, None],
        "q75": [0.3, None], "mass50": 0.73}


def shown(seq: int, key: str, ts: str, expires: str, *, venue: str = "kalshi", section: str = "key", p=None,
          dist=None, series=None, period=None) -> dict:
    row = {"type": "shown", "seq": seq, "ts": ts, "edition": "morning", "mode": "send", "venue": venue, "key": key,
           "section": section, "question": "（中文）", "expires": expires}
    if p is not None:
        row["p"] = p
    if dist is not None:
        row["dist"] = dist
    if series is not None:
        row["series"], row["period"] = series, period
    return row


def settle(seq: int, key: str, ts: str, outcome, *, venue: str = "kalshi", label: str | None = None) -> dict:
    row = {"type": "settle", "seq": seq, "ts": ts, "venue": venue, "key": key, "outcome": outcome, "source": venue,
           "status": "finalized"}
    if label:
        row["label"] = label
    return row


def kmarket(ticker: str, **fields) -> dict:
    d = {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "status": "finalized",
         "close_time": "2029-04-04T17:59:00Z", "result": "no", "expiration_value": "", "yes_bid_dollars": "0.0000",
         "yes_ask_dollars": "1.0000", "open_interest_fp": "0.00"}
    d.update(fields)
    return d


CPI_LEGS = [kmarket(f"KXCPI-29MAR-T{k}", strike_type="greater", floor_strike=k, result=r, expiration_value="0.3")
            for k, r in ((0.1, "yes"), (0.2, "yes"), (0.3, "no"))]
UNCLEAR_LEGS = [kmarket(f"KXCPI-29FEB-T{k}", strike_type="greater", floor_strike=k, result="yes") for k in (0.1, 0.2)]
TICKERS = {
    "KXFEDDECISION-29APR-H0": kmarket("KXFEDDECISION-29APR-H0", strike_type="custom", custom_strike={"Hike": "0"},
                                      result="yes", expiration_value="Fed maintains rate"),
    "KXFEDDECISION-29APR-C25": kmarket("KXFEDDECISION-29APR-C25", strike_type="custom", custom_strike={"Cut": "25"},
                                       status="determined"),
}
EVENTS = {"KXCPI-29MAR": CPI_LEGS, "KXCPI-29FEB": UNCLEAR_LEGS}


def kalshi_markets(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    if request.url.path.endswith("/historical/markets"):
        return httpx.Response(200, json={"markets": [], "cursor": ""})
    if "tickers" in params:
        found = [TICKERS[params["tickers"]]] if params["tickers"] in TICKERS else []
    else:
        found = EVENTS.get(params.get("event_ticker", ""), [])
    return httpx.Response(200, json={"markets": found, "cursor": ""})


def venue_routes(overrides: dict | None = None) -> dict:
    table = {
        (KALSHI_HOST, "/trade-api/v2/markets"): kalshi_markets,
        (KALSHI_HOST, "/trade-api/v2/historical/markets"): kalshi_markets,
        (DATA_HOST, "/v2/tokens"): {"data": [{"token_id": TOKEN, "condition_id": COND, "resolved": True,
                                              "final_price": 1}]},
        (DATA_HOST, "/v2/resolutions"): {"data": [{"condition_id": COND, "status": "resolved", "was_disputed": False,
                                                   "extended_review": False, "new_version_q": False}]},
    }
    table.update(overrides or {})
    return table


def ledger_rows() -> list[dict]:
    return [
        shown(1, "KXCPI-29MAR", "2029-04-10T23:35:00Z", "2029-04-11T12:30:00Z", dist=DIST, series="KXCPI",
              period="2029-03"),
        shown(2, "KXFEDDECISION-29APR-H0", "2029-04-01T23:35:00Z", "2029-04-04T17:59:00Z", p=80,
              series="KXFEDDECISION", period="2029-04"),
        shown(3, "KXFEDDECISION-29APR-C25", "2029-04-01T23:35:00Z", "2029-04-04T17:59:00Z", p=12,
              series="KXFEDDECISION", period="2029-04"),
        shown(4, f"{COND}:{TOKEN}", "2029-04-02T23:35:00Z", "2029-04-10T00:00:00Z", venue="polymarket",
              section="lopsided", p=91),
        shown(5, "KXU3-29APR", "2029-04-11T23:35:00Z", "2029-05-04T12:30:00Z", dist=DIST, series="KXU3",
              period="2029-04"),  # 还没到期
        shown(6, "KXCPICORE-29MAR", "2029-04-11T13:00:00Z", "2029-04-11T12:30:00Z", dist=DIST, series="KXCPICORE",
              period="2029-03"),  # 过期之后才记下的，不算
        shown(7, "KXPAYROLLS-29MAR", "2029-04-05T23:35:00Z", "2029-04-06T12:30:00Z", dist=DIST, series="KXPAYROLLS",
              period="2029-03"),
        settle(8, "KXPAYROLLS-29MAR", "2029-04-07T00:00:00Z", 123000.0),  # 已经结算过
        shown(9, "KXFEDDECISION-29APR-H0", "2029-04-03T11:35:00Z", "2029-04-04T17:59:00Z", p=70,
              series="KXFEDDECISION", period="2029-04"),  # 晚间版又展示了一次
        shown(10, "KXCPI-28NOV", "2028-12-01T23:35:00Z", "2028-12-10T13:30:00Z", dist=DIST, series="KXCPI",
              period="2028-11"),  # 过期很久，两个库都查不到
    ]


def make_clients(table: dict, *, requests: int = 80):
    fetcher, calls = make_fetcher(by_host(table), requests=requests)
    return Kalshi(fetcher, [KALSHI_BASE]), Polymarket(fetcher), calls


def test_settlements_for_expired_shown_rows(caplog):
    caplog.set_level(logging.DEBUG, logger="digest.markets")
    kalshi, poly, calls = make_clients(venue_routes())
    rows = collect_settlements(kalshi, poly, ledger_rows(), NOW)
    by_key = {r["key"]: r for r in rows}
    assert list(by_key) == ["KXCPI-29MAR", f"{COND}:{TOKEN}", "KXFEDDECISION-29APR-H0", "KXCPI-28NOV"]  # 最近过期的先问
    assert by_key["KXCPI-29MAR"] == {"type": "settle", "ts": "2029-04-12T00:00:00Z", "venue": "kalshi",
                                     "key": "KXCPI-29MAR", "outcome": 0.3, "source": "kalshi", "status": "finalized",
                                     "series": "KXCPI", "period": "2029-03", "section": "key"}
    fed = by_key["KXFEDDECISION-29APR-H0"]
    assert (fed["outcome"], fed["label"], fed["status"]) == (1, "维持不变", "finalized")
    poly_row = by_key[f"{COND}:{TOKEN}"]
    assert (poly_row["outcome"], poly_row["status"], poly_row["disputed"], poly_row["venue"]) == (
        1, "resolved", False, "polymarket")
    old = by_key["KXCPI-28NOV"]
    assert old["outcome"] is None and old["status"] == "missing"
    # 还没结算完（determined）的不记，下次再问；没到期、过期后才展示、已结算的根本不问
    asked = " ".join(str(c.url) for c in calls)
    assert "C25" in asked
    for key in ("KXU3-29APR", "KXCPICORE-29MAR", "KXPAYROLLS-29MAR"):
        assert key not in asked
    assert sum("KXFEDDECISION-29APR-H0" in str(c.url) for c in calls) == 1  # 早晚两次展示只问一次
    for record in caplog.records:
        assert "KX" not in record.getMessage() and COND not in record.getMessage()


def test_settlements_limit_unclear_and_missing_venue():
    kalshi, poly, calls = make_clients(venue_routes())
    rows = collect_settlements(kalshi, poly, ledger_rows(), NOW, limit=2)
    assert [r["key"] for r in rows] == ["KXCPI-29MAR", f"{COND}:{TOKEN}"]
    # 没有 Kalshi 客户端时只问 Polymarket
    rows = collect_settlements(None, poly, ledger_rows(), NOW)
    assert [r["venue"] for r in rows] == ["polymarket"]
    # 各档全是 yes 又没有结算值：说不清，记下来但不计分
    unclear = [shown(1, "KXCPI-29FEB", "2029-03-12T23:35:00Z", "2029-03-13T12:30:00Z", dist=DIST, series="KXCPI",
                     period="2029-02")]
    rows = collect_settlements(kalshi, None, unclear, NOW)
    assert (rows[0]["outcome"], rows[0]["status"]) == (None, "unclear")
    # 已经有结算记录（哪怕是说不清的）就不再问
    assert collect_settlements(kalshi, None, unclear + rows, NOW) == []


def test_settlement_errors_skip_only_that_market():
    table = venue_routes({(DATA_HOST, "/v2/tokens"): httpx.Response(500, text="oops")})
    kalshi, poly, _ = make_clients(table)
    rows = collect_settlements(kalshi, poly, ledger_rows(), NOW)
    assert f"{COND}:{TOKEN}" not in {r["key"] for r in rows}
    assert "KXCPI-29MAR" in {r["key"] for r in rows}


def test_unresolved_polymarket_waits():
    table = venue_routes({(DATA_HOST, "/v2/tokens"): {"data": [{"token_id": TOKEN, "resolved": False}]}})
    _, poly, calls = make_clients(table)
    assert collect_settlements(None, poly, ledger_rows(), NOW) == []
    assert not any(c.url.path == "/v2/resolutions" for c in calls)


def test_settled_rows_feed_the_weekly_line():
    """结算记录（议息的结果名来自 kalshi.fed_outcome）和纽约联储的见证对得上，不算对不上。"""
    kalshi, poly, _ = make_clients(venue_routes())
    rows = ledger_rows()
    rows += collect_settlements(kalshi, poly, rows, NOW)
    rows += [witness("KXFEDDECISION", "2029-04", 0.0), witness("KXCPI", "2029-03", 0.3)]
    lines = weekly_lines(rows, datetime(2029, 4, 16, 7, 30, tzinfo=BJT))
    # 阶梯（0.73，公布值在区间里）、议息维持不变（0.80，发生）、Polymarket（0.91，发生）
    assert lines[0].text.startswith("· 上周 3 件出了结果：应发生 2.4 件（正常范围 ")
    assert lines[0].text.endswith("实际 3 件")
    assert lines[0].rows[0]["mismatches"] == 0


# ---------- 每周一早上 ----------


def week_rows() -> list[dict]:
    u3 = dict(DIST, q25=[4.4, None], q75=[4.5, None], mass50=0.5, ks=[4.3, 4.4, 4.5], s=[0.9, 0.7, 0.2])
    return [
        # 议息“不变”：早上 80%、晚上 70%，只算第一次的 80%
        shown(1, "KXFED-29APR-H0", "2029-04-02T23:35:00Z", "2029-04-04T17:59:00Z", p=80, series="KXFEDDECISION",
              period="2029-04"),
        shown(2, "KXFED-29APR-H0", "2029-04-03T11:35:00Z", "2029-04-04T17:59:00Z", p=70, series="KXFEDDECISION",
              period="2029-04"),
        shown(3, "KXFED-29APR-C25", "2029-04-02T23:35:00Z", "2029-04-04T17:59:00Z", p=15, series="KXFEDDECISION",
              period="2029-04"),
        settle(4, "KXFED-29APR-H0", "2029-04-05T00:00:00Z", 1, label="维持不变"),
        settle(5, "KXFED-29APR-C25", "2029-04-05T00:00:00Z", 0, label="降息 25 个基点"),
        # 阶梯：区间 [0.2, 0.3]、概率 0.6，公布值 0.3 落在里面
        shown(6, "KXCPI-29MAR", "2029-04-02T23:35:00Z", "2029-04-04T12:30:00Z", dist=dict(DIST, mass50=0.6),
              series="KXCPI", period="2029-03"),
        settle(7, "KXCPI-29MAR", "2029-04-04T13:00:00Z", 0.3),
        # 失业率：平台结算 4.5，官方 4.6，对不上
        shown(8, "KXU3-29MAR", "2029-04-02T23:35:00Z", "2029-04-06T12:30:00Z", dist=u3, series="KXU3",
              period="2029-03"),
        settle(9, "KXU3-29MAR", "2029-04-06T13:00:00Z", 4.5),
        # 定价一边倒：91% 的发生了（应验）
        shown(10, "0xaa:11", "2029-04-03T23:35:00Z", "2029-04-05T00:00:00Z", venue="polymarket", section="lopsided",
              p=91),
        settle(11, "0xaa:11", "2029-04-06T00:00:00Z", 1, venue="polymarket"),
        # 过期之后才记下的展示：有结算也不算
        shown(12, "KXCPICORE-29MAR", "2029-04-04T12:31:00Z", "2029-04-04T12:30:00Z", dist=DIST, series="KXCPICORE",
              period="2029-03"),
        settle(13, "KXCPICORE-29MAR", "2029-04-04T13:00:00Z", 0.3),
        # 更早结算的定价一边倒：8% 的发生了（没应验），不在上周，但算进累计
        shown(14, "0xbb:22", "2029-03-20T23:35:00Z", "2029-03-27T00:00:00Z", venue="polymarket", section="lopsided",
              p=8),
        settle(15, "0xbb:22", "2029-03-28T00:00:00Z", 1, venue="polymarket"),
        witness("KXFEDDECISION", "2029-04", 0.0),
        witness("KXCPI", "2029-03", 0.3),
        witness("KXU3", "2029-03", 4.6),
    ]


MONDAY = datetime(2029, 4, 9, 7, 30, tzinfo=BJT)  # 不是本月第一个周一


def test_weekly_line_numbers():
    lines = weekly_lines(week_rows(), MONDAY)
    assert [line.text for line in lines] == [
        "· 上周 4 件出了结果：应发生 2.5 件（正常范围 1 到 4），实际 3 件；1 件平台结算和官方数据对不上，没算",
        "· 定价一边倒：上线以来 2 件应验 1 件（要攒到 250 到 300 件才能下结论）",
    ]
    assert all(line.section == "score" and line.sub is None and line.expires is None for line in lines)
    assert lines[1].rows == ()
    assert lines[0].rows == ({"type": "score", "ts": "2029-04-08T23:30:00Z", "week": "2029-04-02", "n": 4,
                              "mismatches": 1, "expected": 2.46, "range": [1, 4], "actual": 3,
                              "lopsided": {"n": 2, "hit": 1}},)


def test_fed_mismatch_with_nyfed_is_not_scored():
    rows = [r for r in week_rows() if not (r["type"] == "witness" and r["series"] == "KXFEDDECISION")]
    rows.append(witness("KXFEDDECISION", "2029-04", -25.0))  # 官方是降息 25，平台说不变
    lines = weekly_lines(rows, MONDAY)
    assert lines[0].text == ("· 上周 2 件出了结果：应发生 1.5 件（正常范围 1 到 2），实际 2 件；"
                             "3 件平台结算和官方数据对不上，没算")


def test_weekly_only_on_monday_and_only_with_results():
    assert weekly_lines(week_rows(), datetime(2029, 4, 10, 7, 30, tzinfo=BJT)) == []
    # 北京时间周一早上，UTC 还是周日
    assert weekly_lines(week_rows(), datetime(2029, 4, 8, 23, 30, tzinfo=timezone.utc))
    assert weekly_lines([], MONDAY) == []
    only_mismatch = [r for r in week_rows() if "KXU3" in str(r.get("key", "")) or r["type"] == "witness"]
    assert [line.text for line in weekly_lines(only_mismatch, MONDAY)] == [
        "· 上周没有能算的结果；1 件平台结算和官方数据对不上，没算"]


def test_monthly_sentence_only_on_first_monday():
    rows = week_rows() + [
        shown(20, "KXRECSSNBER-29", "2029-03-05T23:35:00Z", "2029-03-09T13:25:00Z", p=90, series="KXRECSSNBER",
              period="2029-12"),
        settle(21, "KXRECSSNBER-29", "2029-03-10T00:00:00Z", 1),
        shown(22, "KXFED-29MAR-H25", "2029-03-19T23:35:00Z", "2029-03-21T17:59:00Z", p=30, series="KXFEDDECISION",
              period="2029-03"),
        settle(23, "KXFED-29MAR-H25", "2029-03-22T00:00:00Z", 0),
    ]
    first = datetime(2029, 4, 2, 7, 30, tzinfo=BJT)
    lines = weekly_lines(rows, first)
    # 上周（3 月 26 日到 4 月 1 日）只有 8% 那件；3 月一共 3 件：价格平均 42.7%，实际发生 66.7%
    assert [line.text for line in lines] == [
        "· 上周 1 件出了结果：应发生 0.1 件（正常范围 0 到 1），实际 1 件",
        "· 定价一边倒：上线以来 1 件应验 0 件（要攒到 250 到 300 件才能下结论）",
        "· 上月 3 件已结算，件数太少，先不算整体偏高还是偏低",
    ]
    month = lines[0].rows[0]["month"]
    assert month["period"] == "2029-03" and month["n"] == 3 and month["citl"] == pytest.approx(-0.24)
    assert set(month["corp"]) == {"n", "mcb", "dsc", "unc", "bs"}
    assert "month" not in weekly_lines(rows, MONDAY)[0].rows[0]


def test_monthly_sentence_when_prices_were_about_right():
    rows = [
        shown(1, "KXA-29MAR-X", "2029-03-05T23:35:00Z", "2029-03-09T13:25:00Z", p=50),
        settle(2, "KXA-29MAR-X", "2029-03-10T00:00:00Z", 1),
        shown(3, "KXA-29MAR-Y", "2029-03-05T23:35:00Z", "2029-03-09T13:25:00Z", p=50),
        settle(4, "KXA-29MAR-Y", "2029-03-10T00:00:00Z", 0),
    ]
    lines = weekly_lines(rows, datetime(2029, 4, 2, 7, 30, tzinfo=BJT))
    assert [line.text for line in lines] == ["· 上月 2 件已结算，件数太少，先不算整体偏高还是偏低"]
    assert lines[0].rows[0]["month"]["n"] == 2  # 件数少也照样记进 score 记录


def monthly_rows(n: int, yes: int) -> list[dict]:
    """3 月里 n 件价格都是 50% 的，其中 yes 件发生了。"""
    rows = []
    for i in range(n):
        key = f"KXA-29MAR-X{i}"
        rows.append(shown(2 * i + 1, key, "2029-03-05T23:35:00Z", "2029-03-09T13:25:00Z", p=50))
        rows.append(settle(2 * i + 2, key, "2029-03-10T00:00:00Z", 1 if i < yes else 0))
    return rows


def test_monthly_sentence_needs_enough_items():
    first = datetime(2029, 4, 2, 7, 30, tzinfo=BJT)
    # 20 件：价格平均 50%，实际发生 70%，价格整体偏低 20 个百分点
    assert [line.text for line in weekly_lines(monthly_rows(20, 14), first)] == [
        "· 上月 20 件已结算：价格整体偏低 20 个百分点（件数少，只作记录）"]
    assert [line.text for line in weekly_lines(monthly_rows(20, 10), first)] == [
        "· 上月 20 件已结算：价格整体和实际结果相差不到 1 个百分点（件数少，只作记录）"]
    assert [line.text for line in weekly_lines(monthly_rows(19, 14), first)] == [
        "· 上月 19 件已结算，件数太少，先不算整体偏高还是偏低"]


def test_ladder_interval_rebuilt_when_bounds_missing():
    """记录里没有 q25 / q75 时从 ks 和 s 重算：S = 0.9 / 0.58 / 0.17 的区间是 [0.2, 0.3]，概率 0.73。"""
    dist = {"ks": [0.1, 0.2, 0.3], "s": [0.9, 0.58, 0.17]}
    rows = [
        shown(1, "KXCPI-29MAR", "2029-04-02T23:35:00Z", "2029-04-04T12:30:00Z", dist=dist, series="KXCPI",
              period="2029-03"),
        settle(2, "KXCPI-29MAR", "2029-04-04T13:00:00Z", 0.4),
    ]
    lines = weekly_lines(rows, MONDAY)
    assert lines[0].text == "· 上周 1 件出了结果：应发生 0.7 件（正常范围 0 到 1），实际 0 件"
    assert lines[0].rows[0]["expected"] == pytest.approx(0.73)
