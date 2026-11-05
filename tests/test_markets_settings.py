from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import date, time

import pytest

from digest import markets
from digest.markets.settings import (
    DISABLED_STATUS,
    RULES_PATH,
    MarketsSettings,
    Releases,
    SeriesRule,
    load_settings,
    rules_hash,
)
from digest.markets.types import Quote


def write_rules(tmp_path, text: str):
    path = tmp_path / "markets.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def months(start: tuple[int, int], count: int) -> list[tuple[int, int]]:
    year, month = start
    result = []
    for _ in range(count):
        result.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def test_shipped_rules_load_cleanly():
    settings = load_settings()
    assert settings.error is None
    assert settings.warnings == ()
    # 代码里的默认值和仓库里登记的规则一致（日程表的默认是空表，单独检查）
    assert replace(settings, releases=Releases()) == MarketsSettings()


def test_shipped_rules_have_release_rows():
    releases = load_settings().releases
    expected = months((2026, 10), 14)  # 参考月 2026 年 10 月到 2027 年 11 月
    for table in (releases.cpi, releases.empsit):
        assert list(table) == expected
        days = list(table.values())
        assert days == sorted(days)
        for (year, month), day in table.items():
            assert (day.year, day.month) > (year, month)
    assert all(day.weekday() == 4 for day in releases.empsit.values())  # 就业报告都在周五
    assert releases.fomc[0] == date(2026, 12, 9) and releases.fomc[-1] == date(2027, 12, 8)
    assert list(releases.fomc) == sorted(set(releases.fomc))
    assert all(day.weekday() == 2 for day in releases.fomc)  # 议息声明都在周三
    assert releases.times_et == {"cpi": time(8, 30), "empsit": time(8, 30), "fomc": time(14, 0)}


def test_rules_hash_is_sha256_of_file_bytes(tmp_path):
    assert rules_hash(RULES_PATH) == hashlib.sha256(RULES_PATH.read_bytes()).hexdigest()
    path = write_rules(tmp_path, "version: 1\n")
    before = rules_hash(path)
    path.write_text("version: 1\n# 只改了一行注释\n", encoding="utf-8")
    assert rules_hash(path) != before


BAD_VALUES = """
version: 1
audience: everyone
budget: {seconds: abc, requests: 0, timeout: 5, max_bytes: 2.5}
replay_budget: 7
kalshi:
  hosts: [http://plain.example-market.test]
  key_data:
    - {series: KXZZTEST, kind: ladder, grid: 0.5, unit: pct, release: cpi, name: 测试指标}
    - {series: KXZZBAD, kind: spaceship, release: cpi, name: 坏的}
    - just-a-string
  max_spread: 1.5
  min_open_interest: true
polymarket:
  tags: {alpha: 7, beta: -1}
  exclude_words: [yes, ok]
  lopsided: {hi: [0.97, 0.85], lo: [0.01, 0.1], max_items: 1}
  movers: {mode: loud, min_days: 9, max_days: 3, min_wallets: 12}
vix: {vix_url: "ftp://files.example-market.test/a.csv", max_age_days: 3}
bands: [[50, 一半], [40, 倒过来]]
unknown_block: {anything: 1}
"""


def test_bad_values_fall_back_to_defaults_with_warnings(tmp_path):
    settings = load_settings(write_rules(tmp_path, BAD_VALUES))
    d = MarketsSettings()
    assert settings.error is None
    assert settings.audience == "owner_only"
    assert settings.budget == replace(d.budget, timeout=5.0)
    assert settings.replay_budget == d.replay_budget
    assert settings.kalshi.hosts == d.kalshi.hosts
    assert settings.kalshi.key_data == (SeriesRule("KXZZTEST", "ladder", "测试指标", grid=0.5, unit="pct", release="cpi"),)
    assert settings.kalshi.weekly == d.kalshi.weekly
    assert settings.kalshi.max_spread == d.kalshi.max_spread
    assert settings.kalshi.min_open_interest == d.kalshi.min_open_interest
    assert settings.polymarket.tags == d.polymarket.tags
    assert settings.polymarket.exclude_words == d.polymarket.exclude_words
    lopsided = settings.polymarket.lopsided
    assert (lopsided.hi, lopsided.lo, lopsided.max_items) == (d.polymarket.lopsided.hi, (0.01, 0.1), 1)
    movers = settings.polymarket.movers
    assert (movers.mode, movers.min_days, movers.max_days, movers.min_wallets) == ("shadow", 2.0, 180.0, 12)
    assert settings.vix == replace(d.vix, max_age_days=3)
    assert settings.bands == d.bands

    text = "\n".join(settings.warnings)
    for where in (
        "audience",
        "budget.seconds",
        "budget.requests",
        "budget.max_bytes",
        "replay_budget",
        "kalshi.hosts",
        "kalshi.key_data 第 2 项",
        "kalshi.key_data 第 3 项",
        "kalshi.max_spread",
        "kalshi.min_open_interest",
        "polymarket.tags",
        "polymarket.exclude_words",
        "polymarket.lopsided.hi",
        "polymarket.movers.mode",
        "polymarket.movers.min_days",
        "vix.vix_url",
        "bands",
    ):
        assert where in text, where
    assert len(settings.warnings) == 17
    # 不认识的键直接忽略；提示里只有位置，不带写错的值
    assert "unknown_block" not in text
    for value in ("everyone", "abc", "plain.example-market.test", "spaceship", "loud", "ftp", "倒过来"):
        assert value not in text


