"""Kalshi 公开行情接口（不需要账号、只发 GET）：事件、合约、历史 K 线，并解析成 KEvent / KMarket / Candle。

解析规则（见 contracts/kalshi-live.md 和 kalshi-history.md）：
- 价格是 "0.5600" 这样的字符串（*_dollars），张数是 "1234.00"（*_fp），一律用 Decimal 解析再转成 float。
- 买价 "0.0000" 表示没有买单，卖价 "1.0000" 表示没有卖单，都当作 None，不当作价格。
- 实时 K 线的键带 _dollars / _fp 后缀，历史 K 线不带，两种都认。
- 收盘之后的那根 K 线带的是收盘后的空盘口（买 0 卖 1），取价只用结束时间不晚于收盘时间的 K 线。
- 接口返回的合约不按行权价排序，这里按 floor_strike 排（“低于”类按 cap_strike）。

安全约定：日志和异常里只有主机名、HTTP 状态码、异常类型和计数，没有合约代码、价格和完整地址。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlsplit

from digest.markets.net import FetchError, Fetcher
from digest.markets.types import Quote

log = logging.getLogger("digest.markets")

# 主地址连不上或服务端出错时换下一个地址；4xx 是请求本身的问题，换地址也没用
FALLBACK_KINDS = ("connect", "5xx")
# 连不上或超时（Fetcher 已经重试过一次）的地址这次运行先不用；每个地址最近一次都这样时，Kalshi 算是连不上，
# 后面的请求不再发，免得每类合约都把两个地址各等一遍、吃掉整个预算。5xx 是服务端给了回答，可能只是某条路径出错，不算
DOWN_KINDS = ("connect", "timeout")
# 合约归档到历史库以后，实时接口查它的 K 线会返回 4xx（多半是 404），反过来也一样，这时换一个库再查一次
WRONG_TIER_STATUS = (400, 404)
# 单次 K 线请求最多这么多个周期（实测 4,900 可以、5,000 左右被拒，留足余量）
MAX_PERIODS = 4000
PERIODS = (1, 60, 1440)  # K 线周期只能是 1 分钟、1 小时、1 天
EVENTS_PAGE = 200  # /events 每页上限
MARKETS_PAGE = 1000  # /markets 和 /historical/markets 每页上限
MARKETS_MAX_PAGES = 3
# CPI、核心 CPI、失业率的公布值只有一位小数，相邻两档行权价正好差这么多时才能从各档结果反推公布值
RESULT_GRID = 0.1
_GRID_EPS = 1e-6

_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}
_MONTH_PERIOD = re.compile(r"^(\d{2})([A-Z]{3})")
_YEAR_PERIOD = re.compile(r"^(\d{2})$")
_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}:?\d{2})?$")
_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_FED_SUFFIX = re.compile(r"-([CH])(\d+)$")

# 议息结果：(排序用的方向和幅度, 中文名)。custom_strike 形如 {"Cut": ">25"}、{"Hike": "0"}，值是字符串
_FED = {
    ("cut", ">25"): (-2, "降息超过 25 个基点"),
    ("cut", "25"): (-1, "降息 25 个基点"),
    ("cut", "0"): (0, "维持不变"),
    ("hike", "0"): (0, "维持不变"),
    ("hike", "25"): (1, "加息 25 个基点"),
    ("hike", ">25"): (2, "加息超过 25 个基点"),
}
# 合约代码的后缀：C26 = 降息超过 25，C25 = 降息 25，H0 = 不变，H25 = 加息 25，H26 = 加息超过 25
_FED_BY_SUFFIX = {
    ("C", 26): _FED[("cut", ">25")],
    ("C", 25): _FED[("cut", "25")],
    ("C", 0): _FED[("cut", "0")],
    ("H", 0): _FED[("hike", "0")],
    ("H", 25): _FED[("hike", "25")],
    ("H", 26): _FED[("hike", ">25")],
}


@dataclass(frozen=True)
class KMarket:
    """一个二元合约。价格都是 0 到 1 的概率；没有的值为 None，没有的文本为空字符串。"""

    ticker: str
    event_ticker: str
    status: str  # initialized / active / closed / determined / finalized 等
    close_time: datetime | None  # 停止交易的时间（UTC）
    strike_type: str | None  # greater / less / between / custom 等；单个是非题没有
    floor: float | None  # floor_strike：greater 的门槛、between 的下限
    cap: float | None  # cap_strike：less 的门槛、between 的上限
    custom: dict | None  # custom_strike，议息合约才有，如 {"Hike": "25"}
    yes_sub_title: str
    quote: Quote
    open_interest: float  # 持仓张数
    result: str  # "yes" / "no" / "scalar" / ""（还没出结果）
    expiration_value: str  # 结算依据的原始文本，各系列格式不一，见 settled_value
    settlement_value: float | None  # YES 一方的结算价，1.0 或 0.0；没结算时为 None


@dataclass(frozen=True)
class KEvent:
    """一个事件（一次数据公布或一次议息），下面的合约已排好序。"""

    event_ticker: str
    series_ticker: str
    title: str
    mutually_exclusive: bool  # 各合约互斥、恰好一个结果为 yes（议息、标普年底区间）
    strike_date: datetime | None
    markets: tuple[KMarket, ...]

    @property
    def open_interest(self) -> float:
        return sum(m.open_interest for m in self.markets)


@dataclass(frozen=True)
class Candle:
    """一根 K 线收盘时的报价：买价、卖价、本周期最后成交价、本周期之前的最后成交价。"""

    end_ts: int  # 周期结束时间（Unix 秒，含）
    bid: float | None
    ask: float | None
    last: float | None
    previous: float | None


def parse_market(d: dict) -> KMarket:
    """解析接口返回的一个合约。缺 ticker 时抛 ValueError，其余字段缺了或写错都用空值。"""
    if not isinstance(d, Mapping):
        raise ValueError("合约数据不是对象")
    ticker = _text(d.get("ticker"))
    if not ticker:
        raise ValueError("合约缺少 ticker")
    custom = d.get("custom_strike")
    return KMarket(
        ticker=ticker,
        event_ticker=_text(d.get("event_ticker")),
        status=_text(d.get("status")).lower(),
        close_time=_time(d.get("close_time")),
        strike_type=_text(d.get("strike_type")).lower() or None,
        floor=_number(d.get("floor_strike")),
        cap=_number(d.get("cap_strike")),
        custom=dict(custom) if isinstance(custom, Mapping) else None,
        yes_sub_title=_text(d.get("yes_sub_title")),
        quote=Quote(
            bid=_bid(d.get("yes_bid_dollars")),
            ask=_ask(d.get("yes_ask_dollars")),
            last=_trade(d.get("last_price_dollars")),
        ),
        open_interest=_count(d.get("open_interest_fp")),
        # 没出结果的合约有时整个没有 result 键
        result=_text(d.get("result")).lower(),
        expiration_value=_text(d.get("expiration_value")),
        settlement_value=_price(d.get("settlement_value_dollars")),
    )


def parse_event(d: dict) -> KEvent:
    """解析一个事件和嵌在里面的合约（with_nested_markets=true 时才有）。缺 event_ticker 时抛 ValueError。"""
    if not isinstance(d, Mapping):
        raise ValueError("事件数据不是对象")
    event_ticker = _text(d.get("event_ticker"))
    if not event_ticker:
        raise ValueError("事件缺少 event_ticker")
    raw = d.get("markets")
    return KEvent(
        event_ticker=event_ticker,
        # 旧合约的代码前缀和系列不一样（如 CPI-21JUN 属于 KXCPI），系列只看这个字段，不从代码推
        series_ticker=_text(d.get("series_ticker")),
        title=_text(d.get("title")),
        mutually_exclusive=d.get("mutually_exclusive") is True,
        strike_date=_time(d.get("strike_date")),
        markets=tuple(_sorted(_parse_markets(raw if isinstance(raw, list) else []))),
    )


def parse_candles(items: list[dict]) -> list[Candle]:
    """解析 K 线，实时（close_dollars、volume_fp）和历史（close、volume）两种键名都认。

    按结束时间排序；同一时间出现两次时用后面那根；没有合法结束时间的跳过。
    """
    found: dict[int, Candle] = {}
    for c in items or ():
        if not isinstance(c, Mapping):
            continue
        end = _int(c.get("end_period_ts"))
        if end is None:
            continue
        price = c.get("price")
        found[end] = Candle(
            end_ts=end,
            bid=_bid(_field(c.get("yes_bid"), "close")),
            ask=_ask(_field(c.get("yes_ask"), "close")),
            # 本周期没有成交时 price 下面的键整个不出现（实时）或为 null（历史）
            last=_trade(_field(price, "close")),
            previous=_trade(_field(price, "previous")),
        )
    return [found[k] for k in sorted(found)]


def period_of(event_ticker: str) -> tuple[int, int] | None:
    """事件代码里的参考期 (年, 月)：KXCPI-26OCT -> (2026, 10)，KXINXY-26DEC31H1600 -> (2026, 12)，
    只有年份的 KXRECSSNBER-27 -> (2027, 12)。认不出时返回 None。合约代码（多一段行权价）也可以。"""
    parts = (event_ticker or "").strip().upper().split("-")
    if len(parts) < 2:
        return None
    segment = parts[1]
    match = _MONTH_PERIOD.match(segment)
    if match:
        month = _MONTHS.get(match.group(2))
        return (2000 + int(match.group(1)), month) if month else None
    match = _YEAR_PERIOD.match(segment)
    if match:
        return 2000 + int(match.group(1)), 12
    return None


def quote_at(candles: list[Candle], t_ts: int, close_ts: int) -> tuple[Quote, int] | None:
    """某一时刻能看到的报价：结束时间不晚于 min(t_ts, close_ts) 的最后一根 K 线。

    返回 (报价, 这根 K 线距离截止时刻的秒数)；成交价用本周期的，没有时用之前的最后成交价。
    收盘之后的 K 线带的是空盘口，绝不使用；正好在收盘时刻结束的那根是有效的。
    """
    cutoff = min(int(t_ts), int(close_ts))
    best: Candle | None = None
    for c in candles:
        if c.end_ts <= cutoff and (best is None or c.end_ts >= best.end_ts):
            best = c
    if best is None:
        return None
    last = best.last if best.last is not None else best.previous
    return Quote(best.bid, best.ask, last), cutoff - best.end_ts


def fed_outcome(m: KMarket) -> str | None:
    """议息合约对应的结果（中文）。先看 custom_strike，没有或认不出时看合约代码的后缀；都认不出返回 None。"""
    found = _fed(m)
    return found[1] if found else None


def settled_value(series_kind: str, markets: list[KMarket]) -> float | str | None:
    """一个已结算事件的结果；说不清时返回 None（调用方跳过这个事件并计数）。

    - ladder（一串“高于 k”）：expiration_value 能解析成数字时用它（去掉 %、千分位逗号，".9" 补成 "0.9"），
      但要和各档合约的结果对得上；是 "Above 0.2%" 这样的标签或为空时，从各档结果反推：最高的 yes 档和最低的
      no 档正好相差一档（0.1）时，公布值就是那个 no 档（合约是严格大于）。差得更远、全 yes、全 no
      或结果不单调都说不清，返回 None。
    - fed：结果为 yes 的那个合约的 fed_outcome；不是恰好一个 yes 时返回 None。
    - buckets（互斥区间）：能解析的数字 expiration_value，且和各区间的结果对得上。
    - binary（单个是非题）：yes 为 1.0，no 为 0.0。
    """
    markets = list(markets)
    if not markets:
        return None
    if series_kind == "fed":
        winners = [m for m in markets if m.result == "yes"]
        return fed_outcome(winners[0]) if len(winners) == 1 else None
    if series_kind == "binary":
        results = {m.result for m in markets}
        return 1.0 if results == {"yes"} else 0.0 if results == {"no"} else None
    if series_kind not in ("ladder", "buckets"):
        return None
    values = {v for v in (_expiration_number(m.expiration_value) for m in markets) if v is not None}
    if len(values) > 1:
        return None  # 同一事件的各合约写的结算值不一样
    if values:
        value = values.pop()
        return value if all(_agrees(value, m) for m in markets) else None
    return _value_from_results(markets) if series_kind == "ladder" else None


class Kalshi:
    """Kalshi 公开接口的客户端。所有请求都经过 Fetcher（受预算约束），预算用完时 BudgetExceeded 原样抛出。

    主地址连不上或返回 5xx 时，同一个请求改发到下一个地址（也计入预算）；备用地址能用后，
    本次运行后面的请求都先走它，不再在连不上的地址上浪费时间。连不上或超时的地址记进 dead，之后先不用；
    所有地址都在 dead 里时 down 为真，请求不再发出，直接抛 FetchError（kind 为 "down"）。
    任何一次请求成功都会清空 dead（网络恢复了，备用的地址也重新可用）。
    """

    def __init__(self, fetcher: Fetcher, hosts: Sequence[str]) -> None:
        bases = tuple(str(h).strip().rstrip("/") for h in hosts if str(h).strip())
        if not bases:
            raise ValueError("没有可用的 Kalshi 地址")
        self.fetcher = fetcher
        self.hosts = bases
        self._order = bases  # 当前的尝试顺序
        self._archived: set[str] = set()  # 已知只在历史库里的合约，查 K 线时直接走历史接口
        self.dead: set[str] = set()  # 最近一次连不上或超时的地址（同一次运行里的几个客户端可以共用这份记录）

    @property
    def down(self) -> bool:
        """每个地址最近一次都连不上或超时：这次运行不再请求 Kalshi。"""
        return all(base in self.dead for base in self.hosts)

    def events(self, series: str, status: str = "open", nested: bool = True, max_pages: int = 3) -> list[KEvent]:
        """某个系列的事件（接口按时间从新到旧），nested 时带上合约。status 为空表示不限状态。"""
        params: dict[str, Any] = {
            "series_ticker": series,
            "limit": EVENTS_PAGE,
            "with_nested_markets": "true" if nested else "false",
        }
        if status:
            params["status"] = status
        events: list[KEvent] = []
        seen: set[str] = set()
        for d in self._pages("/events", params, "events", max_pages):
            try:
                event = parse_event(d)
            except ValueError:
                continue
            if event.event_ticker not in seen:
                seen.add(event.event_ticker)
                events.append(event)
        return events

    def markets(self, event_ticker: str) -> list[KMarket]:
        """一个事件的全部合约（已排序）：先查实时库，查不到（空列表或 404）再查历史库。"""
        params = {"event_ticker": event_ticker, "limit": MARKETS_PAGE}
        found = self._market_list("/markets", params, MARKETS_MAX_PAGES)
        if not found:
            found = self._market_list("/historical/markets", params, MARKETS_MAX_PAGES)
            self._archived.update(m.ticker for m in found)
        return _sorted(found)

    def market(self, ticker: str) -> KMarket | None:
        """一个合约：先查实时库，再查历史库，都没有时返回 None。

        用列表形式 ?tickers= 查：归档了的合约在实时库里是空列表，不用猜 4xx 的含义。
        """
        for path, archived in (("/markets", False), ("/historical/markets", True)):
            for m in self._market_list(path, {"tickers": ticker}, 1):
                if m.ticker == ticker:
                    if archived:
                        self._archived.add(ticker)
                    return m
        return None

    def cutoff(self) -> datetime | None:
        """历史库的分界：在这之前结算的合约（和它们的 K 线）只能从 /historical/* 取。"""
        data = self._get("/historical/cutoff", None)
        return _time(data.get("market_settled_ts")) if isinstance(data, Mapping) else None

    def candles(
        self,
        series: str,
        ticker: str,
        start_ts: int,
        end_ts: int,
        period: int = 60,
        historical: bool = False,
    ) -> list[Candle]:
        """一个合约在 [start_ts, end_ts] 内结束的 K 线（period 为分钟）。

        范围太长时拆成几次请求，每次不超过 MAX_PERIODS 个周期。historical 为真时查历史接口；
        markets() / market() 发现只在历史库里的合约也自动走历史接口。第一次请求返回 400/404 时
        换另一个库再试一次（合约可能刚好在两次查询之间归档）。
        """
        if period not in PERIODS:
            raise ValueError("K 线周期只能是 1、60 或 1440 分钟")
        archived = historical or ticker in self._archived
        step = period * 60
        start, end = int(start_ts), int(end_ts)
        found: dict[int, Candle] = {}
        first = True
        while start <= end:
            # 两端都含：从 start 起最多 MAX_PERIODS 个周期边界
            stop = min(end, start + (MAX_PERIODS - 1) * step)
            params = {"start_ts": start, "end_ts": stop, "period_interval": period}
            try:
                data = self._get(_candle_path(series, ticker, archived), params)
            except FetchError as exc:
                if not first or exc.status not in WRONG_TIER_STATUS:
                    raise
                archived = not archived
                data = self._get(_candle_path(series, ticker, archived), params)
                if archived:
                    self._archived.add(ticker)
                else:
                    self._archived.discard(ticker)
            first = False
            items = data.get("candlesticks") if isinstance(data, Mapping) else None
            for c in parse_candles(items if isinstance(items, list) else []):
                found[c.end_ts] = c
            start = stop + 1
        return [found[k] for k in sorted(found)]

    def _market_list(self, path: str, params: Mapping[str, Any], max_pages: int) -> list[KMarket]:
        """分页取合约列表并去重；404 当作没有。"""
        try:
            items = self._pages(path, params, "markets", max_pages)
        except FetchError as exc:
            if exc.status == 404:
                return []
            raise
        markets: list[KMarket] = []
        seen: set[str] = set()
        for m in _parse_markets(items):
            if m.ticker not in seen:
                seen.add(m.ticker)
                markets.append(m)
        return markets

    def _pages(self, path: str, params: Mapping[str, Any], key: str, max_pages: int) -> list[dict]:
        """按 cursor 翻页，最后一页的 cursor 是空字符串；最多 max_pages 页。"""
        items: list[dict] = []
        cursor = ""
        for _ in range(max(1, int(max_pages))):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            data = self._get(path, query)
            if not isinstance(data, Mapping):
                break
            page = data.get(key)
            if isinstance(page, list):
                items.extend(x for x in page if isinstance(x, Mapping))
            nxt = data.get("cursor")
            if not isinstance(nxt, str) or not nxt or nxt == cursor:
                break
            cursor = nxt
        else:
            log.info("市场数据：Kalshi 列表超过 %d 页，只取了前面的", max(1, int(max_pages)))
        return items

    def _get(self, path: str, params: Mapping[str, Any] | None) -> Any:
        order = tuple(base for base in self._order if base not in self.dead)
        if not order:
            # 不发请求；Fetcher.hosts 里留着最近一次真正的结果（如 ConnectTimeout），Summary 照样写得出原因
            raise FetchError(_hostname(self._order[0]), None, "down", "连不上，没有再试")
        for i, base in enumerate(order):
            try:
                data = self.fetcher.get_json(base + path, params)
            except FetchError as exc:
                if exc.kind in DOWN_KINDS:
                    self.dead.add(base)
                if i + 1 < len(order) and exc.kind in FALLBACK_KINDS:
                    log.info("市场数据：%s 不可用（%s），改用 %s", exc.host, exc.outcome, _hostname(order[i + 1]))
                    continue
                if self.down:
                    log.warning("市场数据：Kalshi 的地址都连不上（%s），这次不再请求", exc.outcome)
                raise
            self.dead.clear()
            if i > 0:
                self._order = (base, *(h for h in self._order if h != base))
            return data
        raise AssertionError("不会到这里")


def _candle_path(series: str, ticker: str, archived: bool) -> str:
    if archived:
        return f"/historical/markets/{_segment(ticker)}/candlesticks"
    return f"/series/{_segment(series)}/markets/{_segment(ticker)}/candlesticks"


def _segment(value: str) -> str:
    return quote(str(value), safe="")


def _hostname(base: str) -> str:
    try:
        return urlsplit(base).hostname or "?"
    except ValueError:
        return "?"


def _parse_markets(items: Iterable[Any]) -> list[KMarket]:
    markets = []
    for d in items:
        try:
            markets.append(parse_market(d))
        except ValueError:
            continue
    return markets


def _sorted(markets: Iterable[KMarket]) -> list[KMarket]:
    return sorted(markets, key=_strike_key)


def _strike_key(m: KMarket) -> tuple[bool, float, int, str]:
    """排序键：阶梯和区间按 floor_strike，“低于”类按 cap_strike（同一数值时排在区间前面），
    议息按降息到加息，没有行权价的排最后。"""
    kind = m.strike_type or ""
    rank = 1
    if kind in ("less", "less_or_equal"):
        value, rank = m.cap, 0
    elif m.floor is not None:
        value = m.floor
    elif m.cap is not None:
        value = m.cap
    else:
        fed = _fed(m)
        value = float(fed[0]) if fed else None
    return (value is None, value if value is not None else 0.0, rank, m.ticker)


def _fed(m: KMarket) -> tuple[int, str] | None:
    if m.custom:
        for key, raw in m.custom.items():
            value = str(raw).strip().lower().replace(" ", "").removesuffix("bps").removesuffix("bp")
            found = _FED.get((str(key).strip().lower(), value))
            if found:
                return found
    match = _FED_SUFFIX.search(m.ticker.upper())
    if match:
        return _FED_BY_SUFFIX.get((match.group(1), int(match.group(2))))
    return None


def _expiration_number(text: str) -> float | None:
    """把 "0.3"、"4.2%"、"147,000"、".9%" 这类结算值解析成数字；标签、空串返回 None。"""
    s = (text or "").strip().replace(",", "").replace("%", "").strip()
    if s.startswith("."):
        s = "0" + s
    elif s[:2] in ("-.", "+."):
        s = s[0] + "0" + s[1:]
    if not _NUMBER.match(s):
        return None
    return float(Decimal(s))


def _agrees(value: float, m: KMarket) -> bool:
    """数字结算值和这个合约的结果对得上（没出 yes/no 或没有行权价的合约不检查）。"""
    if m.result not in ("yes", "no"):
        return True
    kind = m.strike_type or ""
    if kind == "greater" and m.floor is not None:
        expected = value > m.floor
    elif kind == "greater_or_equal" and m.floor is not None:
        expected = value >= m.floor
    elif kind == "less" and m.cap is not None:
        expected = value < m.cap
    elif kind == "less_or_equal" and m.cap is not None:
        expected = value <= m.cap
    elif kind == "between" and m.floor is not None and m.cap is not None:
        expected = m.floor <= value <= m.cap
    else:
        return True
    return expected == (m.result == "yes")


def _value_from_results(markets: list[KMarket]) -> float | None:
    """从“高于 k”各档的 yes/no 反推一位小数的公布值；推不出唯一值时返回 None。"""
    kinds = {m.strike_type for m in markets}
    if len(kinds) != 1 or not kinds <= {"greater", "greater_or_equal"}:
        return None
    if any(m.floor is None or m.result not in ("yes", "no") for m in markets):
        return None
    yes = [m.floor for m in markets if m.result == "yes"]
    no = [m.floor for m in markets if m.result == "no"]
    if not yes or not no:
        return None  # 全 yes 或全 no：公布值在阶梯外面，不知道具体是多少
    high_yes, low_no = max(yes), min(no)
    if high_yes >= low_no:
        return None  # 结果不单调
    if abs(low_no - high_yes - RESULT_GRID) > _GRID_EPS or not (_on_grid(high_yes) and _on_grid(low_no)):
        return None  # 两档之间不止一个可能的值
    # 严格大于：X > 高的 yes 档且 X <= 低的 no 档，一位小数上只能是低的 no 档；大于等于时是高的 yes 档
    return low_no if kinds == {"greater"} else high_yes


def _on_grid(value: float) -> bool:
    return abs(value / RESULT_GRID - round(value / RESULT_GRID)) < _GRID_EPS


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _number(value: Any) -> float | None:
    """行权价：JSON 数字（整数不带小数点，如 50000、-25000）。"""
    number = _decimal(value)
    return float(number) if number is not None else None


def _price(value: Any) -> float | None:
    """0 到 1 之间的价格字符串；超出范围的当作没有。"""
    number = _decimal(value)
    if number is None or number < 0 or number > 1:
        return None
    return float(number)


def _bid(value: Any) -> float | None:
    """买价 0 表示没有买单。"""
    price = _price(value)
    return price if price is not None and price > 0 else None


def _ask(value: Any) -> float | None:
    """卖价 1 表示没有卖单。"""
    price = _price(value)
    return price if price is not None and price < 1 else None


def _trade(value: Any) -> float | None:
    """成交价：0 和 1 不是能成交的价格（最小一档是 0.01 到 0.99），出现时表示从没成交过。"""
    price = _price(value)
    return price if price is not None and 0 < price < 1 else None


def _count(value: Any) -> float:
    number = _decimal(value)
    return float(number) if number is not None and number > 0 else 0.0


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _field(obj: Any, key: str) -> Any:
    """K 线里的一个价格：实时接口叫 close_dollars，历史接口叫 close。"""
    if not isinstance(obj, Mapping):
        return None
    value = obj.get(f"{key}_dollars")
    return value if value is not None else obj.get(key)


def _time(value: Any) -> datetime | None:
    """RFC 3339 时间（小数秒位数不定，0 到 9 位都有），转成 UTC。Go 的零值 0001-01-01 表示没有。"""
    if not isinstance(value, str):
        return None
    match = _TIME.match(value.strip())
    if not match:
        return None
    day, clock, fraction, zone = match.groups()
    text = f"{day}T{clock}"
    if fraction:
        text += "." + fraction[:6].ljust(6, "0")
    if not zone or zone in ("Z", "z"):
        zone = "+00:00"
    elif ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"
    try:
        parsed = datetime.fromisoformat(text + zone)
    except ValueError:
        return None
    if parsed.year <= 1:
        return None
    return parsed.astimezone(timezone.utc)
