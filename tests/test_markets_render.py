from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from digest.markets import render
from digest.markets.render import band, fmt_value, pct, render_backtest, render_panel
from digest.markets.types import Line

# 全是编出来的行和数字，只用来核对排版
BJT = ZoneInfo("Asia/Shanghai")
NOW = datetime(2031, 9, 8, 7, 30, tzinfo=BJT)
G, E = "<font color=#999999>", "</font>"
HEAD16 = "0f1e2d3c4b5a6978"

CPI = "· 9月9日 周二 20:30 美国8月CPI 环比：中位数 0.2%（一半把握 0.1% 到 0.3%）；克利夫兰联储模型 0.21%"
CPI_SUB = "过去 12 次同一时刻（提前 1 天）：中位数平均差 0.07%"
FED = "· 9月17日 周三 02:00 美联储议息：维持不变 很可能（88%），降息 25 个基点 12%"
FED_SUB = "过去 6 次同一时刻：市场最看好的结果对了 5 次"
GDP = "· 2032 年美国实际 GDP 连续两季负增长：很不可能（14%）"
LOPSIDED = "· 示例国议会年底前会通过假想法案吗：很可能（91%）· 10月20日结算"
MOVER = "· 示例央行 10 月会降息吗：64%（一天 +9 个百分点）"
MOVER_SUB = "相关新闻：今天第 3 条"
VIX = "· VIX 17.3（9月5日收盘，长期平均约 20）：标普500 一天的涨跌约有三分之二的机会在 ±1.1% 以内"
VIX9D = "· 未来 9 天的预期波动（19.0）高于未来 30 天，市场觉得近期风险更大"
SCORE = "· 上周 4 件出了结果：应发生 3.1 件（正常范围 2 到 4），实际 3 件"
NOTE = "Kalshi 的非农合约这次没取到"
INTRO = f"{G}预测市场的价格是参与者用钱押出来的概率，不是机器人的判断{E}"
BASIS = "结算依据：CPI、就业数据按劳工统计局首次公布值（季调）；议息按美联储声明"
DISCLAIMER = "仅供个人研究，不构成投资建议｜程序和大模型自动生成，可能有误"
LINE = "  \n"


def morning_lines() -> list[Line]:
    # 故意打乱顺序：排版按板块的固定顺序，同一板块内保持传入顺序
    return [
        Line("vix", VIX),
        Line("key", CPI, sub=CPI_SUB),
        Line("note", NOTE),
        Line("key", FED, sub=FED_SUB),
        Line("lopsided", LOPSIDED),
        Line("weekly", GDP),
        Line("score", SCORE),
        Line("vix", VIX9D),
    ]


def test_morning_panel_exact_text():
    message = render_panel(morning_lines(), "morning", NOW, HEAD16, cleveland=True)
    expected = "\n\n".join([
        "### 📈 市场与预测",
        INTRO,
        LINE.join(["**接下来的关键数据（北京时间）**", CPI, f"{G}{CPI_SUB}{E}", FED, f"{G}{FED_SUB}{E}"]),
        LINE.join(["**每周一看**", GDP]),
        LINE.join(["**定价一边倒（Polymarket）**", LOPSIDED]),
        LINE.join(["**波动率（Cboe）**", VIX, VIX9D]),
        LINE.join(["**上周对答案**", SCORE]),
        f"{G}{NOTE}{E}",
        "---",
        LINE.join([
            f"{G}{BASIS}｜通胀模型来自克利夫兰联储{E}",
            f"{G}{DISCLAIMER}｜数据截至 07:30｜账本 #{HEAD16}{E}",
        ]),
    ])
    assert message.title == "市场与预测"
    assert message.text == expected
    assert len(message.text.encode()) < render.MAX_PANEL_BYTES


def test_evening_panel_exact_text():
    now = datetime(2031, 9, 9, 11, 45, tzinfo=timezone.utc)  # 北京时间 19:45
    lines = [Line("note", NOTE), Line("mover", MOVER, sub=MOVER_SUB), Line("key", CPI, sub=CPI_SUB)]
    message = render_panel(lines, "evening", now, None, cleveland=False)
    expected = "\n\n".join([
        "### 📈 今晚美国数据",
        INTRO,
        LINE.join(["**接下来的关键数据（北京时间）**", CPI, f"{G}{CPI_SUB}{E}"]),
        LINE.join(["**过去一天的变化（Polymarket）**", MOVER, f"{G}{MOVER_SUB}{E}"]),
        f"{G}{NOTE}{E}",
        "---",
        LINE.join([f"{G}{BASIS}{E}", f"{G}{DISCLAIMER}｜数据截至 19:45｜账本未记录{E}"]),
    ])
    assert message.title == "今晚美国数据"
    assert message.text == expected