def test_release_rows_are_checked_one_by_one(tmp_path):
    text = """
releases:
  times_et: {cpi: "07:15", empsit: "25:99", fomc: 14:00}
  cpi:
    2030-01: 2030-02-12
    2030-02: 2029-01-01
    2030-13: 2031-01-10
    2030-03: not a date
    2030-04: "2030-05-14"
    203004: 2030-05-20
  empsit: [2030-02-01]
  fomc: [2030-03-20, 2030-01-30, nonsense, 2030-01-30]
"""
    settings = load_settings(write_rules(tmp_path, text))
    releases = settings.releases
    # 不加引号的 14:00 被 YAML 读成 840 分钟，照样认
    assert releases.times_et == {"cpi": time(7, 15), "empsit": time(8, 30), "fomc": time(14, 0)}
    assert releases.cpi == {(2030, 1): date(2030, 2, 12), (2030, 4): date(2030, 5, 14)}
    assert releases.empsit == {}
    assert releases.fomc == (date(2030, 1, 30), date(2030, 3, 20))
    warnings = "\n".join(settings.warnings)
    assert "releases.times_et.empsit" in warnings
    for n in (2, 3, 4, 6):
        assert f"releases.cpi 第 {n} 行" in warnings
    assert "releases.empsit" in warnings
    assert "releases.fomc 第 3 项" in warnings
    assert len(settings.warnings) == 7


def test_missing_keys_use_defaults_silently(tmp_path):
    settings = load_settings(write_rules(tmp_path, "version: 1\nkalshi:\n  days_ahead: 3\nvix:\n"))
    assert settings.error is None and settings.warnings == ()
    assert settings.kalshi.days_ahead == 3
    assert replace(settings, kalshi=MarketsSettings().kalshi) == MarketsSettings()
    assert load_settings(write_rules(tmp_path, "")) == MarketsSettings()


def test_unknown_version_is_read_as_version_one(tmp_path):
    settings = load_settings(write_rules(tmp_path, "version: 2\n"))
    assert settings.version == 1 and settings.error is None
    assert len(settings.warnings) == 1 and "version" in settings.warnings[0]


@pytest.mark.parametrize(
    "text, reason",
    [
        ("budget: [1, 2\n", "YAML"),
        ("kalshi:\n  hosts: [a\n  days_ahead: 3\n", "YAML"),
        ("- just\n- a list\n", "顶层"),
        ("plain words\n", "顶层"),
    ],
)
def test_unreadable_rules_disable_the_panel(tmp_path, text, reason):
    settings = load_settings(write_rules(tmp_path, text))
    assert settings.error and reason in settings.error
    assert settings.warnings == ()
    assert replace(settings, error=None) == MarketsSettings()


def test_missing_rules_file_disables_the_panel(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    settings = load_settings(tmp_path / "nope.yaml")
    assert settings.error == "规则文件不存在"
    assert "规则文件不存在" in caplog.text
    assert DISABLED_STATUS == "市场板块：规则文件有误，未启用"


def test_non_utf8_rules_file_disables_the_panel(tmp_path):
    path = tmp_path / "markets.yaml"
    path.write_bytes("audience: 测试\n".encode("gbk"))
    assert load_settings(path).error == "规则文件不是 UTF-8 编码"


def test_warnings_are_logged(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    load_settings(write_rules(tmp_path, "budget: {seconds: -5}\n"))
    assert "budget.seconds" in caplog.text and "-5" not in caplog.text


def test_enabled_reads_the_repository_variable():
    assert not markets.enabled({})
    assert not markets.enabled({"MARKETS_ENABLED": ""})
    assert not markets.enabled({"MARKETS_ENABLED": "false"})
    for value in ("true", "TRUE", " yes ", "1"):
        assert markets.enabled({"MARKETS_ENABLED": value})


def test_markets_enabled_is_cleared_for_tests():
    # conftest 的 clean_env 会清掉本机环境里的开关
    assert not markets.enabled()


# 共用类型 Quote.mid 的取价规则


def test_quote_mid_uses_bid_ask_when_spread_is_tight():
    assert Quote(0.41, 0.47, 0.52).mid() == (pytest.approx(0.44), False)
    # 0.33 - 0.23 有浮点误差，正好 10 个百分点的价差仍算可用
    assert Quote(0.23, 0.33, None).mid() == (pytest.approx(0.28), False)
    assert Quote(0.23, 0.33, None).spread == pytest.approx(0.10)


def test_quote_mid_falls_back_to_last_trade():
    assert Quote(0.12, 0.44, 0.31).mid() == (0.31, True)
    assert Quote(0.41, 0.47, 0.52).mid(max_spread=0.05) == (0.52, True)
    assert Quote(None, 0.66, 0.61).mid() == (0.61, True)
    assert Quote(0.58, 0.54, 0.57).mid() == (0.57, True)  # 买价高于卖价，报价错乱
    assert Quote(None, None, None).mid() is None
    assert Quote(0.12, 0.44, None).mid() is None
    assert Quote(None, 0.44, None).spread is None
