"""Polymarket 公开接口（不需要账号、只发 GET）：Gamma 事件列表、CLOB 盘口、Data API 的价格历史、成交、
结算状态和结算价，并解析成 PMarket / Book / Trade / Resolution。

解析规则（见 contracts/polymarket.md）：
- Gamma 的 outcomes、outcomePrices、clobTokenIds、umaResolutionStatuses 是“装着 JSON 数组的字符串”，要再
  json.loads 一次；v2 市场的 positionIds 是真正的数组。token id 是七十多位的十进制数，一律当字符串。
- Gamma 的数值键等于 0 时整个省略（bestBid、oneDayPriceChange 等），缺了当作没有，不算出错。
- CLOB 盘口的价格和数量都是字符串；文档写的排序和实际相反，这里自己排：最高买价、最低卖价在最前面。
- Data API v2 的数字都是 JSON 数字，外面包一层 {data, pagination}，按 next_cursor 翻页；绝不发 offset。
- 日期写法不统一（Z、+00、空格分隔、多一段 :00 的错误时区），一律宽松解析成 UTC。

安全约定：日志和异常里只有主机名、HTTP 状态码、异常类型和计数，没有问题文本、condition id、token id、价格和
完整地址。成交记录里的钱包地址在解析时就换成本进程内的匿名键，只用来数钱包个数；个人资料字段（name、bio 等）
一律不读。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from digest.markets.net import FetchError, Fetcher
from digest.markets.settings import PolymarketSettings

log = logging.getLogger("digest.markets")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

# 一页的事件数（接口上限 100）。大事件下面有几十个市场，页太大响应会很大
EVENTS_PAGE = 50
# 按过去一天的成交额从高到低取，翻不完时先拿到最活跃的事件；接口不接受时退回最简单的查询
EVENTS_ORDER = "volume24hr"
# 不发 version 时接口只返回 v1（CTF）市场，两种协议都要
VERSIONS = ("v1", "v2")
TRADES_PAGE = 1000  # /v2/trades 第一页的条数（接口上限 1000，翻页后由 cursor 决定）
HISTORY_MAX_PAGES = 5  # 价格历史一页最多 10,000 个点，正常一页就够，这里只防止翻个没完
CLEAN_STATUS = ("initialized", "posed", "active")  # 还没有人提出结算结果的状态
UNLABELED = 999  # 成交记录里 outcome_index 为 999 表示没标结果
_EPS = 1e-9

# 要排除的几类标签：标签编号和 slug 都认（slug 不分大小写）
_TAG_BY_ID = {
    "1": "sports",
    "64": "esports",
    "100343": "mention-markets",
    "102127": "up-or-down",
    "102892": "up-or-down",  # 5M
    "102467": "up-or-down",  # 15m
    "102175": "up-or-down",  # 1H
}
_SHORT_TAGS = ("5m", "15m", "1h")
_SHORT_RECURRENCE = ("5m", "15m", "hourly", "4h")
_UPDOWN_SLUG = re.compile(r"^[a-z]+-updown-(5m|15m|1h|4h)-\d+$")
_UPDOWN_SERIES = re.compile(r"-up-or-down-(5m|15m|hourly|4h|daily)$")
_MENTION_SLUG = re.compile(r"^what-will-.*-say-")

_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2})"
    r"(?:[Tt ](\d{2}:\d{2}(?::\d{2})?)(?:\.(\d+))?)?"
    r"\s*([Zz]|[+-]\d{2}(?::?\d{2})?(?::\d{2})?)?$"
)
_ZONE = re.compile(r"^([+-])(\d{2}):?(\d{2})?")
_WORD = re.compile(r"[^\W_]+")

# 钱包地址只换成这个进程内的匿名键：同一个钱包得到同一个键，换一次运行就对不上，也反推不出地址
_WALLET_KEY = os.urandom(16)


@dataclass(frozen=True)
class PMarket:
    """Gamma 事件下面的一个市场（一个是非问题）。价格是 YES（第 0 个结果）一方的，0 到 1；没有的值为 None。"""

    id: str
    condition_id: str  # 0x 加 64 位十六进制
    question: str
    outcomes: tuple[str, ...]  # 二元市场是 ("Yes", "No")
    token_ids: tuple[str, ...]  # 和 outcomes 一一对应；对不上或还没有时为空
    best_bid: float | None
    best_ask: float | None
    last: float | None
    one_day_change: float | None  # 平台给的一天涨跌（价格单位，不是百分比），只能用来粗筛
    liquidity: float  # 美元，含义没有公开定义，只能用来粗筛
    volume_24h: float
    end: datetime | None  # 预定的结算时间（UTC）
    closed: bool
    active: bool  # 还在交易：active 为真，而且没有停止接单（acceptingOrders 为假）、没有归档
    uma_status: str  # 结算进度，小写：v1 取 umaResolutionStatus（缺时取历史记录的最后一个），v2 取 resolutionStatus；没有时为空
    group_title: str  # 多选事件里这个选项的名字
    event_id: str
    event_title: str
    tags: tuple[str, ...]  # 事件标签的 slug（小写），加上从结构推出的 sports / esports / mention-markets / up-or-down
    description_hash: str  # sha256(description + resolutionSource)，用来发现规则被改过


@dataclass(frozen=True)
class Book:
    """CLOB 盘口：(价格, 数量) 列表。构造时自动排好序：买单从高到低，卖单从低到高。"""

    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        bids = sorted(((float(p), float(s)) for p, s in self.bids), key=lambda level: -level[0])
        asks = sorted(((float(p), float(s)) for p, s in self.asks), key=lambda level: level[0])
        object.__setattr__(self, "bids", tuple(bids))
        object.__setattr__(self, "asks", tuple(asks))

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def spread(self) -> float | None:
        """买卖价差；缺一边或买价高于卖价（盘口错乱）时为 None。"""
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None or bid > ask + _EPS:
            return None
        return round(ask - bid, 10)

    @property
    def mid(self) -> float | None:
        if self.spread is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def depth(self, points: float = 0.05) -> tuple[float, float]:
        """把价格推动 points 要花的钱（美元）：(往上, 往下)。

        往上：吃掉价格不超过 最优卖价 + points 的全部卖单，Σ 价格 × 数量；
        往下：吃掉价格不低于 最优买价 - points 的全部买单，卖出 YES 相当于买 NO，Σ (1 - 价格) × 数量。
        某一边没有挂单时那一边是 0。
        """
        up = down = Decimal(0)
        if self.asks:
            limit = self.asks[0][0] + points + _EPS
            up = sum((_dec(p) * _dec(s) for p, s in self.asks if p <= limit), Decimal(0))
        if self.bids:
            limit = self.bids[0][0] - points - _EPS
            down = sum(((1 - _dec(p)) * _dec(s) for p, s in self.bids if p >= limit), Decimal(0))
        return float(up), float(down)


@dataclass(frozen=True)
class Trade:
    """一笔主动成交（taker 一方）。wallet 是钱包地址换成的匿名键，不是地址本身。"""

    wallet: str
    side: str  # "BUY" | "SELL"，从这个钱包的角度
    size: float  # 份数
    price: float  # 每份的美元价格，0 到 1
    ts: int  # Unix 秒
    outcome_index: int  # 买卖的是第几个结果；999 表示没标


@dataclass(frozen=True)
class Resolution:
    """结算状态（Data API /v2/resolutions）。"""

    status: str  # initialized / posed / proposed / challenged / reproposed / disputed / resolved / active / arbitration，小写
    was_disputed: bool
    extended_review: bool
    new_version_q: bool  # 提问之后规则改过

    def clean(self) -> bool:
        """还没有人提结算结果、没有争议、没有延长审核、规则没改过。不认识的状态一律不算干净。"""
        return self.status in CLEAN_STATUS and not (self.was_disputed or self.extended_review or self.new_version_q)


def parse_event(d: dict) -> list[PMarket]:
    """解析 Gamma 的一个事件，返回下面能用的市场。

    跳过：缺 conditionId 的、协议版本不是 v1 / v2 的、多选事件里占位的“其他”（negRiskOther）。
    其余字段缺了或写错都用空值。不是对象时返回空列表。
    """
    if not isinstance(d, Mapping):
        return []
    event_id = _id(d.get("id"))
    title = _text(d.get("title"))
    tags = _event_tags(d)
    raw = d.get("markets")
    markets: list[PMarket] = []
    for m in raw if isinstance(raw, list) else ():
        try:
            markets.append(_parse_market(m, d, event_id, title, tags))
        except ValueError:
            continue
    return markets


def excluded(m: PMarket, settings: PolymarketSettings | None = None) -> bool:
    """不看的市场：体育、电竞、“会不会提到某个词”、短线涨跌、生死类的问题。

    标签 slug 在 exclude_tags 里（不分大小写），或者问题、事件标题、选项名里出现 exclude_words 里的词。
    词按不分大小写的子串匹配，标点当作空格，所以 " say " 能匹配 `say "rate"` 和句末的 `say?`，但不会误伤 essay。
    settings 默认用规则文件的默认值（和 config/markets.yaml 一致）。
    """
    s = settings if settings is not None else PolymarketSettings()
    bad_tags = {t.strip().lower() for t in s.exclude_tags if isinstance(t, str) and t.strip()}
    if any(t.lower() in bad_tags for t in m.tags):
        return True
    words = [w for w in (_pattern(w) for w in s.exclude_words if isinstance(w, str)) if w]
    return any(w in _plain(text) for text in (m.question, m.event_title, m.group_title) for w in words)


def wallet_stats(trades: list[Trade], outcome_index: int) -> tuple[int, float]:
    """推动某个结果价格的成交里有几个钱包，以及最大的一个钱包占净成交的比例。

    是非市场里买 NO 就等于卖 YES，同样会推动 YES 的价格，所以两个结果的成交都算，换成这个结果的份数：
    买这个结果记 +size、卖记 -size；买另一个结果记 -size、卖记 +size。没标结果（999）的成交不算。
    比例 = 最大的 |净份数| / 所有钱包 |净份数| 之和。没有成交，或所有钱包都买卖相抵（和为 0）时比例记 1.0
    （说明不了是很多人在买）。返回 (钱包数, 比例)。
    """
    other = 1 - outcome_index if outcome_index in (0, 1) else None
    net: dict[str, Decimal] = {}
    for t in trades:
        if t.side not in ("BUY", "SELL") or t.outcome_index not in (outcome_index, other):
            continue
        size = _dec(t.size)
        sign = 1 if (t.side == "BUY") == (t.outcome_index == outcome_index) else -1
        net[t.wallet] = net.get(t.wallet, Decimal(0)) + sign * size
    if not net:
        return 0, 1.0
    sizes = [abs(v) for v in net.values()]
    total = sum(sizes, Decimal(0))
    if total <= 0:
        return len(net), 1.0
    return len(net), float(max(sizes) / total)


class Polymarket:
    """Polymarket 公开接口的客户端。所有请求都经过 Fetcher（受预算约束）；FetchError 和 BudgetExceeded 原样抛出，
    只有事件列表翻页时后面某页失败会保留前面已经取到的。"""

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self._simple = False  # Gamma 不接受排序或协议版本参数（HTTP 422）后，本次运行都改用最简单的查询

    def events(self, tag_id: int, max_pages: int = 2) -> list[PMarket]:
        """某个标签下没关闭的事件里的全部市场（/events/keyset?closed=false），按 condition id 去重。

        事件没关闭不等于下面的市场都在交易，调用方还要看 closed、active 和 end。
        """
        pages = max(1, int(max_pages))
        markets: list[PMarket] = []
        seen: set[str] = set()
        cursor = ""
        for page in range(pages):
            try:
                data = self._events_page(tag_id, cursor)
            except FetchError as exc:
                if page == 0:
                    raise
                log.info("市场数据：Polymarket 事件列表第 %d 页没取到（%s），只用前面的", page + 1, exc.outcome)
                break
            if not isinstance(data, Mapping):
                break
            events = data.get("events")
            for event in events if isinstance(events, list) else ():
                for m in parse_event(event):
                    if m.condition_id not in seen:
                        seen.add(m.condition_id)
                        markets.append(m)
            # next_cursor 只在这一页满了时才有，最后一页没有这个键
            nxt = data.get("next_cursor")
            if not isinstance(nxt, str) or not nxt or nxt == cursor:
                break
            cursor = nxt
        else:
            log.info("市场数据：Polymarket 事件列表超过 %d 页，只取了前面的", pages)
        return markets

    def book(self, token_id: str) -> Book:
        """一个结果的 CLOB 盘口。已关闭或已结算的市场没有盘口，接口返回 404（FetchError）。"""
        return _parse_book(self.fetcher.get_json(f"{CLOB}/book", {"token_id": token_id}))

    def history(self, token_id: str, start_ts: int, bucket: int = 3600) -> list[tuple[int, float]]:
        """从 start_ts（Unix 秒，最多 15 天前）到现在的价格：[(时间, 价格)]，按时间排好。

        每个桶一个点（时间是桶的开始），最后一个点是最新的一次观测，间隔不一定整齐。
        """
        start = int(start_ts)
        if start <= 0:
            raise ValueError("start_ts 必须是正数")
        params = {"token_id": token_id, "start": start, "bucket_seconds": int(bucket)}
        points: dict[int, float] = {}
        for rows, _ in self._data_pages("/v2/prices-history", params, HISTORY_MAX_PAGES):
            for row in rows:
                ts = _int(row.get("timestamp"))
                price = _unit(row.get("price"))
                if ts is not None and price is not None:
                    points[ts] = price
        return sorted(points.items())

    def trades(self, condition_id: str, since_ts: int, max_pages: int = 2) -> list[Trade]:
        """一个市场 since_ts（Unix 秒，含）以来的主动成交，从新到旧。

        接口只在按钱包查时才认 start / end，按市场查只能从最新的往回翻，翻到比 since_ts 更早的记录就停。
        翻满 max_pages 页还没翻到时只有最近的一部分，要知道是否完整用 trades_since。
        """
        return self.trades_since(condition_id, since_ts, max_pages)[0]

    def trades_since(self, condition_id: str, since_ts: int, max_pages: int = 2) -> tuple[list[Trade], bool]:
        """同 trades，另外返回是否完整：翻到了比 since_ts 更早的记录，或者接口说已经没有下一页。"""
        since = int(since_ts)
        params = {"condition": condition_id, "taker_only": "true", "limit": TRADES_PAGE}
        trades: list[Trade] = []
        complete = False
        for rows, last in self._data_pages("/v2/trades", params, max(1, int(max_pages))):
            oldest: int | None = None
            for row in rows:
                trade = _parse_trade(row)
                if trade is None:
                    continue
                oldest = trade.ts if oldest is None else min(oldest, trade.ts)
                if trade.ts >= since:
                    trades.append(trade)
            if last or oldest is None or oldest < since:
                complete = True
                break
        return trades, complete

    def resolution(self, condition_id: str) -> Resolution | None:
        """一个市场的结算状态；查不到（空列表或 404）时返回 None。

        同一个市场返回好几行时取最不干净的：任何一行有争议就算有争议，状态取第一个不干净的。
        """
        try:
            data = self.fetcher.get_json(f"{DATA}/v2/resolutions", {"condition": condition_id})
        except FetchError as exc:
            if exc.status == 404:
                return None
            raise
        wanted = condition_id.strip().lower()
        rows = [r for r in _rows(data) if _text(r.get("condition_id")).lower() in ("", wanted)]
        return _merge_resolutions(rows) if rows else None

    def token_final(self, token_id: str) -> float | None:
        """一个结果已结算时的结算价（0 到 1，通常是 0 或 1）；还没结算或查不到时返回 None。"""
        try:
            data = self.fetcher.get_json(f"{DATA}/v2/tokens", {"token_id": token_id})
        except FetchError as exc:
            if exc.status == 404:
                return None
            raise
        for row in _rows(data):
            if _id(row.get("token_id")) != str(token_id):
                continue
            if row.get("resolved") is not True:
                return None
            return _unit(row.get("final_price"))
        return None

    def _events_page(self, tag_id: int, cursor: str) -> Any:
        url = f"{GAMMA}/events/keyset"
        try:
            return self.fetcher.get_json(url, self._event_params(tag_id, cursor))
        except FetchError as exc:
            if exc.status != 422 or self._simple or cursor:
                raise
        # 排序字段或 version 参数不被接受时，去掉它们再试一次（只会发生一次）
        log.info("市场数据：Polymarket 事件列表不接受排序参数（HTTP 422），改用最简单的查询")
        self._simple = True
        return self.fetcher.get_json(url, self._event_params(tag_id, cursor))

    def _event_params(self, tag_id: int, cursor: str) -> dict[str, Any]:
        params: dict[str, Any] = {"closed": "false", "tag_id": int(tag_id), "limit": EVENTS_PAGE}
        if not self._simple:
            params["version"] = list(VERSIONS)
            params["order"] = EVENTS_ORDER
            params["ascending"] = "false"
        if cursor:
            # 翻页时原样带上同样的筛选条件
            params["after_cursor"] = cursor
        return params

    def _data_pages(self, path: str, params: Mapping[str, Any], max_pages: int) -> Iterator[tuple[list[Mapping], bool]]:
        """Data API v2 按 pagination.next_cursor 翻页，每次给出 (一页的行, 是不是最后一页)。
        翻页时带上同样的筛选条件，不再带 limit。翻满 max_pages 页时最后给出的那页不算最后一页。"""
        cursor = ""
        for _ in range(max_pages):
            query = dict(params)
            if cursor:
                query.pop("limit", None)
                query["cursor"] = cursor
            data = self.fetcher.get_json(DATA + path, query)
            page = data.get("pagination") if isinstance(data, Mapping) else None
            nxt = page.get("next_cursor") if isinstance(page, Mapping) else None
            last = (
                not isinstance(page, Mapping)
                or page.get("has_more") is False
                or not isinstance(nxt, str)
                or not nxt
                or nxt == cursor
            )
            yield _rows(data), last
            if last:
                return
            cursor = nxt
        log.info("市场数据：Polymarket 数据接口超过 %d 页，只取了前面的", max_pages)


def _parse_market(
    m: Any, event: Mapping, event_id: str, event_title: str, event_tags: list[str]
) -> PMarket:
    if not isinstance(m, Mapping):
        raise ValueError("市场数据不是对象")
    condition_id = _text(m.get("conditionId"))
    if not condition_id:
        raise ValueError("市场缺少 conditionId")
    if m.get("negRiskOther") is True:
        raise ValueError("多选事件里占位的“其他”")
    version = _text(m.get("version")).lower() or "v1"
    if version not in VERSIONS:
        raise ValueError("不支持的协议版本")
    outcomes = _strings(_json_list(m.get("outcomes")))
    token_ids = _token_ids(m, version)
    if outcomes and token_ids and len(outcomes) != len(token_ids):
        token_ids = ()  # 对不上就不知道哪个 token 是哪个结果，宁可不用
    description = m.get("description")
    source = m.get("resolutionSource")
    rules = (description if isinstance(description, str) else "") + (source if isinstance(source, str) else "")
    return PMarket(
        id=_id(m.get("id")),
        condition_id=condition_id,
        question=_text(m.get("question")),
        outcomes=outcomes,
        token_ids=token_ids,
        best_bid=_price(m.get("bestBid")),
        best_ask=_price(m.get("bestAsk")),
        last=_price(m.get("lastTradePrice")),
        one_day_change=_change(m.get("oneDayPriceChange")),
        liquidity=_first_amount(m, ("liquidityNum", "liquidity", "liquidityClob")),
        volume_24h=_first_amount(m, ("volume24hr", "volume24hrClob")),
        end=_time(m.get("endDate")) or _time(event.get("endDate")),
        closed=m.get("closed") is True,
        active=m.get("active") is True and m.get("acceptingOrders") is not False and m.get("archived") is not True,
        uma_status=_uma_status(m, version),
        group_title=_text(m.get("groupItemTitle")),
        event_id=event_id,
        event_title=event_title,
        tags=_unique(event_tags + _market_tags(m)),
        description_hash=hashlib.sha256(rules.encode("utf-8")).hexdigest(),
    )


def _token_ids(m: Mapping, version: str) -> tuple[str, ...]:
    """v2 用 positionIds（数组），v1 用 clobTokenIds（JSON 字符串）；选中的没有时试另一个。"""
    keys = ("positionIds", "clobTokenIds") if version == "v2" else ("clobTokenIds", "positionIds")
    for key in keys:
        ids = _ids(_json_list(m.get(key)))
        if ids:
            return ids
    return ()


def _uma_status(m: Mapping, version: str) -> str:
    key = "resolutionStatus" if version == "v2" else "umaResolutionStatus"
    status = _text(m.get(key))
    if not status:
        history = [s.strip() for s in _json_list(m.get("umaResolutionStatuses")) if isinstance(s, str) and s.strip()]
        status = history[-1] if history else ""
    return status.lower()


def _event_tags(event: Mapping) -> list[str]:
    """事件标签的 slug，加上从标签编号、事件 slug、系列推出的几类要排除的标签。"""
    tags: list[str] = []
    raw = event.get("tags")
    for tag in raw if isinstance(raw, list) else ():
        if not isinstance(tag, Mapping):
            continue
        slug = _text(tag.get("slug")).lower()
        if slug:
            tags.append(slug)
            if slug.endswith("-mentions"):
                tags.append("mention-markets")
            if slug in _SHORT_TAGS:
                tags.append("up-or-down")
        derived = _TAG_BY_ID.get(_id(tag.get("id")))
        if derived:
            tags.append(derived)
    slug = _text(event.get("slug")).lower()
    if _UPDOWN_SLUG.match(slug):
        tags.append("up-or-down")
    if _MENTION_SLUG.match(slug):
        tags.append("mention-markets")
    series_slugs = [_text(event.get("seriesSlug")).lower()]
    series = event.get("series")
    for s in series if isinstance(series, list) else ():
        if isinstance(s, Mapping):
            series_slugs.append(_text(s.get("slug")).lower())
            if _text(s.get("recurrence")).lower() in _SHORT_RECURRENCE:
                tags.append("up-or-down")
    for s in series_slugs:
        if s.endswith("-mentions"):
            tags.append("mention-markets")
        if _UPDOWN_SERIES.search(s):
            tags.append("up-or-down")
    if event.get("gameId") not in (None, ""):
        tags.append("sports")
    return tags


def _market_tags(m: Mapping) -> list[str]:
    """市场本身带的体育和短线涨跌信号。"""
    tags: list[str] = []
    fee_type = _text(m.get("feeType")).lower()
    if _text(m.get("sportsMarketType")) or _text(m.get("gameStartTime")) or fee_type.startswith("sports_"):
        tags.append("sports")
    if _UPDOWN_SLUG.match(_text(m.get("slug")).lower()):
        tags.append("up-or-down")
    return tags


def _parse_book(d: Any) -> Book:
    """解析 /book；价格必须在 0 和 1 之间（不含），数量必须大于 0，不合格的档位跳过。"""
    if not isinstance(d, Mapping):
        return Book((), ())
    return Book(_levels(d.get("bids")), _levels(d.get("asks")))


def _levels(raw: Any) -> tuple[tuple[float, float], ...]:
    levels = []
    for level in raw if isinstance(raw, list) else ():
        if not isinstance(level, Mapping):
            continue
        price = _price(level.get("price"))
        size = _amount(level.get("size"))
        if price is not None and size is not None and size > 0:
            levels.append((price, size))
    return tuple(levels)


def _parse_trade(d: Mapping) -> Trade | None:
    """只读用得到的字段；钱包地址马上换成匿名键，个人资料字段不碰。"""
    wallet = _text(d.get("proxy_wallet"))
    side = _text(d.get("side")).upper()
    size = _amount(d.get("size"))
    price = _unit(d.get("price"))
    ts = _int(d.get("timestamp"))
    if not wallet or side not in ("BUY", "SELL") or size is None or size <= 0 or price is None or ts is None:
        return None
    index = _int(d.get("outcome_index"))
    return Trade(
        wallet=_wallet_key(wallet),
        side=side,
        size=size,
        price=price,
        ts=ts,
        outcome_index=index if index is not None else UNLABELED,
    )


def _wallet_key(address: str) -> str:
    # 地址有大小写混写的校验格式，同一个钱包先统一成小写
    return hashlib.blake2b(address.strip().lower().encode(), key=_WALLET_KEY, digest_size=8).hexdigest()


def _merge_resolutions(rows: list[Mapping]) -> Resolution:
    found = [_parse_resolution(r) for r in rows]
    dirty = [r.status for r in found if r.status not in CLEAN_STATUS]
    return Resolution(
        status=dirty[0] if dirty else found[0].status,
        was_disputed=any(r.was_disputed for r in found),
        extended_review=any(r.extended_review for r in found),
        new_version_q=any(r.new_version_q for r in found),
    )


def _parse_resolution(d: Mapping) -> Resolution:
    # 这几个标志按文档是必有的；缺了或写成别的样子时按“有问题”处理，宁可不显示
    return Resolution(
        status=_text(d.get("status")).lower(),
        was_disputed=d.get("was_disputed") is not False,
        extended_review=d.get("extended_review") is not False,
        new_version_q=d.get("new_version_q") is not False,
    )


def _rows(data: Any) -> list[Mapping]:
    """Data API v2 的 {data: [...]}；形状不对时当作没有。"""
    rows = data.get("data") if isinstance(data, Mapping) else None
    return [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []


def _pattern(word: str) -> str:
    """排除词：标点当作空格，保留首尾的空格（" say " 只匹配整词）。"""
    core = " ".join(_WORD.findall(word.lower()))
    if not core:
        return ""
    return (" " if word[:1].isspace() else "") + core + (" " if word[-1:].isspace() else "")


def _plain(text: str) -> str:
    """小写，只留字母和数字，词之间一个空格，两头各补一个空格。"""
    return " " + " ".join(_WORD.findall((text or "").lower())) + " "


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(v for v in values if v))


def _json_list(value: Any) -> list:
    """Gamma 把数组写成 JSON 字符串（"[\\"Yes\\", \\"No\\"]"），也可能是真正的数组或 null。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _strings(values: list) -> tuple[str, ...]:
    """全是字符串才用；混进别的类型时整个不用，免得下标错位。"""
    if not all(isinstance(v, str) for v in values):
        return ()
    return tuple(v.strip() for v in values)