def test_preview_prefix():
    message = render_panel([Line("key", CPI)], "evening", NOW, HEAD16, cleveland=False, preview=True)
    assert message.title == "【预览】今晚美国数据"
    assert message.text.startswith("### 📈 【预览】今晚美国数据\n\n")
    assert render_panel([Line("vix", VIX)], "morning", NOW, None, False, preview=True).title == "【预览】市场与预测"


def test_nothing_to_send():
    assert render_panel([], "morning", NOW, HEAD16, False) is None
    assert render_panel([Line("note", NOTE)], "morning", NOW, HEAD16, False) is None
    # 晚间版只在今晚有数据公布或有显示的价格变化时才发
    evening_extras = [Line("vix", VIX), Line("lopsided", LOPSIDED), Line("score", SCORE), Line("note", NOTE)]
    assert render_panel(evening_extras, "evening", NOW, HEAD16, False) is None
    assert render_panel([Line("mover", MOVER)], "evening", NOW, HEAD16, False) is not None
    # 不认识的板块丢掉
    assert render_panel([Line("chat", "· 不该出现")], "morning", NOW, HEAD16, False) is None
    message = render_panel([Line("chat", "· 不该出现"), Line("vix", VIX)], "morning", NOW, HEAD16, False)
    assert "不该出现" not in message.text


def test_footer_ledger_head():
    def footer(head16):
        return render_panel([Line("vix", VIX)], "morning", NOW, head16, False).text.rsplit(LINE, 1)[1]

    assert footer(HEAD16).endswith(f"｜账本 #{HEAD16}{E}")
    for bad in (None, "", "0f1e2d3c", HEAD16.upper(), HEAD16 + "00", "](http://x.invalid)abcde"):
        assert footer(bad).endswith(f"｜账本未记录{E}")

    def first(head16):
        message = render_panel([Line("vix", VIX)], "morning", NOW, head16, False, first_entry=True)
        return message.text.rsplit(LINE, 1)[1]

    # 账本还是空的、这次推送后才开始记；已经有链头时照常印链头
    assert first(None).endswith(f"｜账本从这一条开始记{E}")
    assert first(HEAD16).endswith(f"｜账本 #{HEAD16}{E}")
    # 不带时区的时间按北京时间处理
    assert "数据截至 07:30" in footer(None)
    assert "数据截至 07:30" in render_panel([Line("vix", VIX)], "morning", NOW.replace(tzinfo=None), None, False).text


def test_size_limit_trims_notes_then_subs_then_lines(monkeypatch):
    lines = [Line("key", CPI, sub=CPI_SUB), Line("key", FED, sub=FED_SUB), Line("vix", VIX), Line("note", NOTE)]

    def size(ls):
        return len(render_panel(ls, "morning", NOW, HEAD16, False).text.encode())

    full = render_panel(lines, "morning", NOW, HEAD16, False).text
    no_note = lines[:3]
    no_note_no_last_sub = [lines[0], Line("key", FED), lines[2]]

    monkeypatch.setattr(render, "MAX_PANEL_BYTES", size(no_note))
    text = render_panel(lines, "morning", NOW, HEAD16, False).text
    assert NOTE not in text and CPI_SUB in text and FED_SUB in text and VIX in text

    monkeypatch.setattr(render, "MAX_PANEL_BYTES", size(no_note_no_last_sub))
    text = render_panel(lines, "morning", NOW, HEAD16, False).text
    assert CPI_SUB in text and FED_SUB not in text and VIX in text

    monkeypatch.setattr(render, "MAX_PANEL_BYTES", size([Line("key", CPI), Line("key", FED)]))
    text = render_panel(lines, "morning", NOW, HEAD16, False).text
    assert CPI in text and FED in text and VIX not in text and CPI_SUB not in text and FED_SUB not in text
    assert len(text.encode()) <= render.MAX_PANEL_BYTES < len(full.encode())

    # 什么都放不下时不发
    monkeypatch.setattr(render, "MAX_PANEL_BYTES", 100)
    assert render_panel(lines, "morning", NOW, HEAD16, False) is None


def test_default_limit_keeps_a_long_panel_small():
    lines = [Line("key", CPI + f" {n}", sub=CPI_SUB) for n in range(40)] + [Line("note", NOTE)]
    message = render_panel(lines, "morning", NOW, HEAD16, True)
    assert len(message.text.encode()) <= render.MAX_PANEL_BYTES
    assert f"{CPI} 0" in message.text and message.text.endswith(f"账本 #{HEAD16}{E}")


@pytest.mark.parametrize("p, expected", [
    (0.0, 0), (0.004, 0), (0.005, 1), (0.145, 15), (0.285, 29), (0.575, 58), (0.5, 50),
    ((0.41 + 0.42) / 2, 42), (0.999, 100), (1.0, 100), (1.3, 100), (-0.2, 0),
])
def test_pct_rounds_half_up(p, expected):
    assert pct(p) == expected


