"""市场板块的中文排版：概率用词、数值格式，以及板块和回测的钉钉消息（设计文档 5.1、5.7、6、8）。

各行的正文由 select / score 用固定模板拼好（外部文本已经过 md_text 和 truncate），这里只负责
按板块排列、加标题和页脚。消息只发到钉钉，这里的任何文本都不能写进日志或 Summary。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from digest.markets.schedule import BJT
from digest.markets.settings import DEFAULT_BANDS, DEFAULT_KEY_DATA, Lopsided
from digest.markets.types import Line
from digest.render.dingtalk import BREAK, LINE, Message, grey
from digest.render.text import md_text, truncate

ICON = "📈"
TITLES = {"morning": "市场与预测", "evening": "今晚美国数据"}
PREVIEW = "【预览】"
BACKTEST_TITLE = "【回测】Kalshi 过去的成绩"
INTRO = "预测市场的价格是参与者用钱押出来的概率，不是机器人的判断"
BACKTEST_INTRO = "按推送的时刻重算 Kalshi 已结算合约的价格，再和实际公布值对答案"
# 板块的显示顺序和标题；note 是没有标题的灰色附注，放在最后
SECTIONS = (
    ("key", "接下来的关键数据（北京时间）"),
    ("weekly", "每周一看"),
    ("lopsided", "定价一边倒（Polymarket）"),
    ("mover", "过去一天的变化（Polymarket）"),
    ("vix", "波动率（Cboe）"),
    ("score", "上周对答案"),
    ("note", None),
)
ORDER = {key: n for n, (key, _) in enumerate(SECTIONS)}
# 晚间版只在今晚有数据公布（或有显示的价格变化）时才发
EVENING_SECTIONS = ("key", "mover")
SETTLEMENT = "结算依据：CPI、就业数据按劳工统计局首次公布值（季调）；议息按美联储声明"
CLEVELAND = "通胀模型来自克利夫兰联储"
DISCLAIMER = "仅供个人研究，不构成投资建议｜程序和大模型自动生成，可能有误"
NO_LEDGER = "账本未记录"
# 账本还是空的、这次推送成功后才开始记：第一次正式推送时页脚这样写
FIRST_ENTRY = "账本从这一条开始记"
# 板块自己的长度上限，远低于钉钉的 MAX_BYTES；超出时先删附注，再删灰色小字，最后从末尾删行
MAX_PANEL_BYTES = 6000
MINUS = "\u2212"  # 负数用数学减号“−”，和中文排版更协调

_HEAD16 = re.compile(r"[0-9a-f]{16}")
_LABEL = re.compile(r"^(E0|M[0-7])$")


def pct(p: float) -> int:
    """概率换成整数百分比，四舍五入（0.5 进位）。

    先取 p 的最短十进制表示再乘 100，避免 0.145 * 100 = 14.4999… 这样的浮点误差把 14.5 舍成 14。
    """
    if not math.isfinite(p):
        raise ValueError("概率不是有限的数")
    value = int((Decimal(str(p)) * 100).quantize(Decimal("1"), ROUND_HALF_UP))
    return max(0, min(100, value))


def band(p_int: int, bands: Sequence[tuple[int, str]] = DEFAULT_BANDS) -> str:
    """整数百分比对应的概率用词（5.1）。bands 是规则文件里的 (上限, 用词)，按上限从小到大排。"""
    bands = bands or DEFAULT_BANDS
    for upper, word in bands:
        if p_int <= upper:
            return word
    return bands[-1][1]


def fmt_value(kind: str, v: float) -> str:
    """公布值的显示格式。

    百分比（"%" 或 "pct"）："0.3%"、"−0.1%"、"4.25%"（最多两位小数，至少一位）；
    人数（"jobs" 或 "payrolls"，v 是人数）："+11 万"、"−2.5 万"（一位小数，末尾的 .0 去掉）。
    """
    if kind in ("%", "pct"):
        value = _half_up(v, "0.01")
        if value == value.quantize(Decimal("0.1")):
            value = value.quantize(Decimal("0.1"))
        return _signed(value, plus=False) + "%"
    if kind in ("jobs", "payrolls"):
        return _signed(_wan(v), plus=True) + " 万"
    value = _half_up(v, "0.01")
    return _signed(value.quantize(Decimal("1")) if value == value.to_integral_value() else value.normalize(), plus=False)


def render_panel(
    lines: list[Line],
    edition: str,
    now: datetime,
    head16: str | None,
    cleveland: bool,
    preview: bool = False,
    first_entry: bool = False,
) -> Message | None:
    """把各行排成一条钉钉消息。没有可发的内容时返回 None：只有附注，或晚间版今晚没有数据公布。

    板块按 SECTIONS 的顺序排，同一板块内保持传入的顺序；不认识的板块丢掉。
    超过 MAX_PANEL_BYTES 时会删掉一部分内容，调用方可以用 line.text in message.text 判断哪些行发出去了。
    """
    kept = sorted((line for line in lines if line.section in ORDER), key=lambda line: ORDER[line.section])
    title = (PREVIEW if preview else "") + TITLES.get(edition, TITLES["morning"])
    footer = _footer(now, head16, cleveland, first_entry)
    text = _compose(title, kept, footer)
    while len(text.encode()) > MAX_PANEL_BYTES and (trimmed := _trim(kept)) is not None:
        kept = trimmed
        text = _compose(title, kept, footer)
    if not _worth_sending(kept, edition):
        return None
    return Message(title=title, text=text)


def render_backtest(summary: dict, now: datetime) -> Message:
    """回测结果的钉钉消息（设计文档第 8 节）。summary 的结构见 _series_entries、_band_stats。

    每类合约一行（公布当晚 19:30 的成绩，再加提前 3 天早上的），一行定价一边倒的合约实际发生了多少，
    页脚是请求次数，没跑完时注明部分完成。summary 里缺的、不是数字的项都跳过，不会抛异常。
    """
    summary = summary if isinstance(summary, Mapping) else {}
    lines = [line for ticker, entry in _series_entries(summary) if (line := _series_line(ticker, entry))]
    if bands := _bands_line(summary):
        lines.append(bands)
    if not lines:
        lines.append("· 这次没有算出结果")
    footer = []
    if (requests := _int(summary.get("requests"))) is not None:
        footer.append(f"请求 {requests} 次")
    if (skipped := _int(summary.get("skipped"))) is not None and skipped > 0:
        footer.append(f"跳过 {skipped} 次（结算不清楚）")
    if summary.get("partial") is True:
        footer.append("部分完成")
    footer += [f"数据截至 {_clock(now)}", "仅供个人研究，不构成投资建议"]
    blocks = [f"### {ICON} {BACKTEST_TITLE}", grey(BACKTEST_INTRO), LINE.join(lines), "---", grey("｜".join(footer))]
    return Message(title=BACKTEST_TITLE, text=BREAK.join(blocks))


# ---- 板块消息 ----


def _compose(title: str, lines: list[Line], footer: str) -> str:
    blocks = [f"### {ICON} {title}", grey(INTRO)]
    for key, heading in SECTIONS:
        entries = [_entry(line) for line in lines if line.section == key]
        if not entries:
            continue
        body = LINE.join(entries)
        blocks.append(f"**{heading}**{LINE}{body}" if heading else body)
    blocks += ["---", footer]
    return BREAK.join(blocks)


def _entry(line: Line) -> str:
    text = grey(line.text) if line.section == "note" else line.text
    if line.sub:
        text += LINE + grey(line.sub)
    return text


def _footer(now: datetime, head16: str | None, cleveland: bool, first_entry: bool = False) -> str:
    basis = SETTLEMENT + (f"｜{CLEVELAND}" if cleveland else "")
    if head16 and _HEAD16.fullmatch(head16):
        ledger = f"账本 #{head16}"
    else:
        ledger = FIRST_ENTRY if first_entry else NO_LEDGER
    return grey(basis) + LINE + grey(f"{DISCLAIMER}｜数据截至 {_clock(now)}｜{ledger}")


def _trim(lines: list[Line]) -> list[Line] | None:
    """删掉一点内容：先删最后一条附注，再删最后一行的灰色小字，最后删最后一行。没得删时返回 None。"""
    for i in reversed(range(len(lines))):
        if lines[i].section == "note":
            return lines[:i] + lines[i + 1 :]
    for i in reversed(range(len(lines))):
        if lines[i].sub:
            return lines[:i] + [replace(lines[i], sub=None)] + lines[i + 1 :]
    return lines[:-1] if lines else None


def _worth_sending(lines: list[Line], edition: str) -> bool:
    if edition == "evening":
        return any(line.section in EVENING_SECTIONS for line in lines)
    return any(line.section != "note" for line in lines)


def _clock(now: datetime) -> str:
    """北京时间的 HH:MM；不带时区的时间按已经是北京时间处理。"""
    local = now.astimezone(BJT) if now.tzinfo else now
    return f"{local:%H:%M}"


# ---- 数值 ----


def _half_up(v: float, step: str) -> Decimal:
    if not math.isfinite(v):
        raise ValueError("数值不是有限的数")
    return Decimal(str(v)).quantize(Decimal(step), ROUND_HALF_UP)


def _wan(v: float) -> Decimal:
    """人数换成“万”，一位小数，末尾的 .0 去掉。"""
    value = _half_up(v / 10000, "0.1")
    return value.quantize(Decimal("1")) if value == value.to_integral_value() else value


def _signed(value: Decimal, plus: bool) -> str:
    if value == 0:
        return str(abs(value))
    text = str(abs(value))
    if value < 0:
        return MINUS + text
    return ("+" if plus else "") + text


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None and number == int(number) else None


# ---- 回测消息 ----

# 每类合约的默认中文名、类型和单位（和 config/markets.yaml 的 key_data 默认值相同）
_RULES = {rule.series: rule for rule in DEFAULT_KEY_DATA}


def _series_entries(summary: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """summary["series"]：{系列代码: {"name"?, "kind"?, "unit"?, "labels": {"E0": {...}, "M3": {...}}}}。

    各时刻的成绩也可以直接放在系列这一层（{"E0": {...}}）。排序按规则文件里的顺序，其余按代码。
    """
    series = summary.get("series")
    if not isinstance(series, Mapping):
        return []
    entries = [(str(t), e) for t, e in series.items() if isinstance(e, Mapping)]
    order = list(_RULES)
    return sorted(entries, key=lambda te: (order.index(te[0]) if te[0] in order else len(order), te[0]))


def _labels(entry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    source = entry.get("labels") if isinstance(entry.get("labels"), Mapping) else entry
    return {
        str(label): stats
        for label, stats in source.items()
        if _LABEL.match(str(label)) and isinstance(stats, Mapping) and (_int(stats.get("n")) or 0) > 0
    }


def _when(label: str, kind: str) -> str:
    if label == "E0":
        # 议息声明在北京时间凌晨（02:00 或 03:00），晚间版是前一天晚上发的
        return "公布前一晚 19:30" if kind == "fed" else "公布当晚 19:30"
    days = int(label[1:])
    return "公布当天早上" if days == 0 else f"提前 {days} 天早上"


def _series_line(ticker: str, entry: Mapping[str, Any]) -> str | None:
    """一类合约一行，例如：
    · CPI 环比（23 次）：公布当晚 19:30 中位数平均差 0.06 个百分点，一半把握区间命中 13 次；提前 3 天早上平均差 0.08 个百分点"""
    labels = _labels(entry)
    order = ["E0"] + [f"M{d}" for d in range(8)]
    first = next((label for label in order if label in labels), None)
    if first is None:
        return None
    rule = _RULES.get(ticker)
    name = entry.get("name") if isinstance(entry.get("name"), str) else (rule.name if rule else ticker)
    kind = entry.get("kind") if isinstance(entry.get("kind"), str) else (rule.kind if rule else "ladder")
    unit = entry.get("unit") if isinstance(entry.get("unit"), str) else (rule.unit if rule else None)
    stats = labels[first]
    later = labels.get("M3") if first != "M3" else None
    if kind == "fed":
        body = _fed_text(stats, later)
    else:
        body = _ladder_text(stats, later, unit)
    if body is None:
        return None
    when = _when(first, kind)
    # “19:30 中位数”中间空一格，“早上中位数”不空
    sep = " " if when[-1].isdigit() else ""
    return f"· {md_text(truncate(name, 20))}（{_int(stats.get('n'))} 次）：{when}{sep}{body}"


def _ladder_text(stats: Mapping[str, Any], later: Mapping[str, Any] | None, unit: str | None) -> str | None:
    mae = _num(stats.get("mae_median"))
    if mae is None:
        return None
    text = f"中位数平均差 {_error(mae, unit)}"
    if (covered := _int(stats.get("covered50"))) is not None:
        text += f"，一半把握区间命中 {covered} 次"
    if later and (later_mae := _num(later.get("mae_median"))) is not None:
        text += f"；提前 3 天早上平均差 {_error(later_mae, unit)}"
    return text


def _fed_text(stats: Mapping[str, Any], later: Mapping[str, Any] | None) -> str | None:
    hits = _int(stats.get("top_hit"))
    if hits is None:
        return None
    text = f"市场最看好的结果对了 {hits} 次"
    if (p := _num(stats.get("p_actual_mean"))) is not None:
        text += f"，实际结果的平均价格 {pct(p)}%"
    if later and (later_hits := _int(later.get("top_hit"))) is not None:
        text += f"；提前 3 天早上对了 {later_hits} 次"
    return text


def _error(value: float, unit: str | None) -> str:
    """平均差：百分比两位小数（个百分点），人数换成万人（一位小数）。"""
    if unit == "jobs":
        return f"{abs(_wan(value))} 万人"
    return f"{_half_up(abs(value), '0.01')}" + (" 个百分点" if unit == "pct" else "")


def _band_stats(summary: Mapping[str, Any], key: str) -> tuple[int, int] | None:
    """summary["bands"][key] 或 summary[key]：{"n": 合约数, "yes": 实际发生数}。"""
    bands = summary.get("bands") if isinstance(summary.get("bands"), Mapping) else summary
    stats = bands.get(key)
    if not isinstance(stats, Mapping):
        return None
    n, yes = _int(stats.get("n")), _int(stats.get("yes"))
    if n is None or yes is None or n <= 0 or not 0 <= yes <= n:
        return None
    return n, yes


def _bands_line(summary: Mapping[str, Any]) -> str | None:
    rules = Lopsided()
    parts = []
    for key, (lo, hi) in (("band_hi", rules.hi), ("band_lo", rules.lo)):
        if stats := _band_stats(summary, key):
            n, yes = stats
            parts.append(f"价格在 {pct(lo)}% 到 {pct(hi)}% 的 {n} 个，实际发生 {yes} 个（{pct(yes / n)}%）")
    return "· 定价一边倒的合约：" + "；".join(parts) if parts else None
