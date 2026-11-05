"""市场板块的规则文件 config/markets.yaml：宽松读取和文件指纹。

读取规则（和 digest.config 的严格读取不同，这里出错绝不能影响简报）：
- 不认识的键忽略，缺的键用默认值，值为空（null）也按缺省处理。
- 某个键的值写错时用默认值，并在 warnings 里记一条中文提示。提示只写键的位置和应该怎么写，不带写错的值。
- 日程表和合约列表里写错的单行跳过并提示，其余照用。
- 文件不存在、不是合法的 YAML 或顶层不是映射时，error 写明原因，其余全是默认值，
  调用方应停用板块并在 Summary 写 DISABLED_STATUS。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from digest.config import CONFIG_DIR

log = logging.getLogger("digest.markets")

RULES_PATH = CONFIG_DIR / "markets.yaml"
DISABLED_STATUS = "市场板块：规则文件有误，未启用"

AUDIENCES = ("owner_only", "group")
MOVER_MODES = ("shadow", "show")
SERIES_KINDS = ("fed", "ladder", "buckets", "binary")
UNITS = ("pct", "jobs")
RELEASE_KINDS = ("cpi", "empsit", "fomc")
# 公布日期离参考月月底太远多半是写错了年份或月份
MAX_RELEASE_LAG_DAYS = 120

_SERIES = re.compile(r"^[A-Z0-9]+$")
_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class BudgetSettings:
    """一次运行能用的网络预算，交给 net.Budget 和 net.Fetcher。"""

    seconds: float = 150.0  # 总时长
    requests: int = 80  # 总请求次数（重试也算）
    timeout: float = 10.0  # 单次请求超时
    max_bytes: int = 10_000_000  # 单个响应的大小上限
    min_interval: float = 0.0  # 两次请求之间至少隔多少秒


def _replay_budget() -> BudgetSettings:
    return BudgetSettings(seconds=3000.0, requests=4000, timeout=15.0, min_interval=1.0)


@dataclass(frozen=True)
class SeriesRule:
    """Kalshi 的一类合约。"""

    series: str  # 系列代码，如 KXCPI
    kind: str  # fed 议息（互斥结果）| ladder 阶梯（一串“高于 k”）| buckets 互斥区间 | binary 单个是非
    name: str  # 显示用的中文名
    grid: float | None = None  # 公布值的最小单位（如 0.1 个百分点）；None 表示连续（非农人数）
    unit: str | None = None  # pct 百分比 | jobs 人数
    release: str | None = None  # 用哪张官方日程表：cpi | empsit | fomc


DEFAULT_KALSHI_HOSTS = (
    "https://external-api.kalshi.com/trade-api/v2",
    "https://api.elections.kalshi.com/trade-api/v2",
)
DEFAULT_KEY_DATA = (
    SeriesRule("KXFEDDECISION", "fed", "美联储议息", release="fomc"),
    SeriesRule("KXCPI", "ladder", "CPI 环比", grid=0.1, unit="pct", release="cpi"),
    SeriesRule("KXCPICORE", "ladder", "核心 CPI 环比", grid=0.1, unit="pct", release="cpi"),
    SeriesRule("KXPAYROLLS", "ladder", "非农新增就业", unit="jobs", release="empsit"),
    SeriesRule("KXU3", "ladder", "失业率", grid=0.1, unit="pct", release="empsit"),
)
DEFAULT_WEEKLY = (
    SeriesRule("KXINXY", "buckets", "标普500 年底"),
    SeriesRule("KXRECSSNBER", "binary", "美国实际 GDP 连续两季负增长"),
)


@dataclass(frozen=True)
class KalshiSettings:
    hosts: tuple[str, ...] = DEFAULT_KALSHI_HOSTS  # 末尾不带 /，按顺序尝试
    key_data: tuple[SeriesRule, ...] = DEFAULT_KEY_DATA
    weekly: tuple[SeriesRule, ...] = DEFAULT_WEEKLY
    days_ahead: int = 7  # 早间版列出未来几天内公布的数据
    evening_hours: float = 12.0  # 晚间版只列出这么多小时内公布的数据
    min_open_interest: float = 5000.0  # 一个事件所有合约的持仓合计至少这么多张
    max_spread: float = 0.10  # 价差超过这个数的合约改用成交价，并标注（成交价）


DEFAULT_TIMES_ET = {"cpi": time(8, 30), "empsit": time(8, 30), "fomc": time(14, 0)}


@dataclass(frozen=True)
class Releases:
    """官方日程。cpi / empsit 按参考月 (年, 月) 查公布日期；fomc 是排好序的声明发布日。
    默认是空表：表里没有的月份由 schedule 退回 Kalshi 合约的收盘日期。"""

    times_et: dict[str, time] = field(default_factory=lambda: dict(DEFAULT_TIMES_ET))  # 美东时间
    cpi: dict[tuple[int, int], date] = field(default_factory=dict)
    empsit: dict[tuple[int, int], date] = field(default_factory=dict)
    fomc: tuple[date, ...] = ()


@dataclass(frozen=True)
class Lopsided:
    """“定价一边倒”：快要结算、一边的价格已经很高（或很低）的事件。"""

    min_days: float = 2.0
    max_days: float = 100.0
    max_spread: float = 0.03
    min_liquidity: float = 10000.0
    hi: tuple[float, float] = (0.85, 0.97)
    lo: tuple[float, float] = (0.03, 0.15)
    max_items: int = 2


@dataclass(frozen=True)
class Movers:
    """过去一天价格变化大的市场，逐项核实后才算数。"""

    mode: str = "shadow"  # shadow 只在后台记录 | show 显示在板块里
    min_days: float = 2.0
    max_days: float = 180.0
    max_spread: float = 0.03
    min_liquidity: float = 50000.0
    prefilter_change: float = 0.05
    min_change: float = 0.05
    min_spread_multiple: float = 2.0
    persist_points: int = 2
    min_depth_usd: float = 5000.0
    min_wallets: int = 5
    max_top_share: float = 0.5
    max_candidates: int = 4


DEFAULT_TAGS = {"finance": 120, "economy": 100328, "fed-rates": 100196}


@dataclass(frozen=True)
class PolymarketSettings:
    tags: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TAGS))  # 标签 slug -> tag_id
    max_pages: int = 2
    exclude_tags: tuple[str, ...] = ("sports", "esports", "mention-markets", "up-or-down")
    # 不分大小写的子串匹配，所以 " say " 两边的空格是有意的
    exclude_words: tuple[str, ...] = (
        "mention", "up or down", " say ", "tweet", " die ", " dies ", " died ", " dead ", "death", "assassinat",
        " killed ",
    )
    lopsided: Lopsided = field(default_factory=Lopsided)
    movers: Movers = field(default_factory=Movers)


@dataclass(frozen=True)
class VixSettings:
    vix_url: str = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    vix9d_url: str = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv"
    max_age_days: int = 5


@dataclass(frozen=True)
class NowcastSettings:
    url: str = "https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_month.json"


DEFAULT_WITNESS_SERIES = {"cpi": "CUSR0000SA0", "core": "CUSR0000SA0L1E", "payrolls": "CES0000000001", "u3": "LNS14000000"}


@dataclass(frozen=True)
class WitnessSettings:
    """核对结果用的官方数据。"""

    bls_v1: str = "https://api.bls.gov/publicAPI/v1/timeseries/data/"  # 后面直接接序列代码
    series: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_WITNESS_SERIES))
    nyfed: str = "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/10.json"


DEFAULT_BANDS = (
    (5, "几乎不可能"),
    (20, "很不可能"),
    (44, "不太可能"),
    (55, "大致五五开"),
    (79, "较可能"),
    (94, "很可能"),
    (100, "几乎确定"),
)
DEFAULT_BANNED_WORDS = (
    "买入", "卖出", "持有", "做多", "做空", "看涨", "看跌", "看多", "看空", "利好", "利空", "机会", "推荐", "建议",
    "加仓", "减仓", "抄底", "稳赚", "必涨", "必跌", "翻倍", "牛股", "黑马", "内幕", "喊单", "高把握", "保证收益", "无风险",
)


@dataclass(frozen=True)
class MarketsSettings:
    version: int = 1
    audience: str = "owner_only"  # owner_only | group（group 时只放 Polymarket）
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    replay_budget: BudgetSettings = field(default_factory=_replay_budget)
    kalshi: KalshiSettings = field(default_factory=KalshiSettings)
    releases: Releases = field(default_factory=Releases)
    polymarket: PolymarketSettings = field(default_factory=PolymarketSettings)
    vix: VixSettings = field(default_factory=VixSettings)
    nowcast: NowcastSettings = field(default_factory=NowcastSettings)
    witnesses: WitnessSettings = field(default_factory=WitnessSettings)
    # 概率用词：四舍五入后的整数百分比不超过上限时用这个词，按上限从小到大排
    bands: tuple[tuple[int, str], ...] = DEFAULT_BANDS
    banned_words: tuple[str, ...] = DEFAULT_BANNED_WORDS
    warnings: tuple[str, ...] = ()  # 写错后改用默认值或跳过的地方（中文，不含写错的值）
    error: str | None = None  # 有值时整个规则文件不可用，板块不启用


def rules_hash(path: Path = RULES_PATH) -> str:
    """规则文件的指纹：文件字节的 sha256（十六进制）。文件读不了时抛 OSError。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_settings(path: Path | None = None) -> MarketsSettings:
    """读取规则文件，永远返回 MarketsSettings；文件整体不可用时 error 有值。"""
    path = RULES_PATH if path is None else Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _failed("规则文件不存在")
    except UnicodeDecodeError:
        return _failed("规则文件不是 UTF-8 编码")
    except OSError as exc:
        return _failed(f"规则文件读不了：{type(exc).__name__}")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"（第 {mark.line + 1} 行附近）" if mark is not None else ""
        return _failed(f"规则文件不是合法的 YAML{where}")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return _failed("规则文件的顶层应该是键值映射")
    warnings: list[str] = []
    settings = _parse(_Block(raw, "", warnings))
    for warning in warnings:
        log.warning("市场规则文件：%s", warning)
    return replace(settings, warnings=tuple(warnings))