def test_pct_rejects_nan():
    with pytest.raises(ValueError):
        pct(float("nan"))


@pytest.mark.parametrize("p, word", [
    (0, "几乎不可能"), (5, "几乎不可能"), (6, "很不可能"), (20, "很不可能"), (21, "不太可能"), (44, "不太可能"),
    (45, "大致五五开"), (55, "大致五五开"), (56, "较可能"), (79, "较可能"), (80, "很可能"), (94, "很可能"),
    (95, "几乎确定"), (100, "几乎确定"),
])
def test_band_words(p, word):
    assert band(p) == word


def test_band_uses_rules_from_settings():
    bands = ((50, "低"), (100, "高"))
    assert (band(50, bands), band(51, bands), band(120, bands)) == ("低", "高", "高")


@pytest.mark.parametrize("kind, value, text", [
    ("%", 0.3, "0.3%"), ("pct", 0.3, "0.3%"), ("%", -0.1, "−0.1%"), ("%", 0.0, "0.0%"), ("%", -0.0, "0.0%"),
    ("%", 4.0, "4.0%"), ("%", 4.25, "4.25%"), ("%", 0.1 + 0.2, "0.3%"),
    ("jobs", 110_000, "+11 万"), ("payrolls", -25_000, "−2.5 万"), ("jobs", 0, "0 万"), ("jobs", -400, "0 万"),
    ("jobs", 4_950, "+0.5 万"), ("jobs", 123_456, "+12.3 万"), ("jobs", 1_000_000, "+100 万"),
])
def test_fmt_value(kind, value, text):
    assert fmt_value(kind, value) == text


SUMMARY = {
    "version": 1,
    "partial": True,
    "requests": 777,
    "skipped": 2,
    "series": {
        "KXCPI": {"kind": "ladder", "unit": "pct", "labels": {
            "E0": {"n": 17, "mae_median": 0.0612, "covered50": 9, "mass50_mean": 0.52, "rps_mean": 0.11},
            "M3": {"n": 15, "mae_median": 0.084, "covered50": 7},
        }},
        "KXFEDDECISION": {"labels": {"E0": {"n": 7, "top_hit": 6, "p_actual_mean": 0.835}, "M3": {"n": 7, "top_hit": 5}}},
        "KXPAYROLLS": {"labels": {"M1": {"n": 4, "mae_median": 46_000, "covered50": 2}, "M5": {"n": 0}}},
        "KXU3": {"labels": {"E0": {"n": 0}}},  # 没有算出来的不显示
        "KXNEWTHING": {"name": "新的[合约]", "E0": {"n": 3, "mae_median": 1.5}},  # 系列这一层直接放成绩也认
    },
    "bands": {"band_hi": {"n": 88, "yes": 80}, "band_lo": {"n": 50, "yes": 4}},
}


def test_backtest_exact_text():
    message = render_backtest(SUMMARY, NOW)
    expected = "\n\n".join([
        "### 📈 【回测】Kalshi 过去的成绩",
        f"{G}按推送的时刻重算 Kalshi 已结算合约的价格，再和实际公布值对答案{E}",
        LINE.join([
            "· 美联储议息（7 次）：公布前一晚 19:30 市场最看好的结果对了 6 次，实际结果的平均价格 84%；提前 3 天早上对了 5 次",
            "· CPI 环比（17 次）：公布当晚 19:30 中位数平均差 0.06 个百分点，一半把握区间命中 9 次；提前 3 天早上平均差 0.08 个百分点",
            "· 非农新增就业（4 次）：提前 1 天早上中位数平均差 4.6 万人，一半把握区间命中 2 次",
            "· 新的［合约］（3 次）：公布当晚 19:30 中位数平均差 1.50",
            "· 定价一边倒的合约：价格在 85% 到 97% 的 88 个，实际发生 80 个（91%）；价格在 3% 到 15% 的 50 个，实际发生 4 个（8%）",
        ]),
        "---",
        f"{G}请求 777 次｜跳过 2 次（结算不清楚）｜部分完成｜数据截至 07:30｜仅供个人研究，不构成投资建议{E}",
    ])
    assert message.title == "【回测】Kalshi 过去的成绩"
    assert message.text == expected


@pytest.mark.parametrize("summary", [
    {}, None, {"series": "坏"}, {"series": {"KXCPI": "坏", "KXU3": {"labels": {"E0": {"n": True, "mae_median": "x"}}}}},
    {"series": {"KXCPI": {"E0": {"n": 3, "mae_median": float("nan")}}}, "bands": {"band_hi": {"n": 3, "yes": 9}}},
    {"partial": "false", "requests": "12"},
])
def test_backtest_tolerates_bad_summary(summary):
    message = render_backtest(summary, NOW)
    assert "· 这次没有算出结果" in message.text
    assert "部分完成" not in message.text and "请求" not in message.text
