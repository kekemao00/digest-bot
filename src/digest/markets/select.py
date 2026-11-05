"""市场板块各行的挑选和拼写（设计文档第 5 节）。这里只处理已经取到的数据，不发任何网络请求。

- 关键数据（Kalshi）：公布时间落在时间窗里的事件，阶梯合成分布给中位数和一半把握区间，议息给最可能的结果。
- 每周一看（Kalshi，周一早上）：标普500 年底区间、明年 GDP 连续两季负增长。
- 定价一边倒（Polymarket）：每个事件最多一行，挑最一边倒的那个结果。
- 过去一天的变化（Polymarket）：粗筛候选，核实后逐项判断门槛；每个候选都记一条 candidate 记录。
- 波动率（Cboe）：VIX 收盘值和 VIX9D 的比较。

拼出来的文本只进钉钉消息和加密账本，绝不写进日志或 Summary；外部文本一律经过 md_text 和 truncate。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from digest.markets.kalshi import KEvent, KMarket, fed_outcome, period_of
from digest.markets.ladder import Bound, Distribution, Strike, build_ladder, fill_one_sided, normalize_buckets
from digest.markets.polymarket import CLEAN_STATUS, Book, PMarket, excluded
from digest.markets.render import MINUS, band, fmt_value, pct
from digest.markets.schedule import BJT, ET, bjt_label, lead_days, release_for_event
from digest.markets.settings import Lopsided, MarketsSettings, SeriesRule
from digest.markets.sources import Close
from digest.markets.types import Line, Quote
from digest.render.text import md_text, truncate

# ---- 固定文本 ----

LAST_TRADE = "（成交价）"
NOWCAST_CLAUSE = "克利夫兰联储模型"
NO_REPLAY = "回测还没跑"
FAR_LEAD = "提前这么多天的记录不足"
LONGSHOT = "便宜合约历来被高估"
SHADOW = "【后台观察】"
LONGSHOT_BELOW = 15  # 整数百分比低于这个数时加上“便宜合约历来被高估”
# 议息的第二个结果至少这么多（整数百分比）才写出来，太小的只是噪声
FED_SECOND_MIN = 5
QUESTION_CHARS = 60  # 外部问题文本最多显示的字数
MAX_LEAD_DAYS = 7  # 回测只算到提前 7 天的早上

# 合约已经不能交易的状态（closed 以后都是等结果或已结算，inactive 是交易所暂停）
CLOSED_STATUSES = frozenset({"closed", "determined", "disputed", "amended", "finalized", "settled", "inactive"})
# Gamma 自带的结算进度里，还没有人提结算结果的状态（v2 市场还有 inactive）；准确的状态以 /v2/resolutions 为准
QUIET_UMA = frozenset({"", "inactive", *CLEAN_STATUS})
# 克利夫兰联储的预测对应哪一类合约
NOWCAST_FIELDS = {"KXCPI": "cpi", "KXCPICORE": "core"}
HOUR = 3600
DAY = 86400
_EPS = 1e-9

# 价格变化的各项门槛，以及预览里显示的中文名
GATES = ("change", "spread", "persist", "depth", "wallets", "top_share", "clean", "news")
GATE_NAMES = {
    "change": "变化幅度",
    "spread": "相对价差",
    "persist": "持续性",
    "depth": "盘口深度",
    "wallets": "钱包数",
    "top_share": "单个钱包占比",
    "clean": "结算状态",
    "news": "对应新闻",
}


# ---- 通用 ----


def iso(dt: datetime | None) -> str | None:
    """账本里的时间：UTC 的 ISO 字符串（YYYY-MM-DDTHH:MM:SSZ）；不带时区的按 UTC 处理。"""
    if dt is None:
        return None
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def question_text(zh: str | None, question: str) -> str:
    """显示用的问题：有中文译文用译文，否则用英文原文；转义并截到 60 字。"""
    return md_text(truncate(zh or question, QUESTION_CHARS))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _day_label(dt: datetime) -> str:
    local = _aware(dt).astimezone(BJT)
    return f"{local.month}月{local.day}日"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _period_text(period: tuple[int, int] | None) -> str | None:
    return f"{period[0]}-{period[1]:02d}" if period else None


def _quote_row(quote: Quote, mid: float | None) -> dict:
    return {"bid": quote.bid, "ask": quote.ask, "last": quote.last, "mid": mid}


def _tradable(m: KMarket, now: datetime) -> bool:
    return m.status not in CLOSED_STATUSES and (m.close_time is None or _aware(m.close_time) > now)


def _earliest_close(markets: Sequence[KMarket]) -> datetime | None:
    closes = [_aware(m.close_time) for m in markets if m.close_time is not None]
    return min(closes) if closes else None


# ---- 关键数据（Kalshi） ----


def window_end(now: datetime, edition: str, settings: MarketsSettings) -> datetime:
    """早间版看未来 days_ahead 天，晚间版只看 evening_hours 小时内（今晚）。"""
    if edition == "evening":
        return now + timedelta(hours=settings.kalshi.evening_hours)
    return now + timedelta(days=settings.kalshi.days_ahead)


def key_events(
    rule: SeriesRule, events: Sequence[KEvent], settings: MarketsSettings, now: datetime, edition: str
) -> list[tuple[KEvent, datetime]]:
    """公布时间 R 满足 now < R <= 时间窗末尾的事件，按 R 排好；早间版的议息另外总是带上下一次。"""
    if rule.release is None:
        return []
    found: list[tuple[KEvent, datetime]] = []
    seen: set[str] = set()
    for event in events:
        if event.event_ticker in seen:
            continue
        seen.add(event.event_ticker)
        release, _source = release_for_event(settings, rule.release, event)
        if release is not None and release > now:
            found.append((event, release))
    found.sort(key=lambda er: er[1])
    end = window_end(now, edition, settings)
    chosen = [(e, r) for e, r in found if r <= end]
    if rule.kind == "fed" and edition == "morning" and found and not chosen:
        chosen = [found[0]]
    return chosen


def lead_of(now: datetime, release: datetime, edition: str) -> int:
    """回测里对应的提前天数：晚间版是当晚（0），早间版是北京时间的日历日差。"""
    return 0 if edition == "evening" else lead_days(now, release)


def replay_sub(clause: str | None, has_summary: bool, lead: int) -> str:
    """关键数据下面的灰色小字：回测成绩，或者为什么没有。"""
    if lead > MAX_LEAD_DAYS:
        return FAR_LEAD
    if not has_summary:
        return NO_REPLAY
    return clause or FAR_LEAD


def nowcast_value(rule: SeriesRule, nowcasts: Mapping[tuple[int, int], Any], period: tuple[int, int] | None,
                  now: datetime, max_age_days: int) -> float | None:
    """CPI 和核心 CPI 那一期的克利夫兰联储预测；没有、过时或不是这两类时返回 None。"""
    name = NOWCAST_FIELDS.get(rule.series)
    if name is None or period is None:
        return None
    nowcast = nowcasts.get(period)
    if nowcast is None or nowcast.as_of is None:
        return None
    if (_aware(now).astimezone(ET).date() - nowcast.as_of).days > max_age_days:
        return None
    return _finite(getattr(nowcast, name, None))


def key_line(
    rule: SeriesRule,
    event: KEvent,
    release: datetime,
    now: datetime,
    edition: str,
    settings: MarketsSettings,
    nowcast: float | None = None,
    sub: str | None = None,
) -> Line | None:
    """一个事件一行；持仓太少、已经收盘或报价拼不出分布时返回 None。"""
    if event.open_interest < settings.kalshi.min_open_interest:
        return None
    markets = [m for m in event.markets if _tradable(m, now)]
    close = _earliest_close(markets)
    expires = min(release, close) if close is not None else release
    if not markets or expires <= now:
        return None
    lead = lead_of(now, release, edition)
    if rule.kind == "fed":
        return _fed_line(rule, event, markets, release, expires, lead, settings, sub)
    if rule.kind == "ladder":
        return _ladder_line(rule, event, markets, release, expires, lead, settings, nowcast, sub)
    return None


def bound_text(bound: Bound, unit: str | None) -> str:
    """分位数的显示：开口的写成“不高于 / 高于”。"""
    value = fmt_value(unit or "", bound.value)
    if bound.open_side == "below":
        return f"不高于 {value}"
    if bound.open_side == "above":
        return f"高于 {value}"
    return value


def bound_row(bound: Bound) -> dict:
    """区间端点写进账本的样子：{"value": 数值, "open_side": None | "below" | "above"}（score 认这种写法）。"""
    return {"value": bound.value, "open_side": bound.open_side}


def dist_row(dist: Distribution) -> dict:
    """分布写进账本的样子：阈值、修正后的 P(X > k)、中位数、一半把握区间和它的概率。"""
    median = dist.median()
    q25, q75 = dist.interval50()
    try:
        mass50: float | None = dist.mass(q25, q75)
    except ValueError:
        mass50 = None
    return {
        "ks": list(dist.ks),
        "s": list(dist.survival),
        "grid": dist.grid,
        "used_last": dist.used_last,
        "median": bound_row(median),
        "q25": bound_row(q25),
        "q75": bound_row(q75),
        "mass50": mass50,
    }


def _label(rule: SeriesRule, period: tuple[int, int] | None) -> str:
    month = f"{period[1]}月" if period else ""
    return f"美国{month}{rule.name}"


def _ladder_line(rule, event, markets, release, expires, lead, settings, nowcast, sub) -> Line | None:
    used = [m for m in markets if m.strike_type in ("greater", "greater_or_equal") and m.floor is not None]
    max_spread = settings.kalshi.max_spread
    # 阶梯两端常常只有一边报价（最低一档没有卖单、最高一档没有买单），先补全，免得用很久以前的成交价
    strikes = [
        Strike(m.floor, fill_one_sided(m.quote, max_spread), inclusive=m.strike_type == "greater_or_equal")
        for m in used
    ]
    dist = build_ladder(strikes, rule.grid, max_spread)
    if dist is None:
        return None
    median = dist.median()
    q25, q75 = dist.interval50()
    low, high = bound_text(q25, rule.unit), bound_text(q75, rule.unit)
    interval = f"一半把握就在 {low}" if low == high else f"一半把握 {low} 到 {high}"
    period = period_of(event.event_ticker)
    label = _label(rule, period)
    text = f"· {bjt_label(release)} {md_text(label)}：中位数 {bound_text(median, rule.unit)}（{interval}）"
    if dist.used_last:
        text += LAST_TRADE
    if nowcast is not None:
        text += f"；{NOWCAST_CLAUSE} {fmt_value('pct', nowcast)}"
    row = {
        "type": "shown",
        "venue": "kalshi",
        "section": "key",
        "key": event.event_ticker,
        "series": rule.series,
        "kind": rule.kind,
        "unit": rule.unit,
        "period": _period_text(period),
        "question": label,
        "dist": dist_row(dist),
        "strikes": [
            {"ticker": m.ticker, "k": m.floor, "bid": m.quote.bid, "ask": m.quote.ask, "last": m.quote.last}
            for m in used
        ],
        "nowcast": nowcast,
        "release": iso(release),
        "expires": iso(expires),
        "lead": lead,
    }
    return Line("key", text, sub=sub, expires=expires, rows=(row,))


def _fed_line(rule, event, markets, release, expires, lead, settings, sub) -> Line | None:
    legs = [(m, name) for m in markets if (name := fed_outcome(m))]
    max_spread = settings.kalshi.max_spread
    quotes = [fill_one_sided(m.quote, max_spread) for m, _ in legs]
    ps = normalize_buckets(quotes, max_spread)
    if ps is None:
        return None
    order = sorted(range(len(legs)), key=lambda i: -ps[i])
    top = order[0]
    top_market, top_name = legs[top]
    p = pct(ps[top])
    text = f"· {bjt_label(release)} {md_text(rule.name)}：{top_name} {band(p, settings.bands)}（{p}%）"
    if len(order) > 1 and (p2 := pct(ps[order[1]])) >= FED_SECOND_MIN:
        text += f"，{legs[order[1]][1]} {p2}%"
    mids = [q.mid(max_spread) for q in quotes]
    if any(m is not None and m[1] for m in mids):
        text += LAST_TRADE
    top_mid = mids[top][0] if mids[top] else None
    row = {
        "type": "shown",
        "venue": "kalshi",
        "section": "key",
        "key": top_market.ticker,
        "event": event.event_ticker,
        "series": rule.series,
        "kind": rule.kind,
        "period": _period_text(period_of(event.event_ticker)),
        "question": f"{rule.name}：{top_name}",
        "p": p,
        "quote": _quote_row(top_market.quote, top_mid),
        "outcomes": [{"key": m.ticker, "name": name, "p": ps[i]} for i, (m, name) in enumerate(legs)],
        "release": iso(release),
        "expires": iso(expires),
        "lead": lead,
    }
    return Line("key", text, sub=sub, expires=expires, rows=(row,))


# ---- 每周一看（Kalshi，周一早上） ----


def weekly_events(rule: SeriesRule, events: Sequence[KEvent], now: datetime) -> list[KEvent]:
    """标普500 年底：今年的（12 月起再加明年的）；GDP 连续两季负增长：明年的。按年份排。"""
    local = _aware(now).astimezone(BJT)
    if rule.kind == "buckets":
        years = {local.year} | ({local.year + 1} if local.month == 12 else set())
    elif rule.kind == "binary":
        years = {local.year + 1}
    else:
        return []
    chosen: dict[str, tuple[int, KEvent]] = {}
    for event in events:
        if not event.event_ticker.startswith(rule.series + "-"):
            continue
        period = period_of(event.event_ticker)
        if period is not None and period[0] in years and event.event_ticker not in chosen:
            chosen[event.event_ticker] = (period[0], event)
    return [event for _, event in sorted(chosen.values(), key=lambda ye: (ye[0], ye[1].event_ticker))]


def weekly_line(rule: SeriesRule, event: KEvent, now: datetime, settings: MarketsSettings) -> Line | None:
    if event.open_interest < settings.kalshi.min_open_interest:
        return None
    markets = [m for m in event.markets if _tradable(m, now)]
    period = period_of(event.event_ticker)
    if not markets or period is None:
        return None
    if rule.kind == "buckets":
        return _buckets_line(rule, event, markets, period[0], now, settings)
    if rule.kind == "binary":
        return _binary_line(rule, event, markets, period[0], settings)
    return None


def _points(value: float) -> str:
    """指数点位：区间上沿 4199.99 这样的写成 4200。"""
    nearest = round(value)
    if abs(value - nearest) <= 0.02:
        return str(int(nearest))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _bucket_bounds(m: KMarket) -> tuple[float | None, float | None] | None:
    if m.strike_type == "less" and m.cap is not None:
        return None, m.cap
    if m.strike_type == "between" and m.floor is not None and m.cap is not None:
        return m.floor, m.cap
    if m.strike_type in ("greater", "greater_or_equal") and m.floor is not None:
        return m.floor, None
    return None


def _bucket_text(lo: float | None, hi: float | None) -> str:
    if lo is None:
        return f"{_points(hi)} 点以下"
    if hi is None:
        return f"{_points(lo)} 点以上"
    return f"{_points(lo)}–{_points(hi)} 点"


def _year_end_name(name: str, year: int, now: datetime) -> str:
    """“标普500 年底” -> “标普500 今年年底” / “标普500 明年年底”。"""
    this_year = _aware(now).astimezone(BJT).year
    word = "今年" if year == this_year else "明年" if year == this_year + 1 else f"{year} 年"
    base = name[: -len("年底")].rstrip() if name.endswith("年底") else name
    return f"{base} {word}年底"


def _first_reaching(ps: Sequence[float], q: float) -> int:
    total = 0.0
    for i, p in enumerate(ps):
        total += p
        if total >= q - _EPS:
            return i
    return len(ps) - 1


def _buckets_line(rule, event, markets, year, now, settings) -> Line | None:
    bounds = [_bucket_bounds(m) for m in markets]
    if any(b is None for b in bounds):
        return None
    max_spread = settings.kalshi.max_spread
    quotes = [fill_one_sided(m.quote, max_spread) for m in markets]
    ps = normalize_buckets(quotes, max_spread)
    if ps is None:
        return None
    mid_i, low_i, high_i = (_first_reaching(ps, q) for q in (0.5, 0.25, 0.75))
    low_lo, low_hi = bounds[low_i]
    high_lo, high_hi = bounds[high_i]
    start = _points(low_lo) if low_lo is not None else f"{_points(low_hi)} 以下"
    end = _points(high_hi) if high_hi is not None else f"{_points(high_lo)} 以上"
    name = _year_end_name(rule.name, year, now)
    text = f"· {md_text(name)}：最可能在 {_bucket_text(*bounds[mid_i])}（一半把握 {start} 到 {end} 点）"
    if any((m := q.mid(max_spread)) is not None and m[1] for q in quotes):
        text += LAST_TRADE
    expires = _earliest_close(markets)
    # 和阶梯一样记成一个区间：年底收盘落在“一半把握”那几个区间里算应验，概率是这几个区间的概率之和
    low = Bound(low_lo, None) if low_lo is not None else Bound(low_hi, "below")
    high = Bound(high_hi, None) if high_hi is not None else Bound(high_lo, "above")
    median_lo, median_hi = bounds[mid_i]
    row = {
        "type": "shown",
        "venue": "kalshi",
        "section": "weekly",
        "key": event.event_ticker,
        "series": rule.series,
        "kind": rule.kind,
        "period": _period_text(period_of(event.event_ticker)),
        "question": name,
        "dist": {
            "q25": bound_row(low),
            "q75": bound_row(high),
            "mass50": min(1.0, sum(ps[low_i : high_i + 1])),
            "median_bucket": {"lo": median_lo, "hi": median_hi},
        },
        "buckets": [
            {"ticker": m.ticker, "lo": lo, "hi": hi, "p": p} for m, (lo, hi), p in zip(markets, bounds, ps)
        ],
        "expires": iso(expires),
    }
    return Line("weekly", text, expires=expires, rows=(row,))


def _binary_line(rule, event, markets, year, settings) -> Line | None:
    market = markets[0]
    priced = market.quote.mid(settings.kalshi.max_spread)
    if priced is None:
        return None
    value, used_last = priced
    p = pct(value)
    # 合约规则是“前一年或这一年里连续两个季度负增长”（KXRECSSNBER-27 管 2026 和 2027 两年），按规则写两年
    name = f"{year - 1} 到 {year} 年{rule.name}"
    text = f"· {md_text(name)}：{band(p, settings.bands)}（{p}%）" + (LAST_TRADE if used_last else "")
    expires = _aware(market.close_time) if market.close_time is not None else None
    row = {
        "type": "shown",
        "venue": "kalshi",
        "section": "weekly",
        "key": market.ticker,
        "event": event.event_ticker,
        "series": rule.series,
        "kind": rule.kind,
        "period": _period_text(period_of(event.event_ticker)),
        "question": name,
        "p": p,
        "quote": _quote_row(market.quote, value),
        "expires": iso(expires),
    }
    return Line("weekly", text, expires=expires, rows=(row,))


# ---- 定价一边倒（Polymarket） ----


def _yes_no(m: PMarket) -> bool:
    """标准的是非市场：结果正好是 Yes、No 两个（第 0 个是 YES），token 也正好两个。
    结果是两个队名之类的市场，第 0 个 token 的价格不是“问题成立”的概率，不能这样显示。"""
    outcomes = tuple(o.strip().lower() for o in m.outcomes)
    return outcomes == ("yes", "no") and len(m.token_ids) == 2


def _open_market(m: PMarket, now: datetime, min_days: float, max_days: float) -> bool:
    if m.closed or not m.active or m.end is None or not _yes_no(m):
        return False
    days = (_aware(m.end) - now).total_seconds() / DAY
    return min_days <= days <= max_days


def _gamma_spread(m: PMarket) -> float | None:
    return Quote(m.best_bid, m.best_ask, None).spread


def band_side(mid: float, rules: Lopsided) -> str | None:
    """价格落在“很高”（hi）还是“很低”（lo）的区间；都不在时返回 None。"""
    if rules.hi[0] - _EPS <= mid <= rules.hi[1] + _EPS:
        return "hi"
    if rules.lo[0] - _EPS <= mid <= rules.lo[1] + _EPS:
        return "lo"
    return None


def lopsided_picks(markets: Sequence[PMarket], settings: MarketsSettings, now: datetime) -> list[PMarket]:
    """候选：每个事件只留价格在两个区间里、离 50% 最远的那个结果，按流动性从高到低排。

    这里只用 Gamma 的字段粗筛（还在交易、2 到 100 天内结算、价差、流动性、没有排除的词、
    结算进度没开始）；显示前还要用盘口和 /v2/resolutions 再核一遍（见 verify_lopsided）。
    """
    rules = settings.polymarket.lopsided
    best: dict[str, tuple[float, PMarket]] = {}
    for m in markets:
        if not _open_market(m, now, rules.min_days, rules.max_days):
            continue
        if m.liquidity < rules.min_liquidity or m.uma_status not in QUIET_UMA:
            continue
        if excluded(m, settings.polymarket):
            continue
        spread = _gamma_spread(m)
        if spread is None or spread < 0 or spread > rules.max_spread + _EPS:
            continue
        mid = (m.best_bid + m.best_ask) / 2
        if band_side(mid, rules) is None:
            continue
        group = m.event_id or m.condition_id
        distance = abs(mid - 0.5)
        held = best.get(group)
        tie = held is not None and abs(distance - held[0]) <= _EPS and m.liquidity > held[1].liquidity
        if held is None or distance > held[0] + _EPS or tie:
            best[group] = (distance, m)
    return sorted((m for _, m in best.values()), key=lambda m: (-m.liquidity, m.condition_id))


def verify_lopsided(book: Book, rules: Lopsided) -> float | None:
    """用 CLOB 盘口核实：价差不超过上限、中间价仍在区间里时返回中间价，否则 None。"""
    spread, mid = book.spread, book.mid
    if spread is None or mid is None or spread > rules.max_spread + _EPS:
        return None
    return mid if band_side(mid, rules) is not None else None


def lopsided_line(m: PMarket, book: Book, mid: float, zh: str | None, settings: MarketsSettings) -> Line:
    p = pct(mid)
    shown = zh or m.question
    text = f"· {question_text(zh, m.question)}：{band(p, settings.bands)}（{p}%）· {_day_label(m.end)}结算"
    if p < LONGSHOT_BELOW:
        text += f"；{LONGSHOT}"
    expires = _aware(m.end)
    row = {
        "type": "shown",
        "venue": "polymarket",
        "section": "lopsided",
        "key": f"{m.condition_id}:{m.token_ids[0]}",
        "event": m.event_id,
        "question": shown,
        "p": p,
        "side": band_side(mid, settings.polymarket.lopsided),
        "quote": _quote_row(Quote(book.best_bid, book.best_ask, m.last), mid),
        "liquidity": m.liquidity,
        "description_hash": m.description_hash,
        "expires": iso(expires),
    }
    return Line("lopsided", text, expires=expires, rows=(row,))


# ---- 过去一天的变化（Polymarket） ----


def mover_candidates(markets: Sequence[PMarket], settings: MarketsSettings, now: datetime) -> list[PMarket]:
    """用 Gamma 的一天涨跌粗筛，按 |变化| 从大到小取前 max_candidates 个（同一个市场只算一次）。"""
    rules = settings.polymarket.movers
    found: dict[str, PMarket] = {}
    for m in markets:
        if m.condition_id in found or not _open_market(m, now, rules.min_days, rules.max_days):
            continue
        change = m.one_day_change
        if change is None or not math.isfinite(change) or abs(change) < rules.prefilter_change - _EPS:
            continue
        spread = _gamma_spread(m)
        if spread is None or spread < 0 or spread > rules.max_spread + _EPS:
            continue
        if m.liquidity < rules.min_liquidity or excluded(m, settings.polymarket):
            continue
        found[m.condition_id] = m
    ranked = sorted(found.values(), key=lambda m: (-abs(m.one_day_change), m.condition_id))
    return ranked[: max(0, rules.max_candidates)]


@dataclass
class MoverCheck:
    """一个候选的核实结果；没取到的项为 None。"""

    market: PMarket
    mid: float | None = None  # 现在的盘口中间价
    spread: float | None = None  # 现在的盘口价差
    p24: float | None = None  # 24 小时前的价格（价格历史）
    change: float | None = None  # mid - p24
    persist: bool | None = None  # 最近几个整点都维持同一方向
    depth_up: float | None = None  # 往上推 5 个百分点要花的钱（美元）
    depth_down: float | None = None
    wallets: int | None = None  # 过去一天主动成交的钱包数
    top_share: float | None = None  # 最大的一个钱包占净成交的比例
    clean: bool | None = None  # 结算状态干净
    news: int | None = None  # 对应的新闻编号（1 起）
    zh: str | None = None  # 中文问题
    # 成交是否覆盖了整整 24 小时（翻页翻到了更早的记录或已经没有下一页）；没覆盖到时钱包的两项门槛算没通过
    trades_complete: bool | None = None
    errors: list[str] = field(default_factory=list)  # 没取到的步骤和异常类型，如 "book:FetchError"

    @property
    def key(self) -> str:
        m = self.market
        return f"{m.condition_id}:{m.token_ids[0]}" if m.token_ids else m.condition_id


def price_change(
    history: Sequence[tuple[int, float]], mid: float | None, now_ts: int, persist_points: int, bucket: int = HOUR
) -> tuple[float | None, float | None, bool | None]:
    """按价格历史重算一天的变化：(24 小时前的价格, 现在的中间价 - 它, 最近几个整点是否维持同一方向)。

    价格历史每个整点一个点（时间是桶的开始），最后还有一个不在整点上的最新观测，那个点不算。
    """
    if mid is None:
        return None, None, None
    aligned = sorted((int(ts), float(p)) for ts, p in history if int(ts) % bucket == 0 and math.isfinite(p))
    target = now_ts - DAY
    before = [p for ts, p in aligned if ts <= target]
    if not before:
        return None, None, False
    p24 = before[-1]
    change = mid - p24
    recent = [p for ts, p in aligned if ts > target][-persist_points:] if persist_points > 0 else []
    if abs(change) <= _EPS or len(recent) < persist_points:
        return p24, change, False
    sign = 1 if change > 0 else -1
    return p24, change, all((p - p24) * sign > _EPS for p in recent)


def mover_gates(check: MoverCheck, settings: MarketsSettings) -> dict[str, bool]:
    """每项门槛是否通过；没取到的值一律算没通过。"""
    rules = settings.polymarket.movers
    change = abs(check.change) if check.change is not None else None
    depth = (
        min(check.depth_up, check.depth_down) if check.depth_up is not None and check.depth_down is not None else None
    )
    # 只取到最近几个小时的成交时，钱包数和集中度说明不了一整天的情况
    whole_day = check.trades_complete is not False
    return {
        "change": change is not None and change >= rules.min_change - _EPS,
        "spread": change is not None and check.spread is not None
        and change >= rules.min_spread_multiple * check.spread - _EPS,
        "persist": check.persist is True,
        "depth": depth is not None and depth >= rules.min_depth_usd - _EPS,
        "wallets": whole_day and check.wallets is not None and check.wallets >= rules.min_wallets,
        "top_share": whole_day and check.top_share is not None and check.top_share <= rules.max_top_share + _EPS,
        "clean": check.clean is True,
        "news": check.news is not None,
    }


def pick_mover(checks: Sequence[MoverCheck], settings: MarketsSettings) -> MoverCheck | None:
    """全部门槛都通过的候选里变化最大的一个；没有时返回 None。"""
    passing = [c for c in checks if all(mover_gates(c, settings).values())]
    return max(passing, key=lambda c: abs(c.change), default=None)


def change_points(change: float) -> str:
    """一天的变化换成带符号的整数百分点，四舍五入：+7 / −6。"""
    points = int((Decimal(str(change)) * 100).quantize(Decimal("1"), ROUND_HALF_UP))
    if points > 0:
        return f"+{points}"
    return f"{MINUS}{abs(points)}" if points < 0 else "0"


def news_sub(news: int, edition: str) -> str:
    return f"相关新闻：{'今晚' if edition == 'evening' else '今天'}第 {news} 条"


def mover_line(check: MoverCheck, edition: str, settings: MarketsSettings, preview: bool = False) -> Line | None:
    """一行价格变化。预览里每个候选都显示，后台观察时标【后台观察】，小字写明哪几项没通过。"""
    if check.mid is None or check.change is None:
        return None
    m = check.market
    p = pct(check.mid)
    prefix = SHADOW if preview and settings.polymarket.movers.mode == "shadow" else ""
    text = f"· {prefix}{question_text(check.zh, m.question)}：{p}%（一天 {change_points(check.change)} 个百分点）"
    parts = []
    if preview:
        failed = [GATE_NAMES[g] for g, ok in mover_gates(check, settings).items() if not ok]
        parts.append("没通过：" + "、".join(failed) if failed else "各项核实都通过了")
    if check.news is not None:
        parts.append(news_sub(check.news, edition))
    expires = _aware(m.end) if m.end is not None else None
    row = {
        "type": "shown",
        "venue": "polymarket",
        "section": "mover",
        "key": check.key,
        "event": m.event_id,
        "question": check.zh or m.question,
        "p": p,
        "change": check.change,
        "quote": {"bid": None, "ask": None, "last": m.last, "mid": check.mid, "spread": check.spread},
        "news": check.news,
        "description_hash": m.description_hash,
        "expires": iso(expires),
    }
    return Line("mover", text, sub="；".join(parts) or None, expires=expires, rows=(row,))


def candidate_row(check: MoverCheck, settings: MarketsSettings, shown: bool) -> dict:
    """每个候选都记一条，门槛的数值和是否通过都在里面，用来在后台观察阈值是否合适。"""
    m = check.market
    return {
        "type": "candidate",
        "venue": "polymarket",
        "key": check.key,
        "event": m.event_id,
        "question": m.question,
        "zh": check.zh,
        "movers_mode": settings.polymarket.movers.mode,
        "prefilter": {"change": m.one_day_change, "bid": m.best_bid, "ask": m.best_ask, "liquidity": m.liquidity},
        "values": {
            "mid": check.mid,
            "spread": check.spread,
            "p24": check.p24,
            "change": check.change,
            "persist": check.persist,
            "depth_up": check.depth_up,
            "depth_down": check.depth_down,
            "wallets": check.wallets,
            "top_share": check.top_share,
            "trades_complete": check.trades_complete,
            "clean": check.clean,
            "news": check.news,
        },
        "gates": mover_gates(check, settings),
        "errors": list(check.errors),
        "shown": shown,
        "description_hash": m.description_hash,
        "expires": iso(m.end),
    }


# ---- 波动率（Cboe） ----


def _stale(close: Close, now: datetime, max_age_days: int) -> bool:
    return (_aware(now).astimezone(ET).date() - close.day).days > max_age_days


def _one_decimal(value: float) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.1"), ROUND_HALF_UP))


def vix_lines(vix: Close | None, vix9d: Close | None, now: datetime, settings: MarketsSettings) -> list[Line]:
    """VIX 一行；VIX9D 和 VIX 是同一天的收盘、相差至少 1 时再加一行。超过 max_age_days 天的不用。"""
    max_age = settings.vix.max_age_days
    if vix is None or not math.isfinite(vix.value) or vix.value <= 0 or _stale(vix, now, max_age):
        return []
    daily = vix.value / math.sqrt(252)
    lines = [
        Line(
            "vix",
            f"· VIX {vix.value:.2f}（{vix.day.month}月{vix.day.day}日收盘，长期平均约 20）："
            f"标普500 一天的涨跌约有三分之二的机会在 ±{_one_decimal(daily)}% 以内",
        )
    ]
    if vix9d is None or vix9d.day != vix.day or not math.isfinite(vix9d.value):
        return lines
    diff = vix9d.value - vix.value
    if diff >= 1 - _EPS:
        lines.append(Line("vix", f"· 未来 9 天的预期波动（{vix9d.value:.2f}）高于未来 30 天，市场觉得近期风险更大"))
    elif diff <= -1 + _EPS:
        lines.append(Line("vix", f"· 未来 9 天的预期波动（{vix9d.value:.2f}）低于未来 30 天，市场觉得近期相对平静"))
    return lines