def _ids(values: list) -> tuple[str, ...]:
    """token id：十进制数字的字符串（整数也认，转成字符串）；有一个不合格就整个不用。"""
    ids = []
    for v in values:
        text = _id(v)
        if not text.isdigit():
            return ()
        ids.append(text)
    return tuple(ids)


def _id(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    return value.strip() if isinstance(value, str) else ""


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


def _dec(value: float) -> Decimal:
    """浮点数按它的十进制写法转 Decimal（0.1 就是 0.1），求和时没有浮点误差。"""
    return Decimal(repr(float(value)))


def _amount(value: Any) -> float | None:
    """非负的金额或数量。"""
    number = _decimal(value)
    return float(number) if number is not None and number >= 0 else None


def _first_amount(m: Mapping, keys: tuple[str, ...]) -> float:
    for key in keys:
        amount = _amount(m.get(key))
        if amount is not None:
            return amount
    return 0.0


def _unit(value: Any) -> float | None:
    """0 到 1 之间（含两端）的价格：成交价、历史价格、结算价。"""
    number = _decimal(value)
    if number is None or number < 0 or number > 1:
        return None
    return float(number)


def _price(value: Any) -> float | None:
    """挂单价和最近成交价：0 和 1 不是能挂的价格，Gamma 省略的 0 也当作没有。"""
    price = _unit(value)
    return price if price is not None and 0 < price < 1 else None


def _change(value: Any) -> float | None:
    number = _decimal(value)
    if number is None or abs(number) > 1:
        return None
    return float(number)


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


def _time(value: Any) -> datetime | None:
    """宽松解析日期时间，转成 UTC：2026-11-04T05:00:00Z、带小数秒、空格分隔加 +00、错写成 +00:00:00 的时区、
    只有日期（当作 UTC 零点）都认；没写时区的当作 UTC。认不出时返回 None。"""
    if not isinstance(value, str):
        return None
    match = _TIME.match(value.strip())
    if not match:
        return None
    day, clock, fraction, zone = match.groups()
    clock = clock or "00:00:00"
    if len(clock) == 5:
        clock += ":00"
    text = f"{day}T{clock}"
    if fraction:
        text += "." + fraction[:6].ljust(6, "0")
    offset = "+00:00"
    if zone and zone not in ("Z", "z"):
        parts = _ZONE.match(zone)
        if parts is None:
            return None
        sign, hours, minutes = parts.groups()
        offset = f"{sign}{hours}:{minutes or '00'}"
    try:
        return datetime.fromisoformat(text + offset).astimezone(timezone.utc)
    except ValueError:
        return None