def _failed(reason: str) -> MarketsSettings:
    log.warning("市场规则文件有误，板块不启用：%s", reason)
    return MarketsSettings(error=reason)


# ---- 各块的读取 ----


def _parse(root: _Block) -> MarketsSettings:
    d = MarketsSettings()
    version = root.number("version", d.version, minimum=1, integer=True)
    if version != 1:
        root.warn(root.where("version"), "只认识版本 1", "按版本 1 读取")
        version = 1
    return MarketsSettings(
        version=version,
        audience=root.choice("audience", d.audience, AUDIENCES),
        budget=_budget(root.sub("budget"), d.budget),
        replay_budget=_budget(root.sub("replay_budget"), d.replay_budget),
        kalshi=_kalshi(root.sub("kalshi")),
        releases=_releases(root.sub("releases")),
        polymarket=_polymarket(root.sub("polymarket")),
        vix=_vix(root.sub("vix")),
        nowcast=NowcastSettings(url=root.sub("nowcast").url("url", NowcastSettings().url)),
        witnesses=_witnesses(root.sub("witnesses")),
        bands=_bands(root),
        banned_words=root.strings("banned_words", d.banned_words),
    )


def _budget(b: _Block, d: BudgetSettings) -> BudgetSettings:
    return BudgetSettings(
        seconds=b.number("seconds", d.seconds, above=0),
        requests=b.number("requests", d.requests, minimum=1, integer=True),
        timeout=b.number("timeout", d.timeout, above=0),
        max_bytes=b.number("max_bytes", d.max_bytes, minimum=1, integer=True),
        min_interval=b.number("min_interval", d.min_interval, minimum=0),
    )


