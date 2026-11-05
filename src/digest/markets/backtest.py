"""Kalshi 历史回测：在我们推送的时刻（北京时间早上 07:30、晚上 19:30）重算已结算事件的价格，再和结算值对答案。

做法（设计文档第 8 节，contracts/kalshi-history.md 第 7 节）：
- 系列：规则文件 key_data 里的阶梯（CPI、核心 CPI、非农、失业率）和议息。只看 KX 时代的事件（事件代码以系列代码开头）。
- 事件列表走实时接口（各个年代的事件都在那里）。嵌套的合约为空（已归档）时再查这个事件的合约列表（实时库，
  查不到再查历史库），两边按合约代码去重，实时的优先。
- 公布时间 R：最早的合约收盘时间在美东是哪一天，加上固定的公布时间（劳工统计局 08:30，议息 14:00）。
  只对过去的事件这样近似。
- 快照：晚上 E0 是 R 之前最近的一个北京时间 19:30；早上 M0..M7 是 R 的北京日期往前 d 天的 07:30（只取早于 R 的）。
- 价格：每个合约取 R 前 8 天到收盘的小时 K 线；时刻 T 的报价是结束时间不晚于 min(T, 收盘 − 60 秒) 的最后一根
  （kalshi.quote_at），收盘之后的 K 线绝不使用。
- 结果：kalshi.settled_value。合约没有全部结算完毕、结果说不清的事件跳过并计数。
- 指标（每个系列、每个快照）：n，中位数平均差 mae_median，一半把握区间命中次数 covered50，区间概率平均 mass50_mean，
  RPS 平均 rps_mean；议息是市场最看好的结果对了几次 top_hit、实际结果的平均概率 p_actual_mean。另外统计所有二元合约里
  价格在 85%–97% 和 3%–15% 的有多少个、实际发生了多少个（每个合约只算一次，用最早一个有买卖报价的快照）。
- 报价先和面板一样补全一边（kalshi 的冷门档没有买单、热门档没有卖单，见 ladder.fill_one_sided），
  回测和面板的归一化、分布算法完全一致。
- 请求一个接一个发，间隔由 Fetcher 的 min_interval 拉开。被限流（429）后歇一会儿再发，连续两次请求都被限流、
  Kalshi 的地址都连不上（或预算用完）就停下，已经算出的照样汇总，标成部分完成。

安全约定：回测的数据和结果只加密保存、只发钉钉；这里的日志只有计数，没有合约代码、价格和结果。
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypeVar

from digest.markets.kalshi import Kalshi, KEvent, KMarket, fed_outcome, period_of, quote_at, settled_value
from digest.markets.ladder import Bound, Strike, build_ladder, fill_one_sided, normalize_buckets
from digest.markets.net import BudgetExceeded, FetchError, Fetcher
from digest.markets.schedule import BJT, ET
from digest.markets.settings import DEFAULT_KEY_DATA, DEFAULT_TIMES_ET, MarketsSettings, SeriesRule
from digest.markets.stats import rps
from digest.markets.types import Quote

log = logging.getLogger("digest.markets")

SUMMARY_VERSION = 1
MAX_LEAD = 7  # 早上最多往前看 7 天（M0..M7）
LABELS = ("E0",) + tuple(f"M{d}" for d in range(MAX_LEAD + 1))
EVENING = time(19, 30)  # 晚间版推送时刻（北京时间）
MORNING = time(7, 30)  # 早间版推送时刻（北京时间）
CANDLE_DAYS = 8  # K 线从公布前 8 天取起
CLOSE_GUARD = 60  # 只用结束时间不晚于收盘前 60 秒的 K 线
HOURLY = 60  # K 线周期（分钟）
DRY_EVENTS = 2  # 试运行时每个系列只算最近的两次事件
EVENT_PAGES = 3  # 事件列表最多翻几页（每页 200 个，够用很多年）
MAX_THROTTLED = 2  # 连续这么多次请求被限流就停下
THROTTLE_PAUSE = 10.0  # 被限流后先歇这么多秒再发下一个请求（计入时间预算）
REPLAY_KINDS = ("ladder", "fed")
LADDER_STRIKES = ("greater", "greater_or_equal")

# 和 render 一致：缺规则时按系列代码取默认的名称、类型和单位
_RULES = {rule.series: rule for rule in DEFAULT_KEY_DATA}
_LABEL = re.compile(r"^(E0|M[0-7])$")
_EPS = 1e-9

T = TypeVar("T")


class _Stop(Exception):
    """连续被限流、预算用完或 Kalshi 连不上，回测到此为止。只在本模块内部使用。"""


def lead_label(edition: str, lead_days: int) -> str:
    """面板上一行对应的回测快照：晚间版是 E0；早间版是 M{提前天数}，最多 M7（再远的由调用方另说“记录不足”）。"""
    if edition == "evening":
        return "E0"
    return f"M{min(max(int(lead_days), 0), MAX_LEAD)}"


def record_clause(summary: dict | None, series: str, label: str) -> str | None:
    """面板 Kalshi 行下面的灰色小字：这个系列在同一时刻过去的成绩。summary 里没有这一项时返回 None。

    阶梯："过去 23 次同一时刻（提前 3 天早上）：中位数平均差 0.07 个百分点"（非农用“万人”）；
    议息："过去 16 次同一时刻：市场最看好的结果对了 12 次"。
    """
    if not isinstance(summary, Mapping) or not _LABEL.match(str(label)):
        return None
    entries = summary.get("series")
    entry = entries.get(series) if isinstance(entries, Mapping) else None
    if not isinstance(entry, Mapping):
        return None
    labels = entry.get("labels") if isinstance(entry.get("labels"), Mapping) else entry
    stats = labels.get(label)
    if not isinstance(stats, Mapping):
        return None
    n = _whole(stats.get("n"))
    if n is None or n <= 0:
        return None
    rule = _RULES.get(series)
    kind = entry.get("kind") if isinstance(entry.get("kind"), str) else (rule.kind if rule else "ladder")
    if kind == "fed":
        hits = _whole(stats.get("top_hit"))
        if hits is None or not 0 <= hits <= n:
            return None
        return f"过去 {n} 次同一时刻：市场最看好的结果对了 {hits} 次"
    unit = entry.get("unit") if isinstance(entry.get("unit"), str) else (rule.unit if rule else None)
    mae = _number(stats.get("mae_median"))
    if mae is None or mae < 0:
        return None
    return f"过去 {n} 次同一时刻（{_lead_text(label)}）：中位数平均差 {_amount(mae, unit)}"


def covers(lo: Bound, hi: Bound, x: float) -> bool:
    """x 是否落在一半把握的区间 [lo, hi] 里。开口的端点按它表示的范围算：
    lo 为“不高于 k_1”时没有下限，lo 为“高于 k”时要求 x > k；hi 为“高于 k_n”时没有上限，其余要求 x ≤ hi。"""
    x = float(x)
    tol = _EPS * max(1.0, abs(x))
    if lo.open_side == "below":
        low_ok = True
    elif lo.open_side == "above":
        low_ok = x > lo.value + tol
    else:
        low_ok = x >= lo.value - tol
    high_ok = hi.open_side == "above" or x <= hi.value + tol
    return low_ok and high_ok


def run(fetcher: Fetcher, settings: MarketsSettings, now: datetime, dry: bool) -> dict:
    """跑一次回测，返回汇总（只含数字和系列代码，要加密保存，内容只发钉钉）。

    dry 为真时每个系列只算最近的两次已结算事件（CI 的冒烟测试），汇总里的 series_count / events / scored /
    requests 用来写试运行的状态行。网络错误按合约或事件跳过并计数；连续两次被限流、预算用完或 Kalshi 的地址都连不上时停下，partial 为真。
    """
    start = fetcher.requests
    job = _Job(fetcher, Kalshi(fetcher, settings.kalshi.hosts), settings)
    rules = [rule for rule in settings.kalshi.key_data if rule.kind in REPLAY_KINDS]
    series = {rule.series: _Series(rule) for rule in rules}
    try:
        for rule in rules:
            job.replay_series(rule, series[rule.series], dry)
    except _Stop:
        job.partial = True
        log.warning("市场回测：中途停下，只完成了一部分")
    summary = {
        "version": SUMMARY_VERSION,
        "generated": _iso(now),
        "dry": bool(dry),
        "partial": job.partial,
        "requests": fetcher.requests - start,
        "series_count": job.series_done,
        "events": sum(s.events for s in series.values()),
        "scored": sum(s.scored for s in series.values()),
        "skipped": sum(s.skipped for s in series.values()),
        "errors": job.errors,
        "series": {ticker: s.to_json() for ticker, s in series.items()},
        "bands": {name: band.to_json() for name, band in job.bands.items()},
    }
    log.info(
        "市场回测：%d 类合约 %d 次事件，算出 %d 次，跳过 %d 次，出错 %d 次；请求 %d 次",
        summary["series_count"], summary["events"], summary["scored"], summary["skipped"], job.errors,
        summary["requests"],
    )
    return summary


# ---- 汇总 ----


@dataclass
class _Ladder:
    """一个阶梯系列在一个快照上的成绩。"""

    errors: list[float] = field(default_factory=list)
    covered: int = 0
    masses: list[float] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "n": len(self.errors),
            "mae_median": _round(math.fsum(self.errors) / len(self.errors)),
            "covered50": self.covered,
            "mass50_mean": _round(math.fsum(self.masses) / len(self.masses)),
            "rps_mean": _round(math.fsum(self.scores) / len(self.scores)),
        }


@dataclass
class _Fed:
    """议息在一个快照上的成绩。"""

    hits: int = 0
    actual: list[float] = field(default_factory=list)

    def to_json(self) -> dict:
        mean = math.fsum(self.actual) / len(self.actual)
        return {"n": len(self.actual), "top_hit": self.hits, "p_actual_mean": _round(mean)}


@dataclass
class _Series:
    rule: SeriesRule
    events: int = 0  # 处理过的事件
    scored: int = 0  # 至少一个快照算出了成绩
    skipped: int = 0  # 结算说不清，跳过
    labels: dict[str, Any] = field(default_factory=dict)

    def stats(self, label: str) -> Any:
        if label not in self.labels:
            self.labels[label] = _Fed() if self.rule.kind == "fed" else _Ladder()
        return self.labels[label]

    def to_json(self) -> dict:
        out: dict[str, Any] = {"name": self.rule.name, "kind": self.rule.kind}
        if self.rule.unit:
            out["unit"] = self.rule.unit
        out.update(events=self.events, scored=self.scored, skipped=self.skipped)
        out["labels"] = {label: self.labels[label].to_json() for label in LABELS if label in self.labels}
        return out


@dataclass
class _Band:
    """价格落在某个区间的二元合约：个数和实际发生（结果为 yes）的个数。"""

    n: int = 0
    yes: int = 0

    def to_json(self) -> dict:
        return {"n": self.n, "yes": self.yes}


# ---- 回放 ----


class _Job:
    """一次回测的状态：客户端、限流计数、出错计数和价格区间统计。"""

    def __init__(self, fetcher: Fetcher, kalshi: Kalshi, settings: MarketsSettings) -> None:
        self.fetcher = fetcher
        self.kalshi = kalshi
        self.settings = settings
        self.max_spread = settings.kalshi.max_spread
        lopsided = settings.polymarket.lopsided
        self.ranges = {"band_hi": lopsided.hi, "band_lo": lopsided.lo}
        self.bands = {name: _Band() for name in self.ranges}
        self.throttled = 0  # 连续被限流的请求数
        self.errors = 0
        self.series_done = 0
        self.partial = False

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """发一次 Kalshi 请求并记录限流；连续 MAX_THROTTLED 次被限流、Kalshi 的地址都连不上或预算用完时抛 _Stop。

        Fetcher 对 429 已经自己重试过一次，所以这里的“一次被限流”是一个最终失败的请求。
        """
        try:
            result = fn(*args, **kwargs)
        except BudgetExceeded:
            raise _Stop from None
        except FetchError as exc:
            if self.kalshi.down:
                log.warning("市场回测：Kalshi 的地址都连不上，停下")
                raise _Stop from None
            if exc.status != 429:
                self.throttled = 0
                raise
            self.throttled += 1
            if self.throttled >= MAX_THROTTLED:
                raise _Stop from None
            self._pause()
            raise
        self.throttled = 0
        return result

    def _pause(self) -> None:
        try:
            self.fetcher.budget.check(reserve=THROTTLE_PAUSE)
        except BudgetExceeded:
            raise _Stop from None
        self.fetcher.sleep(THROTTLE_PAUSE)

    def replay_series(self, rule: SeriesRule, acc: _Series, dry: bool) -> None:
        try:
            events = self.call(self.kalshi.events, rule.series, status="settled", nested=True, max_pages=EVENT_PAGES)
        except FetchError:
            self.errors += 1
            return
        self.series_done += 1
        events = _latest([e for e in events if _kx_era(e, rule.series)])
        for event in events[:DRY_EVENTS] if dry else events:
            acc.events += 1
            try:
                outcome = self.replay_event(rule, event, acc)
            except _Stop:
                raise
            except FetchError:
                self.errors += 1
                continue
            except Exception as exc:  # 数据不对等意外：这个事件不算，其余照常
                log.warning("市场回测：一个事件没算出来（%s）", type(exc).__name__)
                self.errors += 1
                continue
            if outcome == "skipped":
                acc.skipped += 1
            elif outcome == "scored":
                acc.scored += 1

    def replay_event(self, rule: SeriesRule, event: KEvent, acc: _Series) -> str:
        """回放一个事件，返回 "scored"（至少一个快照有成绩）、"empty"（没有可用报价）或 "skipped"（结算说不清）。"""
        legs = _legs(rule.kind, self._markets(event))
        if len(legs) < 2 or any(m.status != "finalized" or m.result not in ("yes", "no") for m in legs):
            return "skipped"
        outcome = settled_value(rule.kind, legs)
        release = _release(legs, rule, self.settings)
        if outcome is None or release is None:
            return "skipped"
        snaps = _snapshots(release)
        quotes = self._quotes(event.series_ticker or rule.series, legs, snaps, release)
        if rule.kind == "fed":
            scored = self._score_fed(legs, quotes, snaps, str(outcome), acc)
        else:
            scored = self._score_ladder(rule, legs, quotes, snaps, float(outcome), acc)
        self._count_bands(legs, quotes, snaps)
        return "scored" if scored else "empty"

    def _markets(self, event: KEvent) -> list[KMarket]:
        """事件的合约：实时列表里嵌套的为准；嵌套为空（已归档）时查合约列表（实时库，再历史库），按代码去重。"""
        found = {m.ticker: m for m in event.markets}
        if not found:
            for m in self.call(self.kalshi.markets, event.event_ticker):
                found.setdefault(m.ticker, m)
        return list(found.values())

    def _quotes(
        self, series: str, legs: list[KMarket], snaps: list[tuple[str, int]], release: datetime
    ) -> dict[str, dict[str, Quote]]:
        """每个合约在每个快照时刻的报价。某个合约的 K 线取不到时跳过它（其余合约照算）。"""
        start = int((release - timedelta(days=CANDLE_DAYS)).timestamp())
        quotes: dict[str, dict[str, Quote]] = {}
        for m in legs:
            if m.close_time is None:
                continue
            close = int(m.close_time.timestamp())
            try:
                candles = self.call(self.kalshi.candles, series, m.ticker, start, close, period=HOURLY)
            except FetchError:
                self.errors += 1
                continue
            found: dict[str, Quote] = {}
            for label, ts in snaps:
                at = quote_at(candles, ts, close - CLOSE_GUARD)
                if at is not None:
                    found[label] = at[0]
            quotes[m.ticker] = found
        return quotes

    def _score_ladder(
        self,
        rule: SeriesRule,
        legs: list[KMarket],
        quotes: dict[str, dict[str, Quote]],
        snaps: list[tuple[str, int]],
        x: float,
        acc: _Series,
    ) -> bool:
        scored = False
        for label, _ in snaps:
            strikes = [
                Strike(
                    k=m.floor,
                    quote=fill_one_sided(quotes[m.ticker][label], self.max_spread),
                    inclusive=m.strike_type == "greater_or_equal",
                )
                for m in legs
                if label in quotes.get(m.ticker, {})
            ]
            try:
                dist = build_ladder(strikes, rule.grid, self.max_spread)
                if dist is None:
                    continue
                lo, hi = dist.interval50()
                error, hit, mass, score = _median_error(dist.median(), x, dist.grid), covers(lo, hi, x), \
                    dist.mass(lo, hi), rps(dist, x)
            except ValueError:
                continue  # 这个快照的阶梯算不出来；先算好四个数再记，免得只记了一半
            stats = acc.stats(label)
            stats.errors.append(error)
            stats.covered += hit
            stats.masses.append(mass)
            stats.scores.append(score)
            scored = True
        return scored

    def _score_fed(
        self,
        legs: list[KMarket],
        quotes: dict[str, dict[str, Quote]],
        snaps: list[tuple[str, int]],
        outcome: str,
        acc: _Series,
    ) -> bool:
        names = [fed_outcome(m) for m in legs]
        scored = False
        for label, _ in snaps:
            found = [quotes.get(m.ticker, {}).get(label) for m in legs]
            if any(q is None for q in found):
                continue
            # 和面板的议息一行一样：冷门结果没有买单（卖价贴着 0）时买价按 0 补，再归一化
            probs = normalize_buckets([fill_one_sided(q, self.max_spread) for q in found], self.max_spread)
            if probs is None:
                continue
            # 同一个结果可能有两条腿（旧合约里“不变”有加息 0 和降息 0 两种写法），合在一起算
            by_name: dict[str, float] = {}
            for name, p in zip(names, probs):
                by_name[name] = by_name.get(name, 0.0) + p
            top = max(by_name, key=lambda name: by_name[name])
            stats = acc.stats(label)
            stats.hits += top == outcome
            stats.actual.append(min(1.0, by_name.get(outcome, 0.0)))
            scored = True
        return scored

    def _count_bands(
        self, legs: list[KMarket], quotes: dict[str, dict[str, Quote]], snaps: list[tuple[str, int]]
    ) -> None:
        """每个合约只算一次：按时间顺序找第一个有买卖报价中间值（不是成交价）的快照。"""
        order = [label for label, _ in sorted(snaps, key=lambda s: s[1])]
        for m in legs:
            for label in order:
                quote = quotes.get(m.ticker, {}).get(label)
                priced = quote.mid(self.max_spread) if quote is not None else None
                if priced is None or priced[1]:
                    continue
                for name, (low, high) in self.ranges.items():
                    if low - _EPS <= priced[0] <= high + _EPS:
                        self.bands[name].n += 1
                        self.bands[name].yes += m.result == "yes"
                break


def _snapshots(release: datetime) -> list[tuple[str, int]]:
    """公布时间 R 对应的快照时刻（Unix 秒）：E0 是 R 之前最近的北京时间 19:30；M{d} 是 R 的北京日期往前
    d 天的 07:30，只取早于 R 的（议息在北京时间凌晨公布，当天早上的 M0 不存在）。"""
    release = _aware(release)
    day = release.astimezone(BJT).date()
    evening = datetime.combine(day, EVENING, tzinfo=BJT)
    if evening >= release:
        evening -= timedelta(days=1)
    found = [("E0", int(evening.timestamp()))]
    for d in range(MAX_LEAD + 1):
        morning = datetime.combine(day - timedelta(days=d), MORNING, tzinfo=BJT)
        if morning < release:
            found.append((f"M{d}", int(morning.timestamp())))
    return found


def _median_error(median: Bound, x: float, grid: float | None) -> float:
    """中位数和公布值的差。中位数落在阶梯外（开口）时取到那一段的最小距离：
    “不高于 v”时是 max(0, x − v)；“高于 v”时 X 至少是 v 加一格（连续时就是 v），取 max(0, 那个数 − x)。"""
    if median.open_side == "below":
        return max(0.0, x - median.value)
    if median.open_side == "above":
        return max(0.0, median.value + (grid or 0.0) - x)
    return abs(x - median.value)


def _legs(kind: str, markets: list[KMarket]) -> list[KMarket]:
    """参与计算的合约：阶梯取有门槛的“高于 k”合约；议息取认得出结果的那几条腿。"""
    if kind == "fed":
        return [m for m in markets if fed_outcome(m) is not None]
    return [m for m in markets if m.strike_type in LADDER_STRIKES and m.floor is not None]


def _release(legs: list[KMarket], rule: SeriesRule, settings: MarketsSettings) -> datetime | None:
    """过去事件的公布时间：最早的收盘时间在美东的日期 + 固定的公布时间。"""
    closes = [_aware(m.close_time) for m in legs if m.close_time is not None]
    kind = rule.release or ("fomc" if rule.kind == "fed" else "")
    clock = settings.releases.times_et.get(kind) or DEFAULT_TIMES_ET.get(kind)
    if not closes or clock is None:
        return None
    return datetime.combine(min(closes).astimezone(ET).date(), clock, tzinfo=ET)


def _kx_era(event: KEvent, series: str) -> bool:
    return event.event_ticker.upper().startswith(series.upper() + "-")


def _latest(events: list[KEvent]) -> list[KEvent]:
    """按参考期从新到旧排（同一期按 strike_date）。"""
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        events,
        key=lambda e: (period_of(e.event_ticker) or (0, 0), _aware(e.strike_date) if e.strike_date else oldest),
        reverse=True,
    )


# ---- 文本和数值 ----


def _lead_text(label: str) -> str:
    if label == "E0":
        return "公布当晚 19:30"
    days = int(label[1:])
    return "公布当天早上" if days == 0 else f"提前 {days} 天早上"


def _amount(value: float, unit: str | None) -> str:
    """平均差：百分比两位小数（个百分点）；人数换成万人，一位小数，末尾的 .0 去掉。"""
    if unit == "jobs":
        wan = (Decimal(str(value)) / 10000).quantize(Decimal("0.1"), ROUND_HALF_UP)
        text = str(wan.quantize(Decimal("1"))) if wan == wan.to_integral_value() else str(wan)
        return f"{text} 万人"
    text = str(Decimal(str(value)).quantize(Decimal("0.01"), ROUND_HALF_UP))
    return f"{text} 个百分点" if unit == "pct" else text


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _whole(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number == int(number) else None


def _round(value: float) -> float:
    """汇总里的小数留 6 位，去掉浮点尾巴。"""
    return round(value, 6) + 0.0


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return _aware(dt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
