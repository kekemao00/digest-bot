from __future__ import annotations

import json
import logging
import math
from datetime import date

import httpx
import pytest

from digest.markets.net import Budget, BudgetExceeded, FetchError, Fetcher
from digest.markets.sources import (
    Close,
    Nowcast,
    bls_series,
    cboe_last_close,
    cleveland_nowcast,
    first_print,
    nyfed_target_upper,
)

# 全是编造的数字、日期、地址和序列代码，字段名和格式照 contracts/official.md
HOST = "data.example-source.test"
NOWCAST_URL = f"https://{HOST}/webcharts/nowcast_month.json"
CSV_URL = f"https://{HOST}/daily_prices/FAKE_History.csv"
BLS_BASE = f"https://{HOST}/publicAPI/v1/timeseries/data/"
NYFED_URL = f"https://{HOST}/api/rates/unsecured/effr/last/10.json"
SERIES = "ZZFAKE0000001"


def make_fetcher(handler, *, requests: int = 10) -> tuple[Fetcher, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    return Fetcher(client, Budget(60.0, requests), sleep=lambda s: None, jitter=lambda: 0.0), calls


def serve_json(payload) -> tuple[Fetcher, list[httpx.Request]]:
    return make_fetcher(lambda request: httpx.Response(200, json=payload))


def serve_text(text: str, **headers) -> tuple[Fetcher, list[httpx.Request]]:
    return make_fetcher(lambda request: httpx.Response(200, content=text.encode("utf-8"), headers=headers))


# ---- 克利夫兰联储 ----


def chart(subcaption, labels, series: dict[str, list]) -> dict:
    """一个目标月的图表配置。labels 里写 "VLINE" 的位置是数据公布的竖线。"""
    category = [{"vline": "true", "label": "CPI Xyz"} if label == "VLINE" else {"label": label} for label in labels]
    return {
        "chart": {"caption": "编造的标题", "subcaption": subcaption},
        "categories": [{"category": category}],
        "dataset": [{"seriesname": name, "data": [{"value": v} for v in values]} for name, values in series.items()],
    }


def test_nowcast_takes_last_value_and_skips_vlines():
    payload = [
        chart(
            "2031-3",  # 月份没补零也认
            ["3/02", "03/09", "VLINE", "3/16"],
            {
                " CPI Inflation ": ["0.1100", "0.1234", ""],  # 最后一个是空的，取前一个
                "Core CPI Inflation": [0.21, 0.205, 0.199],  # 数字也认
                "PCE Inflation": ["0.9", "0.9", "0.9"],
                "Actual CPI Inflation": ["", "", ""],
            },
        )
    ]
    fetcher, calls = serve_json(payload)
    result = cleveland_nowcast(fetcher, NOWCAST_URL)
    # 两个数的日期不同时取较早的那个
    assert result == {(2031, 3): Nowcast(cpi=0.1234, core=0.199, as_of=date(2031, 3, 9))}
    assert len(calls) == 1 and calls[0].url == NOWCAST_URL


def test_nowcast_infers_years_across_new_year():
    payload = [
        chart("2030-12", ["12/20", "12/27", "VLINE", "01/03", "01/10"],
              {"CPI Inflation": ["0.31", "0.32", "0.33", "0.34"], "Core CPI Inflation": ["0.41", "0.42", "0.43", "0.44"]}),
        # 预测从目标月的前一个月就开始了：第一个点仍然归到离目标月最近的那一年
        chart("2031-01", ["12/29", "01/05"], {"CPI Inflation": ["0.51", "0.52"], "Core CPI Inflation": ["", ""]}),
        chart("2032-02", ["02/27", "02/29", "03/02"], {"CPI Inflation": ["0.61", "0.62", "0.63"]}),
    ]
    fetcher, _ = serve_json(payload)
    result = cleveland_nowcast(fetcher, NOWCAST_URL)
    assert result[(2030, 12)] == Nowcast(cpi=0.34, core=0.44, as_of=date(2031, 1, 10))
    assert result[(2031, 1)] == Nowcast(cpi=0.52, core=None, as_of=date(2031, 1, 5))
    assert result[(2032, 2)] == Nowcast(cpi=0.63, core=None, as_of=date(2032, 3, 2))


def test_nowcast_skips_bad_targets_and_keeps_the_rest(caplog):
    good = chart("2030-05", ["05/06", "05/13"], {"CPI Inflation": ["0.71", "0.72"], "Core CPI Inflation": ["0.81", "0.82"]})
    payload = [
        "不是对象",
        chart("2030:Q2", ["04/01"], {"CPI Inflation": ["9.99"]}),  # 季度图表混进来了
        chart("2030-13", ["01/02"], {"CPI Inflation": ["9.99"]}),
        chart("2030-06", ["06/03", "06/10"], {"CPI Inflation": ["9.99"]}),  # 数据点比日期少
        chart("2030-07", ["07/14", "07/07"], {"CPI Inflation": ["9.99", "9.99"]}),  # 日期倒退
        chart("2030-08", ["08/05", "02/30"], {"CPI Inflation": ["9.99", "9.99"]}),  # 不存在的日期
        chart("2030-09", ["09/01", "x"], {"CPI Inflation": ["9.99", "9.99"]}),
        {"chart": {"subcaption": "2030-10"}, "categories": "坏的", "dataset": []},
        good,
    ]
    caplog.set_level(logging.WARNING, logger="digest.markets")
    fetcher, _ = serve_json(payload)
    result = cleveland_nowcast(fetcher, NOWCAST_URL)
    assert result == {(2030, 5): Nowcast(cpi=0.72, core=0.82, as_of=date(2030, 5, 13))}
    assert "8 个目标月的格式不对" in caplog.text
    assert "9.99" not in caplog.text and HOST not in caplog.text


def test_nowcast_without_values_is_left_out():
    payload = [
        # 只有公布值没有预测值，或者全是空值：这个目标月不收
        chart("2030-04", ["04/01", "04/08"], {"Actual CPI Inflation": ["", "0.5"], "CPI Inflation": ["", " "]}),
        chart("2030-05", ["05/06"], {"PCE Inflation": ["0.3"]}),
        chart("2030-06", [], {"CPI Inflation": []}),
        chart("2030-07", ["07/06"], {"CPI Inflation": ["NaN"], "Core CPI Inflation": [None]}),
    ]
    fetcher, _ = serve_json(payload)
    assert cleveland_nowcast(fetcher, NOWCAST_URL) == {}


def test_nowcast_duplicate_target_keeps_newer():
    payload = [
        chart("2030-05", ["05/06", "05/13"], {"CPI Inflation": ["0.11", "0.12"]}),
        chart("2030-05", ["05/06", "05/20"], {"CPI Inflation": ["0.21", "0.22"]}),
        chart("2030-05", ["05/06"], {"CPI Inflation": ["0.31"]}),
    ]
    fetcher, _ = serve_json(payload)
    assert cleveland_nowcast(fetcher, NOWCAST_URL)[(2030, 5)] == Nowcast(cpi=0.22, core=None, as_of=date(2030, 5, 20))


def test_nowcast_tolerates_bom_and_odd_content_type():
    body = b"\xef\xbb\xbf" + json.dumps(chart("2030-05", ["05/06"], {"CPI Inflation": ["0.15"]})).encode()
    fetcher, _ = make_fetcher(lambda request: httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"}))
    # 顶层只有一个对象时也当作一个目标月
    assert cleveland_nowcast(fetcher, NOWCAST_URL) == {(2030, 5): Nowcast(cpi=0.15, core=None, as_of=date(2030, 5, 6))}


def test_nowcast_rejects_wrong_top_level():
    fetcher, _ = serve_json("不是列表")
    with pytest.raises(ValueError) as caught:
        cleveland_nowcast(fetcher, NOWCAST_URL)
    assert "不是列表" not in str(caught.value)


def test_network_errors_propagate():
    fetcher, calls = make_fetcher(lambda request: httpx.Response(503))
    with pytest.raises(FetchError) as caught:
        cleveland_nowcast(fetcher, NOWCAST_URL)
    assert caught.value.kind == "5xx" and len(calls) == 2  # 重试过一次
    fetcher, _ = make_fetcher(lambda request: httpx.Response(200, json=[]), requests=0)
    with pytest.raises(BudgetExceeded):
        cleveland_nowcast(fetcher, NOWCAST_URL)


# ---- Cboe ----


def test_cboe_last_non_empty_row():
    text = (
        "DATE,OPEN,HIGH,LOW,CLOSE\r\n"
        "01/04/2030,21.500000,22.000000,20.100000,21.010000\r\n"
        "1/7/2030,21.010000,23.400000,20.900000,23.125000\r\n"
        "01/08/2030,23.100000,23.500000,22.000000,\r\n"  # 当天还没有收盘值
        "01/09/2030,0,0,0,0\r\n"
        "\r\n"
    )
    fetcher, calls = serve_text(text, **{"content-type": "text/csv"})
    assert cboe_last_close(fetcher, CSV_URL) == Close(day=date(2030, 1, 7), value=23.125)
    assert len(calls) == 1


def test_cboe_bom_and_short_rows():
    text = "﻿DATE,OPEN,HIGH,LOW,CLOSE\n03/01/2030,1,1,1,14.5\n03/04/2030,1,1\n13/45/2030,1,1,1,15\n"
    fetcher, _ = serve_text(text)
    assert cboe_last_close(fetcher, CSV_URL) == Close(day=date(2030, 3, 1), value=14.5)


def test_cboe_empty_or_header_only():
    for text in ("", "\n\n", "DATE,OPEN,HIGH,LOW,CLOSE\n"):
        fetcher, _ = serve_text(text)
        assert cboe_last_close(fetcher, CSV_URL) is None


def test_cboe_wrong_header_raises_without_content():
    fetcher, _ = serve_text("<html>编造的页面 42.42</html>\n")
    with pytest.raises(ValueError) as caught:
        cboe_last_close(fetcher, CSV_URL)
    assert "42.42" not in str(caught.value)


# ---- 劳工统计局 ----


def bls_payload(data: list[dict], *, as_list: bool = False, status: str = "REQUEST_SUCCEEDED", message=()) -> dict:
    series = {"seriesID": SERIES, "data": data}
    results = [{"series": [series]}] if as_list else {"series": [series]}
    return {"status": status, "responseTime": 12, "message": list(message), "Results": results}


FAKE_DATA = [
    {"year": "2030", "period": "M02", "periodName": "February", "latest": "true", "value": "250.500", "footnotes": [{}]},
    {"year": "2030", "period": "M01", "periodName": "January", "value": "249.000", "footnotes": [{}]},
    {"year": "2029", "period": "M13", "periodName": "Annual", "value": "245.000", "footnotes": [{}]},
    {"year": "2029", "period": "M12", "periodName": "December", "value": "248.000", "footnotes": [{}]},
    {"year": "2029", "period": "M11", "periodName": "November", "value": "-", "footnotes": [{"text": "编造的说明"}]},
    {"year": "2029", "period": "M10", "periodName": "October", "value": "247.250", "footnotes": [{"code": "P"}]},
    {"year": "2029", "period": "S01", "value": "1.0"},
    {"year": "x", "period": "M09", "value": "1.0"},
    "不是对象",
]
FAKE_SERIES = {(2030, 2): 250.5, (2030, 1): 249.0, (2029, 12): 248.0, (2029, 10): 247.25}


def test_bls_series_object_results():
    fetcher, calls = serve_json(bls_payload(FAKE_DATA))
    assert bls_series(fetcher, SERIES, base=BLS_BASE) == FAKE_SERIES
    request = calls[0]
    # 一次不带参数的 GET，地址就是前缀加序列代码，不带任何个人信息
    assert request.method == "GET" and str(request.url) == BLS_BASE + SERIES
    assert not request.url.query and not request.content


def test_bls_series_list_results_and_base_without_slash():
    fetcher, calls = serve_json(bls_payload(FAKE_DATA, as_list=True))
    assert bls_series(fetcher, SERIES, base=BLS_BASE.rstrip("/")) == FAKE_SERIES
    assert str(calls[0].url) == BLS_BASE + SERIES
    # 文档示例里列表元素直接是序列本身
    fetcher, _ = serve_json({"status": "REQUEST_SUCCEEDED", "Results": [{"seriesID": SERIES, "data": FAKE_DATA}]})
    assert bls_series(fetcher, SERIES, base=BLS_BASE) == FAKE_SERIES


def test_bls_series_picks_matching_series():
    other = {"seriesID": "ZZOTHER", "data": [{"year": "2030", "period": "M01", "value": "1.0"}]}
    mine = {"seriesID": SERIES, "data": [{"year": "2030", "period": "M01", "value": "2.0"}]}
    fetcher, _ = serve_json({"status": "REQUEST_SUCCEEDED", "Results": {"series": [other, mine]}})
    assert bls_series(fetcher, SERIES, base=BLS_BASE) == {(2030, 1): 2.0}


def test_bls_default_base_is_v1():
    fetcher, calls = serve_json(bls_payload(FAKE_DATA))
    bls_series(fetcher, SERIES)
    assert str(calls[0].url) == "https://api.bls.gov/publicAPI/v1/timeseries/data/" + SERIES


def test_bls_not_processed_raises_without_message():
    payload = bls_payload([], status="REQUEST_NOT_PROCESSED", message=[f"编造的拒绝说明 {SERIES}"])
    fetcher, _ = serve_json(payload)
    with pytest.raises(ValueError) as caught:
        bls_series(fetcher, SERIES, base=BLS_BASE)
    assert SERIES not in str(caught.value) and "编造" not in str(caught.value)


def test_bls_invalid_series_returns_empty(caplog):
    caplog.set_level(logging.WARNING, logger="digest.markets")
    fetcher, _ = serve_json(bls_payload([], message=[f"Invalid Series for Series {SERIES}"]))
    assert bls_series(fetcher, SERIES, base=BLS_BASE) == {}
    assert "没有可用的月度数据" in caplog.text and SERIES not in caplog.text
    for broken in ({"status": "REQUEST_SUCCEEDED"}, {"Results": {"series": "坏的"}}, {"Results": []}):
        fetcher, _ = serve_json(broken)
        assert bls_series(fetcher, SERIES, base=BLS_BASE) == {}
    fetcher, _ = serve_json(["不是对象"])
    with pytest.raises(ValueError):
        bls_series(fetcher, SERIES, base=BLS_BASE)


def test_bls_rejects_odd_series_id_before_any_request():
    fetcher, calls = serve_json(bls_payload(FAKE_DATA))
    for bad in ("../x", "zz fake", "ZZ?x=1", ""):
        with pytest.raises(ValueError):
            bls_series(fetcher, bad, base=BLS_BASE)
    assert calls == []


def test_first_print_cpi_rounds_half_up_from_three_decimals(caplog):
    caplog.set_level(logging.INFO, logger="digest.markets")
    data = {(2030, 1): 200.0, (2030, 2): 200.5, (2030, 3): 201.3, (2030, 4): 201.29, (2029, 12): 199.0}
    # 正好 0.25%：四舍五入是 0.3（浮点直接算会得到 0.2499…，银行家舍入也是 0.2）
    assert first_print("cpi", data, (2030, 2)) == 0.3
    assert first_print("core", data, (2030, 2)) == 0.3
    assert "进位的边界" in caplog.text and "0.25" not in caplog.text
    # 201.3 / 200.5 - 1 = 0.399…% -> 0.4
    assert first_print("cpi", data, (2030, 3)) == 0.4
    # 1 月和上一年 12 月比：200 / 199 - 1 = 0.5025…% -> 0.5
    assert first_print("cpi", data, (2030, 1)) == 0.5
    # 很小的负数四舍五入成 0，显示成 0.0 而不是 -0.0
    value = first_print("cpi", data, (2030, 4))
    assert value == 0.0 and math.copysign(1.0, value) == 1.0


def test_first_print_negative_and_missing_months():
    data = {(2030, 5): 210.0, (2030, 6): 209.475, (2030, 8): 211.0}
    assert first_print("cpi", data, (2030, 6)) == -0.3  # 正好 -0.25%，边界上往远离 0 的方向进
    assert first_print("cpi", data, (2030, 8)) is None  # 缺 7 月
    assert first_print("cpi", data, (2030, 9)) is None  # 缺本月
    assert first_print("cpi", {(2030, 1): 0.0, (2030, 2): 1.0}, (2030, 2)) is None


def test_first_print_payrolls_and_u3():
    data = {(2030, 1): 160100.0, (2030, 2): 160223.0, (2030, 3): 160178.0}
    assert first_print("payrolls", data, (2030, 2)) == 123000.0
    assert first_print("payrolls", data, (2030, 3)) == -45000.0
    assert first_print("payrolls", data, (2030, 1)) is None
    assert first_print("u3", {(2030, 2): 4.7}, (2030, 2)) == 4.7
    assert first_print("u3", {(2030, 2): 4.7}, (2030, 3)) is None
    assert first_print("unknown", data, (2030, 2)) is None
    assert first_print("cpi", {(2030, 1): float("nan"), (2030, 2): 1.0}, (2030, 2)) is None


def test_first_print_with_parsed_bls_series():
    fetcher, _ = serve_json(bls_payload(FAKE_DATA))
    data = bls_series(fetcher, SERIES, base=BLS_BASE)
    assert first_print("cpi", data, (2030, 2)) == 0.6  # 250.5 / 249 - 1 = 0.6024…%
    assert first_print("cpi", data, (2029, 12)) is None  # 11 月是 "-"


# ---- 纽约联储 ----


def test_nyfed_target_upper_sorted_by_day():
    payload = {
        "refRates": [
            {"effectiveDate": "2030-06-14", "type": "EFFR", "percentRate": 5.11, "targetRateFrom": 5.0, "targetRateTo": 5.25},
            {"effectiveDate": "2030-06-14", "type": "OBFR", "percentRate": 5.1},  # 没有目标区间
            {"effectiveDate": "2030-06-13", "type": "EFFR", "targetRateFrom": "4.75", "targetRateTo": "5.00"},
            {"effectiveDate": "2030-06-13", "type": "EFFR", "targetRateTo": 9.99},  # 同一天重复，留第一条
            {"effectiveDate": "2030-06-12", "type": "EFFR"},  # 缺上限
            {"effectiveDate": "坏日期", "type": "EFFR", "targetRateTo": 1.0},
            {"effectiveDate": "2030-06-11", "targetRateTo": 5.0},  # 没写类型也认
            "不是对象",
        ]
    }
    fetcher, calls = serve_json(payload)
    assert nyfed_target_upper(fetcher, NYFED_URL) == [
        (date(2030, 6, 11), 5.0),
        (date(2030, 6, 13), 5.0),
        (date(2030, 6, 14), 5.25),
    ]
    assert len(calls) == 1


def test_nyfed_wrong_shape():
    fetcher, _ = serve_json({"refRates": []})
    assert nyfed_target_upper(fetcher, NYFED_URL) == []
    for broken in ({"other": 1}, [1, 2]):
        fetcher, _ = serve_json(broken)
        with pytest.raises(ValueError):
            nyfed_target_upper(fetcher, NYFED_URL)