def _kalshi(b: _Block) -> KalshiSettings:
    d = KalshiSettings()
    return KalshiSettings(
        hosts=_hosts(b, d.hosts),
        key_data=_series_rules(b, "key_data", d.key_data, need_release=True),
        weekly=_series_rules(b, "weekly", d.weekly, need_release=False),
        days_ahead=b.number("days_ahead", d.days_ahead, minimum=0, integer=True),
        evening_hours=b.number("evening_hours", d.evening_hours, above=0),
        min_open_interest=b.number("min_open_interest", d.min_open_interest, minimum=0),
        max_spread=b.number("max_spread", d.max_spread, above=0, maximum=1),
    )


def _hosts(b: _Block, default: tuple[str, ...]) -> tuple[str, ...]:
    value = b.data.get("hosts")
    if value is None:
        return default
    if not isinstance(value, list) or not value or not all(_is_https(v) for v in value):
        b.warn(b.where("hosts"), "应该是 https 地址的列表")
        return default
    return tuple(v.strip().rstrip("/") for v in value)


def _series_rules(b: _Block, key: str, default: tuple[SeriesRule, ...], need_release: bool) -> tuple[SeriesRule, ...]:
    value = b.data.get(key)
    if value is None:
        return default
    if not isinstance(value, list):
        b.warn(b.where(key), "应该是列表")
        return default
    rules = []
    for n, entry in enumerate(value, 1):
        rule, problem = _series_rule(entry, need_release)
        if rule is None:
            b.warn(f"{b.where(key)} 第 {n} 项", problem, "已跳过这一项")
        else:
            rules.append(rule)
    return tuple(rules)


