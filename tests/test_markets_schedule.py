from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest

from digest.markets.kalshi import KEvent, KMarket
from digest.markets.schedule import (
    BJT,
    ET,
    bjt_label,
    first_monday_of_month,
    is_monday,
    lead_days,
    release_at,
    release_for_event,
)
from digest.markets.settings import MarketsSettings, Releases, load_settings
from digest.markets.types import Quote

# 编造的日程（年份都在 2030 年以后），只是为了覆盖夏令时切换前后：
# 美国 2030 年 3 月 10 日开始夏令时、11 月 3 日结束；中国没有夏令时
RELEASES = Releases(
    cpi={
        (2030, 1): date(2030, 2, 13),  # 冬令时
        (2030, 2): date(2030, 3, 8),  # 夏令时开始前的周五
        (2030, 3): date(2030, 4, 11),
        (2030, 9): date(2030, 11, 1),  # 夏令时结束前的周五
        (2030, 10): date(2030, 11, 5),  # 夏令时结束后的周二
    },
    empsit={(2030, 2): date(2030, 3, 12)},  # 夏令时开始后的周二
    fomc=(date(2030, 1, 30), date(2030, 3, 13), date(2030, 12, 11)),
)
SETTINGS = replace(MarketsSettings(), releases=RELEASES)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def market(ticker: str, close: datetime | None) -> KMarket:
    return KMarket(
        ticker=ticker, event_ticker=ticker.rsplit("-", 1)[0], status="active", close_time=close,
        strike_type="greater", floor=0.1, cap=None, custom=None, yes_sub_title="", quote=Quote(0.4, 0.45, 0.42),
        open_interest=100.0, result="", expiration_value="", settlement_value=None,
    )


def event(event_ticker: str, *closes: datetime | None) -> KEvent:
    markets = tuple(market(f"{event_ticker}-T{n}", close) for n, close in enumerate(closes))
    return KEvent(event_ticker=event_ticker, series_ticker=event_ticker.split("-")[0], title="编造的事件",
                  mutually_exclusive=False, strike_date=None, markets=markets)


# ---- release_at ----


def test_cpi_release_is_0830_et_and_dst_aware():
    winter = release_at(SETTINGS, "cpi", (2030, 1))
    assert winter == datetime(2030, 2, 13, 8, 30, tzinfo=ET)
    assert winter.utcoffset() == timedelta(hours=-5)
    assert winter.astimezone(timezone.utc) == utc(2030, 2, 13, 13, 30)
    # 冬天 08:30 是北京时间 21:30，夏天是 20:30
    assert bjt_label(winter) == "2月13日 周三 21:30"
    assert bjt_label(release_at(SETTINGS, "cpi", (2030, 2))) == "3月8日 周五 21:30"
    assert bjt_label(release_at(SETTINGS, "empsit", (2030, 2))) == "3月12日 周二 20:30"
    summer = release_at(SETTINGS, "cpi", (2030, 3))
    assert summer.astimezone(timezone.utc) == utc(2030, 4, 11, 12, 30)
    assert bjt_label(summer) == "4月11日 周四 20:30"
    # 秋天切回冬令时的前后
    assert bjt_label(release_at(SETTINGS, "cpi", (2030, 9))) == "11月1日 周五 20:30"
    assert bjt_label(release_at(SETTINGS, "cpi", (2030, 10))) == "11月5日 周二 21:30"


def test_fomc_release_is_1400_et_on_table_day_of_that_month():
    december = release_at(SETTINGS, "fomc", (2030, 12))
    assert december == datetime(2030, 12, 11, 14, 0, tzinfo=ET)
    # 美东下午两点已经是北京时间第二天凌晨：冬天 3 点，夏天 2 点
    assert bjt_label(december) == "12月12日 周四 03:00"
    assert bjt_label(release_at(SETTINGS, "fomc", (2030, 3))) == "3月14日 周四 02:00"
    assert release_at(SETTINGS, "fomc", (2030, 2)) is None


def test_release_at_missing_rows_and_unknown_kind():
    assert release_at(SETTINGS, "cpi", (2030, 4)) is None
    assert release_at(SETTINGS, "empsit", (2030, 1)) is None
    assert release_at(SETTINGS, "gdp", (2030, 1)) is None
    # 默认设置里日程表是空的
    assert release_at(MarketsSettings(), "cpi", (2030, 1)) is None


