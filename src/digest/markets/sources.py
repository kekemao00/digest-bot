"""市场板块用到的非市场数据：克利夫兰联储通胀实时预测、Cboe 波动率指数收盘、劳工统计局首次公布值、
纽约联储的联邦基金利率目标区间（见 contracts/official.md）。

约定：
- 所有请求都走 Fetcher，网络错误（FetchError）和预算用完（BudgetExceeded）原样抛给调用方。
- 整份数据的格式不对时抛 ValueError，消息是固定的中文，不带数据本身；单条记录写坏了就跳过，其余照用。
- 日志里只有计数和固定的中文，不出现数值、地址和序列内容。
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from digest.markets.net import Fetcher

log = logging.getLogger("digest.markets")

# 克利夫兰联储月度预测里的两条线：CPI 环比和核心 CPI 环比（不带 "Actual " 前缀的是预测值）
CPI_SERIES = "CPI Inflation"
CORE_SERIES = "Core CPI Inflation"
# 一个目标月的预测从当月开始，到官方公布为止，一般一两个月；跨度太长说明年份推错了
MAX_PATH_DAYS = 220
# 劳工统计局 v1 接口（不用注册），后面直接接序列代码
BLS_V1 = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_OK = "REQUEST_SUCCEEDED"
FIRST_PRINT_KINDS = ("cpi", "core", "payrolls", "u3")

_TARGET_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_DAY_LABEL = re.compile(r"^(\d{1,2})/(\d{1,2})$")
_US_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_BLS_MONTH = re.compile(r"^M(0[1-9]|1[0-2])$")
_SERIES_ID = re.compile(r"^[A-Z0-9]+$")
_ONE_DECIMAL = Decimal("0.1")
_THREE_DECIMALS = Decimal("0.001")


# ---- 克利夫兰联储通胀实时预测 ----


@dataclass(frozen=True)
class Nowcast:
    """一个目标月的 CPI、核心 CPI 环比预测（百分比，不年化），以及预测的日期。"""

    cpi: float | None
    core: float | None
    as_of: date | None  # 两个数里较早的那个日期，判断是否过时用


def cleveland_nowcast(fetcher: Fetcher, url: str) -> dict[tuple[int, int], Nowcast]:
    """读取 nowcast_month.json，按目标月 (年, 月) 返回最新的预测；没有任何预测值的目标月不收。"""
    return _parse_nowcast(fetcher.get_json(url))


def _parse_nowcast(payload: Any) -> dict[tuple[int, int], Nowcast]:
    if isinstance(payload, dict):  # 只有一个目标月时也认
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError("克利夫兰联储的数据不是预期的格式")
    result: dict[tuple[int, int], Nowcast] = {}
    bad = 0
    for chart in payload:
        parsed = _nowcast_chart(chart)
        if parsed is None:
            bad += 1
            continue
        target, nowcast = parsed
        if nowcast.cpi is None and nowcast.core is None:
            continue
        old = result.get(target)
        # 同一个目标月出现两次时留日期新的那个
        if old is None or (nowcast.as_of or date.min) >= (old.as_of or date.min):
            result[target] = nowcast
    if bad:
        log.warning("克利夫兰联储：%d 个目标月的格式不对，已跳过", bad)
    return result


def _nowcast_chart(chart: Any) -> tuple[tuple[int, int], Nowcast] | None:
    """一个目标月的图表配置；格式不对时返回 None。"""
    if not isinstance(chart, dict):
        return None
    target = _target_month(chart.get("chart"))
    if target is None:
        return None
    days = _path_dates(chart.get("categories"), target)
    dataset = chart.get("dataset")
    if days is None or not isinstance(dataset, list):
        return None
    found: dict[str, Any] = {}
    for series in dataset:
        name = series.get("seriesname") if isinstance(series, dict) else None
        if isinstance(name, str) and name.strip() in (CPI_SERIES, CORE_SERIES):
            found[name.strip()] = series.get("data")
    latest = {}
    for name, data in found.items():
        # 每个非竖线的日期对应一个数据点，个数对不上就没法知道哪个数是哪天的
        if not isinstance(data, list) or len(data) != len(days):
            return None
        latest[name] = _latest_point(data, days)
    cpi, core = latest.get(CPI_SERIES), latest.get(CORE_SERIES)
    dates = [point[1] for point in (cpi, core) if point is not None]
    return target, Nowcast(
        cpi=cpi[0] if cpi else None,
        core=core[0] if core else None,
        as_of=min(dates) if dates else None,
    )


def _target_month(info: Any) -> tuple[int, int] | None:
    """chart.subcaption 是目标月，形如 "2026-11"（月份是否补零不确定，按数字读）。"""
    caption = info.get("subcaption") if isinstance(info, dict) else None
    match = _TARGET_MONTH.match(caption.strip()) if isinstance(caption, str) else None
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return (year, month) if 1 <= month <= 12 else None


def _path_dates(categories: Any, target: tuple[int, int]) -> list[date] | None:
    """横轴上每个预测点的日期。标签只有“月/日”，年份从目标月推：
    第一个点取离目标月月初最近的那一年，之后月份变小就进一年。竖线（数据公布的标记）不是数据点，跳过。"""
    if not isinstance(categories, list) or not categories or not isinstance(categories[0], dict):
        return None
    items = categories[0].get("category")
    if not isinstance(items, list):
        return None
    labels: list[tuple[int, int]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        if _is_vline(item.get("vline")):
            continue
        label = item.get("label")
        match = _DAY_LABEL.match(label.strip()) if isinstance(label, str) else None
        if not match:
            return None
        labels.append((int(match.group(1)), int(match.group(2))))
    if not labels:
        return []
    year = _nearest_year(labels[0], target)
    if year is None:
        return None
    days: list[date] = []
    previous = labels[0][0]
    for month, day in labels:
        if month < previous:
            year += 1
        previous = month
        try:
            days.append(date(year, month, day))
        except ValueError:
            return None
    increasing = all(a < b for a, b in zip(days, days[1:]))
    if not increasing or (days[-1] - days[0]).days > MAX_PATH_DAYS:
        return None
    return days


def _is_vline(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false")
    return bool(value)


def _nearest_year(label: tuple[int, int], target: tuple[int, int]) -> int | None:
    """第一个预测点属于哪一年：在目标年前后各一年里，取离目标月一号最近的。"""
    month, day = label
    start = date(target[0], target[1], 1)
    candidates = []
    for year in (target[0] - 1, target[0], target[0] + 1):
        try:
            candidates.append((abs((date(year, month, day) - start).days), year))
        except ValueError:  # 比如平年的 2 月 29 日
            continue
    return min(candidates)[1] if candidates else None


def _latest_point(data: list[Any], days: list[date]) -> tuple[float, date] | None:
    """最后一个非空的值和它的日期。值可能是字符串也可能是数字，空字符串表示这天没有。"""
    for item, day in zip(reversed(data), reversed(days)):
        value = _number(item.get("value") if isinstance(item, dict) else None)
        if value is not None:
            return value, day
    return None


# ---- Cboe 波动率指数 ----


@dataclass(frozen=True)
class Close:
    """一天的收盘值。"""

    day: date
    value: float


def cboe_last_close(fetcher: Fetcher, url: str) -> Close | None:
    """VIX / VIX9D 历史 CSV（表头 DATE,OPEN,HIGH,LOW,CLOSE，日期 MM/DD/YYYY，从旧到新）里最后一个有收盘值的行。"""
    return _last_close(fetcher.get_text(url))


def _last_close(text: str) -> Close | None:
    rows = [row for row in csv.reader(io.StringIO(text.lstrip("﻿"))) if any(cell.strip() for cell in row)]
    if not rows:
        return None
    header = [cell.strip().upper() for cell in rows[0]]
    if "DATE" not in header or "CLOSE" not in header:
        raise ValueError("Cboe 的表头不是预期的格式")
    i_date, i_close = header.index("DATE"), header.index("CLOSE")
    for row in reversed(rows[1:]):
        if len(row) <= max(i_date, i_close):
            continue
        day = _us_date(row[i_date])
        value = _number(row[i_close])
        # 指数不会是 0 或负数，这样的值当作空
        if day is not None and value is not None and value > 0:
            return Close(day=day, value=value)
    return None


def _us_date(text: str) -> date | None:
    match = _US_DATE.match(text.strip())
    if not match:
        return None
    month, day, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


# ---- 劳工统计局首次公布值 ----


def bls_series(fetcher: Fetcher, series_id: str, base: str = BLS_V1) -> dict[tuple[int, int], float]:
    """劳工统计局 v1 接口（GET，默认返回最近三年）的一个序列，按 (年, 月) 返回数值。

    只收 M01 到 M12（不收年度平均 M13 等），值为 "-"（没有发布）的月份跳过。
    接口说请求没被处理时抛 ValueError；序列代码写错时接口照样说成功、只是没有数据，这里返回空表。
    base 是接口地址前缀（规则文件的 witnesses.bls_v1），后面直接接序列代码。
    """
    series_id = series_id.strip()
    if not _SERIES_ID.match(series_id):
        raise ValueError("劳工统计局的序列代码应该只有大写字母和数字")
    prefix = base if base.endswith("/") else base + "/"
    data = _parse_bls(fetcher.get_json(prefix + series_id), series_id)
    if not data:
        log.warning("劳工统计局：这个序列没有可用的月度数据")
    return data


def _parse_bls(payload: Any, series_id: str) -> dict[tuple[int, int], float]:
    if not isinstance(payload, dict):
        raise ValueError("劳工统计局的数据不是预期的格式")
    status = payload.get("status")
    if status is not None and status != BLS_OK:
        # 超过每天的请求次数等情况；提示文本里可能有序列代码，不记日志
        raise ValueError("劳工统计局没有处理这次请求")
    series = _bls_series_object(payload.get("Results"), series_id)
    if series is None:
        return {}
    items = series.get("data")
    if not isinstance(items, list):
        return {}
    values: dict[tuple[int, int], float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        year, period = str(item.get("year", "")).strip(), str(item.get("period", "")).strip()
        match = _BLS_MONTH.match(period)
        value = _number(item.get("value"))  # "-" 解析不出数字，自然跳过
        if not (year.isdigit() and match) or value is None:
            continue
        # 数据从新到旧排，同一个月出现两次时留前面（较新）的
        values.setdefault((int(year), int(match.group(1))), value)
    return values


def _bls_series_object(results: Any, series_id: str) -> dict | None:
    """Results 实际是 {"series": [...]}，文档示例写成列表，两种都认；有多个序列时挑代码对得上的。"""
    if isinstance(results, list):
        results = results[0] if results else None
    if not isinstance(results, dict):
        return None
    series = results.get("series")
    if series is None and "data" in results:  # 列表里直接放的是序列本身
        series = [results]
    if not isinstance(series, list):
        return None
    candidates = [s for s in series if isinstance(s, dict)]
    for s in candidates:
        if str(s.get("seriesID", "")).strip() == series_id:
            return s
    return candidates[0] if candidates else None


def first_print(kind: str, data: dict[tuple[int, int], float], period: tuple[int, int]) -> float | None:
    """某个参考月的官方公布值，和劳工统计局公布的算法一致；缺本月或上月的数时返回 None。

    cpi / core：用三位小数的季调指数算环比百分比，四舍五入（ROUND_HALF_UP）到一位小数；
    payrolls：本月减上月的就业人数（数据单位是千人），换算成人；u3：失业率原值。
    不认识的 kind 也返回 None。
    """
    if kind not in FIRST_PRINT_KINDS:
        return None
    current = _decimal(data.get(period))
    if kind == "u3":
        return None if current is None else float(current)
    previous = _decimal(data.get(_previous_month(period)))
    if current is None or previous is None:
        return None
    if kind == "payrolls":
        return float((current - previous) * 1000) + 0.0
    # 公布的指数是三位小数，劳工统计局就用三位小数的指数算环比
    level_t = current.quantize(_THREE_DECIMALS, ROUND_HALF_UP)
    level_p = previous.quantize(_THREE_DECIMALS, ROUND_HALF_UP)
    if level_p <= 0:
        return None
    change = (level_t / level_p - 1) * 100
    rounded = change.quantize(_ONE_DECIMAL, ROUND_HALF_UP)
    if abs(change - rounded) == Decimal("0.05"):
        log.info("劳工统计局：环比恰好落在进位的边界上，按四舍五入处理")
    # + 0.0 把 -0.0 变成 0.0，免得显示成 “-0.0%”
    return float(rounded) + 0.0


def _previous_month(period: tuple[int, int]) -> tuple[int, int]:
    year, month = period
    return (year, month - 1) if month > 1 else (year - 1, 12)


def _decimal(value: Any) -> Decimal | None:
    """float 转回 Decimal：str() 给出最短的十进制写法，"331.245" 不会变成 331.24499999…"""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return Decimal(str(value))


# ---- 纽约联储：联邦基金利率目标区间 ----


def nyfed_target_upper(fetcher: Fetcher, url: str) -> list[tuple[date, float]]:
    """有效联邦基金利率（EFFR）接口里每天的目标区间上限 (日期, 百分比)，按日期从早到晚，同一天只留一条。
    接口的数据可能是旧的（见 contracts/official.md 4.1），调用方要自己看最后一天是不是够新。"""
    return _parse_nyfed(fetcher.get_json(url))


def _parse_nyfed(payload: Any) -> list[tuple[date, float]]:
    rates = payload.get("refRates") if isinstance(payload, dict) else None
    if not isinstance(rates, list):
        raise ValueError("纽约联储的数据不是预期的格式")
    found: dict[date, float] = {}
    for rate in rates:
        if not isinstance(rate, dict):
            continue
        kind = rate.get("type")
        if kind is not None and str(kind).strip().upper() != "EFFR":
            continue
        day = _iso_date(rate.get("effectiveDate"))
        upper = _number(rate.get("targetRateTo"))
        if day is not None and upper is not None:
            found.setdefault(day, upper)  # 接口从新到旧排，同一天留第一条
    return sorted(found.items())


def _iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


# ---- 通用 ----


def _number(value: Any) -> float | None:
    """数字或数字字符串（"0.2421"、"17.240000"、4.25）；空、"-"、布尔值和非有限数都算没有。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(Decimal(text))
        except (InvalidOperation, ValueError):
            return None
    else:
        return None
    return number if math.isfinite(number) else None