def _series_rule(entry: Any, need_release: bool) -> tuple[SeriesRule | None, str]:
    if not isinstance(entry, dict):
        return None, "应该是键值映射"
    series = entry.get("series")
    if not isinstance(series, str) or not _SERIES.match(series.strip()):
        return None, "series 应该是大写字母和数字组成的系列代码"
    kind = entry.get("kind")
    if kind not in SERIES_KINDS:
        return None, f"kind 应该是 {' / '.join(SERIES_KINDS)} 之一"
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "缺少 name"
    grid = entry.get("grid")
    if grid is not None:
        grid = _as_number(grid, integer=False)
        if grid is None or grid <= 0:
            return None, "grid 应该是大于 0 的数"
    unit = entry.get("unit")
    if (unit is not None or kind == "ladder") and unit not in UNITS:
        return None, f"unit 应该是 {' / '.join(UNITS)} 之一"
    release = entry.get("release")
    if (release is not None or need_release) and release not in RELEASE_KINDS:
        return None, f"release 应该是 {' / '.join(RELEASE_KINDS)} 之一"
    return SeriesRule(series.strip(), kind, name.strip(), grid=grid, unit=unit, release=release), ""


def _releases(b: _Block) -> Releases:
    times = dict(DEFAULT_TIMES_ET)
    raw_times = b.sub("times_et")
    for kind in RELEASE_KINDS:
        value = raw_times.data.get(kind)
        if value is None:
            continue
        parsed = _as_clock(value)
        if parsed is None:
            raw_times.warn(raw_times.where(kind), "应该是带引号的 \"HH:MM\"")
        else:
            times[kind] = parsed
    return Releases(times_et=times, cpi=_month_table(b, "cpi"), empsit=_month_table(b, "empsit"), fomc=_date_list(b, "fomc"))


def _month_table(b: _Block, key: str) -> dict[tuple[int, int], date]:
    value = b.data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        b.warn(b.where(key), "应该是“参考月: 公布日期”的映射")
        return {}
    table: dict[tuple[int, int], date] = {}
    for n, (month, day) in enumerate(value.items(), 1):
        period = _as_month(month)
        released = _as_date(day)
        if period is None or released is None or not _plausible_release(period, released):
            b.warn(f"{b.where(key)} 第 {n} 行", "应该是 YYYY-MM: YYYY-MM-DD，公布日期在参考月之后", "已跳过这一行")
            continue
        table[period] = released
    return table


def _date_list(b: _Block, key: str) -> tuple[date, ...]:
    value = b.data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        b.warn(b.where(key), "应该是日期列表")
        return ()
    days = set()
    for n, item in enumerate(value, 1):
        parsed = _as_date(item)
        if parsed is None:
            b.warn(f"{b.where(key)} 第 {n} 项", "应该是 YYYY-MM-DD", "已跳过这一项")
        else:
            days.add(parsed)
    return tuple(sorted(days))


def _polymarket(b: _Block) -> PolymarketSettings:
    d = PolymarketSettings()
    return PolymarketSettings(
        tags=_tags(b, d.tags),
        max_pages=b.number("max_pages", d.max_pages, minimum=1, integer=True),
        exclude_tags=b.strings("exclude_tags", d.exclude_tags),
        exclude_words=b.strings("exclude_words", d.exclude_words),
        lopsided=_lopsided(b.sub("lopsided")),
        movers=_movers(b.sub("movers")),
    )


def _tags(b: _Block, default: dict[str, int]) -> dict[str, int]:
    value = b.data.get("tags")
    if value is None:
        return dict(default)
    tags = _parse_tags(value)
    if tags is None:
        b.warn(b.where("tags"), "应该是“标签名: 正整数编号”的映射")
        return dict(default)
    return tags