def test_release_at_accepts_releases_and_custom_times():
    assert release_at(RELEASES, "cpi", (2030, 1)) == release_at(SETTINGS, "cpi", (2030, 1))
    custom = replace(RELEASES, times_et={"cpi": time(10, 0)})
    assert release_at(custom, "cpi", (2030, 1)) == datetime(2030, 2, 13, 10, 0, tzinfo=ET)
    # times_et 里缺的类型用默认时间
    assert release_at(custom, "fomc", (2030, 1)) == datetime(2030, 1, 30, 14, 0, tzinfo=ET)


def test_real_rules_file_rows_are_0830_and_1400_et():
    settings = load_settings()
    assert settings.error is None and settings.releases.cpi and settings.releases.fomc
    for kind in ("cpi", "empsit"):
        for period, day in getattr(settings.releases, kind).items():
            released = release_at(settings, kind, period)
            assert released == datetime.combine(day, time(8, 30), tzinfo=ET)
            assert released.astimezone(BJT).strftime("%H:%M") in ("20:30", "21:30")
    for day in settings.releases.fomc:
        released = release_at(settings, "fomc", (day.year, day.month))
        assert released == datetime.combine(day, time(14, 0), tzinfo=ET)
        assert released.astimezone(BJT).date() == day + timedelta(days=1)


# ---- release_for_event ----


def test_event_matches_table():
    # CPI 合约在公布当天美东 08:25 收盘（冬天是 13:25Z）
    found = release_for_event(SETTINGS, "cpi", event("KXZZCPI-30JAN", utc(2030, 2, 13, 13, 25), utc(2030, 2, 13, 13, 29)))
    assert found == (datetime(2030, 2, 13, 8, 30, tzinfo=ET), "table")
    # 议息合约在声明前一分钟收盘
    found = release_for_event(SETTINGS, "fomc", event("KXZZFED-30MAR", utc(2030, 3, 13, 17, 59)))
    assert found == (datetime(2030, 3, 13, 14, 0, tzinfo=ET), "table")


def test_event_close_date_is_taken_in_et_not_utc():
    # 美东 3 月 7 日晚上收盘，UTC 已经是 3 月 8 日：按美东日期算，和日程表的 3 月 8 日对不上
    found = release_for_event(SETTINGS, "cpi", event("KXZZCPI-30FEB", utc(2030, 3, 8, 4, 30)))
    assert found == (None, "日程对不上")
    # UTC 3 月 8 日 13:25 在美东也是 3 月 8 日
    assert release_for_event(SETTINGS, "cpi", event("KXZZCPI-30FEB", utc(2030, 3, 8, 13, 25)))[1] == "table"


def test_event_mismatch_uses_earliest_close(caplog):
    caplog.set_level(logging.INFO, logger="digest.markets")
    ticker = "KXZZCPI-30JAN"
    # 有一个合约提前一天收盘：最早的收盘日期和日程表对不上，这一行不显示
    found = release_for_event(SETTINGS, "cpi", event(ticker, utc(2030, 2, 13, 13, 25), utc(2030, 2, 12, 13, 25)))
    assert found == (None, "日程对不上")
    assert "对不上" in caplog.text and ticker not in caplog.text and "2030" not in caplog.text
    # 晚于公布日收盘的合约不影响（最早的那个对得上）
    found = release_for_event(SETTINGS, "cpi", event(ticker, utc(2030, 2, 14, 13, 25), utc(2030, 2, 13, 13, 25)))
    assert found[1] == "table"


def test_event_without_close_times_uses_table():
    found = release_for_event(SETTINGS, "empsit", event("KXZZJOBS-30FEB", None, None))
    assert found == (datetime(2030, 3, 12, 8, 30, tzinfo=ET), "table")


