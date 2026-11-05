"""官方数据的公布时间和北京时间的显示。

- CPI、就业形势报告：规则文件里的官方日程表（参考月 -> 公布日期），美东时间 08:30；
  议息：日程表里的声明日，美东时间 14:00。夏令时、冬令时由 zoneinfo 换算，
  所以同一个 08:30 在北京时间冬天是 21:30、夏天是 20:30。
- Kalshi 的合约在公布当天（美东时间）收盘，用最早的收盘时间核对日程表；对不上就不显示。
- 日程表里没有的月份，改用合约收盘那天（美东时间）加上固定的公布时间。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from digest.markets.kalshi import KEvent, period_of
from digest.markets.settings import DEFAULT_TIMES_ET, MarketsSettings, Releases
from digest.render.text import WEEKDAYS

log = logging.getLogger("digest.markets")

BJT = ZoneInfo("Asia/Shanghai")
ET = ZoneInfo("America/New_York")

# release_for_event 返回的第二项：时间从哪来，或者为什么没有时间
FROM_TABLE = "table"  # 官方日程表，并且和合约收盘日期一致（或没有收盘时间可核对）
FROM_KALSHI = "kalshi"  # 日程表里没有这个月，用合约收盘那天推算
MISMATCH = "日程对不上"
NO_SCHEDULE = "没有日程"


def release_at(settings: MarketsSettings | Releases, kind: str, period: tuple[int, int]) -> datetime | None:
    """某个参考月（议息是会议所在的月份）的公布时间，带时区（美东时间）；日程表里没有时返回 None。

    kind：cpi（CPI 和核心 CPI）| empsit（非农和失业率）| fomc（议息声明）。
    settings 可以是 MarketsSettings，也可以直接是其中的 Releases。
    """
    releases = _releases(settings)
    if kind == "cpi":
        day = releases.cpi.get(period)
    elif kind == "empsit":
        day = releases.empsit.get(period)
    elif kind == "fomc":
        day = next((d for d in releases.fomc if (d.year, d.month) == tuple(period)), None)
    else:
        return None
    clock = _time_et(releases, kind)
    if day is None or clock is None:
        return None
    return _at(day, clock)


def release_for_event(settings: MarketsSettings | Releases, kind: str, event: KEvent) -> tuple[datetime | None, str]:
    """一个 Kalshi 事件对应的公布时间和来源。

    先查日程表（参考月取自事件代码），再用最早的合约收盘时间在美东的日期核对：
    对不上返回 (None, "日程对不上")；日程表里没有时用收盘那天加固定时间，来源 "kalshi"；
    都没有时返回 (None, "没有日程")。
    """
    period = period_of(event.event_ticker)
    table = release_at(settings, kind, period) if period is not None else None
    close_day = _close_day(event)
    if table is not None:
        if close_day is None or close_day == table.date():
            return table, FROM_TABLE
        # 日志里只写数据类型，不写合约代码和日期
        log.warning("市场日程：%s 的官方日程和合约收盘日期对不上，这一行不显示", kind)
        return None, MISMATCH
    clock = _time_et(_releases(settings), kind)
    if close_day is None or clock is None:
        return None, NO_SCHEDULE
    log.info("市场日程：官方日程表里没有 %s 的这一期，改用合约收盘日期推算", kind)
    return _at(close_day, clock), FROM_KALSHI


def bjt_label(dt: datetime) -> str:
    """北京时间的日期、星期和 24 小时制钟点，如 "11月10日 周二 21:30"。不带时区的时间按 UTC 处理。"""
    local = _bjt(dt)
    return f"{local.month}月{local.day}日 周{WEEKDAYS[local.weekday()]} {local:%H:%M}"


def lead_days(now: datetime, release: datetime) -> int:
    """公布日比今天晚几天（按北京时间的日历日算），已经过去的算 0。"""
    return max(0, (_bjt(release).date() - _bjt(now).date()).days)


def is_monday(now: datetime) -> bool:
    """北京时间的周一。"""
    return _bjt(now).weekday() == 0


def first_monday_of_month(now: datetime) -> bool:
    """北京时间本月的第一个周一。"""
    local = _bjt(now)
    return local.weekday() == 0 and local.day <= 7


def _releases(settings: MarketsSettings | Releases) -> Releases:
    return settings if isinstance(settings, Releases) else settings.releases


def _time_et(releases: Releases, kind: str) -> time | None:
    return releases.times_et.get(kind) or DEFAULT_TIMES_ET.get(kind)


def _at(day: date, clock: time) -> datetime:
    """美东时间某天的某个钟点。zoneinfo 按那一天是否夏令时给出偏移。"""
    return datetime.combine(day, clock, tzinfo=ET)


def _close_day(event: KEvent) -> date | None:
    """事件里最早的合约收盘时间在美东是哪一天。"""
    closes = [_aware(m.close_time) for m in event.markets if m.close_time is not None]
    return min(closes).astimezone(ET).date() if closes else None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _bjt(dt: datetime) -> datetime:
    return _aware(dt).astimezone(BJT)