def _parse_tags(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict) or not value:
        return None
    tags: dict[str, int] = {}
    for slug, tag_id in value.items():
        n = _as_number(tag_id, integer=True)
        if not (isinstance(slug, str) and slug.strip() and n is not None and n > 0):
            return None
        tags[slug.strip()] = n
    return tags


def _lopsided(b: _Block) -> Lopsided:
    d = Lopsided()
    min_days, max_days = _day_range(b, d.min_days, d.max_days)
    return Lopsided(
        min_days=min_days,
        max_days=max_days,
        max_spread=b.number("max_spread", d.max_spread, above=0, maximum=1),
        min_liquidity=b.number("min_liquidity", d.min_liquidity, minimum=0),
        hi=b.pair("hi", d.hi),
        lo=b.pair("lo", d.lo),
        max_items=b.number("max_items", d.max_items, minimum=0, integer=True),
    )


def _movers(b: _Block) -> Movers:
    d = Movers()
    min_days, max_days = _day_range(b, d.min_days, d.max_days)
    return Movers(
        mode=b.choice("mode", d.mode, MOVER_MODES),
        min_days=min_days,
        max_days=max_days,
        max_spread=b.number("max_spread", d.max_spread, above=0, maximum=1),
        min_liquidity=b.number("min_liquidity", d.min_liquidity, minimum=0),
        prefilter_change=b.number("prefilter_change", d.prefilter_change, above=0, maximum=1),
        min_change=b.number("min_change", d.min_change, above=0, maximum=1),
        min_spread_multiple=b.number("min_spread_multiple", d.min_spread_multiple, minimum=0),
        persist_points=b.number("persist_points", d.persist_points, minimum=1, integer=True),
        min_depth_usd=b.number("min_depth_usd", d.min_depth_usd, minimum=0),
        min_wallets=b.number("min_wallets", d.min_wallets, minimum=0, integer=True),
        max_top_share=b.number("max_top_share", d.max_top_share, above=0, maximum=1),
        max_candidates=b.number("max_candidates", d.max_candidates, minimum=0, integer=True),
    )


def _day_range(b: _Block, min_default: float, max_default: float) -> tuple[float, float]:
    low = b.number("min_days", min_default, minimum=0)
    high = b.number("max_days", max_default, minimum=0)
    if low > high:
        b.warn(f"{b.where('min_days')} / max_days", "下限大于上限")
        return min_default, max_default
    return low, high


def _vix(b: _Block) -> VixSettings:
    d = VixSettings()
    return VixSettings(
        vix_url=b.url("vix_url", d.vix_url),
        vix9d_url=b.url("vix9d_url", d.vix9d_url),
        max_age_days=b.number("max_age_days", d.max_age_days, minimum=0, integer=True),
    )


def _witnesses(b: _Block) -> WitnessSettings:
    d = WitnessSettings()
    series = dict(d.series)
    raw_series = b.sub("series")
    for name, value in raw_series.data.items():
        if not isinstance(name, str):
            continue
        if not isinstance(value, str) or not _SERIES.match(value.strip()):
            raw_series.warn(raw_series.where(name), "应该是序列代码（大写字母和数字）")
        else:
            series[name] = value.strip()
    return WitnessSettings(bls_v1=b.url("bls_v1", d.bls_v1), series=series, nyfed=b.url("nyfed", d.nyfed))


def _bands(b: _Block) -> tuple[tuple[int, str], ...]:
    value = b.data.get("bands")
    if value is None:
        return DEFAULT_BANDS
    bands = _parse_bands(value)
    if bands is None:
        b.warn(b.where("bands"), "应该是 [上限, 用词] 的列表，上限从小到大，最后一个是 100")
        return DEFAULT_BANDS
    return bands


def _parse_bands(value: Any) -> tuple[tuple[int, str], ...] | None:
    if not isinstance(value, list) or not value:
        return None
    bands: list[tuple[int, str]] = []
    for entry in value:
        if not (isinstance(entry, list) and len(entry) == 2):
            return None
        upper, word = _as_number(entry[0], integer=True), entry[1]
        if upper is None or not 0 <= upper <= 100 or (bands and upper <= bands[-1][0]):
            return None
        if not isinstance(word, str) or not word.strip():
            return None
        bands.append((upper, word.strip()))
    return tuple(bands) if bands[-1][0] == 100 else None


# ---- 通用的宽松读取 ----