def test_event_missing_from_table_falls_back_to_kalshi_close(caplog):
    caplog.set_level(logging.INFO, logger="digest.markets")
    # 日程表里没有 2031 年 4 月：用收盘那天（美东）加 08:30，夏天是北京时间 20:30
    found = release_for_event(SETTINGS, "cpi", event("KXZZCPI-31APR", utc(2031, 5, 9, 12, 29), None))
    assert found == (datetime(2031, 5, 9, 8, 30, tzinfo=ET), "kalshi")
    assert bjt_label(found[0]) == "5月9日 周五 20:30"
    assert "KXZZCPI" not in caplog.text
    # 冬天收盘在 13:29Z，同样是美东当天 08:30，北京时间 21:30
    found = release_for_event(SETTINGS, "empsit", event("KXZZJOBS-30DEC", utc(2031, 1, 7, 13, 29)))
    assert found == (datetime(2031, 1, 7, 8, 30, tzinfo=ET), "kalshi")
    assert bjt_label(found[0]) == "1月7日 周二 21:30"
    # 议息：那个月没有日程，用收盘日 14:00
    found = release_for_event(SETTINGS, "fomc", event("KXZZFED-30JUN", utc(2030, 6, 12, 17, 59)))
    assert found == (datetime(2030, 6, 12, 14, 0, tzinfo=ET), "kalshi")
    # 事件代码认不出参考月时也只能靠收盘日期
    found = release_for_event(SETTINGS, "cpi", event("ZZODD", utc(2030, 2, 13, 13, 25)))
    assert found == (datetime(2030, 2, 13, 8, 30, tzinfo=ET), "kalshi")


def test_event_without_table_or_close_has_no_time():
    assert release_for_event(SETTINGS, "cpi", event("KXZZCPI-31APR")) == (None, "没有日程")
    assert release_for_event(SETTINGS, "cpi", event("KXZZCPI-31APR", None)) == (None, "没有日程")
    assert release_for_event(SETTINGS, "gdp", event("KXZZGDP-30Q1", utc(2030, 4, 30, 12, 29))) == (None, "没有日程")


def test_naive_close_time_is_treated_as_utc():
    naive = datetime(2030, 2, 13, 13, 25)
    assert release_for_event(SETTINGS, "cpi", event("KXZZCPI-30JAN", naive))[1] == "table"


# ---- 北京时间的显示和日期 ----


def test_bjt_label_formats():
    assert bjt_label(datetime(2030, 1, 6, 9, 5, tzinfo=BJT)) == "1月6日 周日 09:05"
    # 不带时区的时间按 UTC：UTC 16:00 是北京时间第二天 0 点
    assert bjt_label(datetime(2030, 1, 6, 16, 0)) == "1月7日 周一 00:00"
    assert bjt_label(utc(2030, 12, 31, 23, 59)) == "1月1日 周三 07:59"


def test_lead_days_counts_bjt_calendar_days():
    release = release_at(SETTINGS, "cpi", (2030, 2))  # 北京时间 3 月 8 日 21:30
    assert lead_days(datetime(2030, 3, 8, 7, 30, tzinfo=BJT), release) == 0
    assert lead_days(datetime(2030, 3, 7, 23, 59, tzinfo=BJT), release) == 1
    assert lead_days(datetime(2030, 3, 1, 7, 30, tzinfo=BJT), release) == 7
    assert lead_days(datetime(2030, 3, 9, 7, 30, tzinfo=BJT), release) == 0  # 已经过去
    # 议息在美东 3 月 13 日下午，北京时间已是 3 月 14 日：按北京时间算差一天
    fomc = release_at(SETTINGS, "fomc", (2030, 3))
    assert lead_days(datetime(2030, 3, 13, 19, 30, tzinfo=BJT), fomc) == 1
    # now 用 UTC 表示也一样
    assert lead_days(utc(2030, 3, 12, 23, 30), fomc) == 1


@pytest.mark.parametrize(
    ("now", "monday", "first"),
    [
        (datetime(2030, 3, 4, 7, 30, tzinfo=BJT), True, True),  # 3 月第一个周一
        (datetime(2030, 3, 11, 7, 30, tzinfo=BJT), True, False),
        (datetime(2030, 4, 1, 19, 30, tzinfo=BJT), True, True),  # 1 号就是周一
        (datetime(2030, 4, 7, 7, 30, tzinfo=BJT), False, False),  # 周日
        (datetime(2030, 7, 1, 0, 0, tzinfo=BJT), True, True),
        (utc(2030, 3, 10, 20, 0), True, False),  # UTC 周日晚上已是北京时间周一（3 月 11 日）
        (utc(2030, 3, 3, 20, 0), True, True),  # 北京时间 3 月 4 日周一
        (utc(2030, 3, 4, 20, 0), False, False),  # 北京时间已是周二
    ],
)
def test_monday_checks_use_bjt(now, monday, first):
    assert is_monday(now) is monday
    assert first_monday_of_month(now) is first