class _Block:
    """规则文件里的一个映射。读取单个值，写错时记提示并返回默认值。"""

    def __init__(self, data: Mapping[Any, Any], path: str, warnings: list[str]) -> None:
        self.data = data
        self.path = path
        self.warnings = warnings

    def where(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def warn(self, where: str, reason: str, action: str = "已改用默认值") -> None:
        self.warnings.append(f"{where}：{reason}，{action}")

    def sub(self, key: str) -> _Block:
        value = self.data.get(key)
        if value is None:
            return _Block({}, self.where(key), self.warnings)
        if not isinstance(value, dict):
            self.warn(self.where(key), "应该是键值映射", "整块改用默认值")
            return _Block({}, self.where(key), self.warnings)
        return _Block(value, self.where(key), self.warnings)

    def number(
        self,
        key: str,
        default: Any,
        *,
        minimum: float | None = None,
        above: float | None = None,
        maximum: float | None = None,
        integer: bool = False,
    ) -> Any:
        value = self.data.get(key)
        if value is None:
            return default
        n = _as_number(value, integer)
        if (
            n is None
            or (minimum is not None and n < minimum)
            or (above is not None and n <= above)
            or (maximum is not None and n > maximum)
        ):
            self.warn(self.where(key), _range_text(minimum, above, maximum, integer))
            return default
        return n

    def choice(self, key: str, default: str, choices: tuple[str, ...]) -> str:
        value = self.data.get(key)
        if value is None:
            return default
        if value not in choices:
            self.warn(self.where(key), f"应该是 {' / '.join(choices)} 之一")
            return default
        return value

    def url(self, key: str, default: str) -> str:
        value = self.data.get(key)
        if value is None:
            return default
        if not _is_https(value):
            self.warn(self.where(key), "应该是 https 地址")
            return default
        return value.strip()

    def pair(self, key: str, default: tuple[float, float]) -> tuple[float, float]:
        value = self.data.get(key)
        if value is None:
            return default
        if isinstance(value, list) and len(value) == 2:
            low, high = (_as_number(v, integer=False) for v in value)
            if low is not None and high is not None and 0 <= low <= high <= 1:
                return low, high
        self.warn(self.where(key), "应该是 [下限, 上限]，都在 0 到 1 之间")
        return default

    def strings(self, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = self.data.get(key)
        if value is None:
            return default
        # 不去掉两边的空格：exclude_words 里的 " say " 靠空格区分单词
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            self.warn(self.where(key), "应该是文字列表（像 yes、no、on 这样的词要加引号）")
            return default
        return tuple(value)


def _as_number(value: Any, integer: bool) -> Any:
    """YAML 里的数字；布尔值、字符串和非有限数一律不算。integer 时只接受整数值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    if integer:
        if isinstance(value, float):
            return int(value) if value.is_integer() else None
        return value
    return float(value)


def _range_text(minimum: float | None, above: float | None, maximum: float | None, integer: bool) -> str:
    noun = "整数" if integer else "数"
    parts = []
    if above is not None:
        parts.append(f"大于 {above:g}")
    if minimum is not None:
        parts.append(f"不小于 {minimum:g}")
    if maximum is not None:
        parts.append(f"不超过 {maximum:g}")
    return f"应该是{'、'.join(parts)} 的{noun}" if parts else f"应该是{noun}"


def _is_https(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = urlsplit(value.strip())
    return parts.scheme == "https" and bool(parts.hostname)


def _as_clock(value: Any) -> time | None:
    """\"08:30\" 这样的钟点。不加引号的 14:00 会被 YAML 读成六十进制整数 840（分钟），也照样认。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return time(value // 60, value % 60) if 0 <= value < 24 * 60 else None
    if isinstance(value, str) and (m := _CLOCK.match(value.strip())):
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour < 24 and minute < 60:
            return time(hour, minute)
    return None


def _as_month(value: Any) -> tuple[int, int] | None:
    if isinstance(value, str) and (m := _MONTH.match(value.strip())):
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return year, month
    return None


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _plausible_release(period: tuple[int, int], released: date) -> bool:
    """公布日期应在参考月结束之后，且不晚于 MAX_RELEASE_LAG_DAYS 天。"""
    year, month = period
    next_month = date(year + month // 12, month % 12 + 1, 1)
    return next_month <= released <= next_month + timedelta(days=MAX_RELEASE_LAG_DAYS)
