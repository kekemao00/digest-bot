"""给市场板块对答案（设计文档第 9 节）：官方首次公布值、平台结算，以及每周一早上的“上周对答案”。

- 见证（witness）：日程表里已经公布、还没有见证记录的数据，取劳工统计局 v1 的首次公布值（CPI、核心 CPI 环比，
  非农新增人数，失业率），议息取纽约联储目标区间上限在声明前后的变化（基点）。每次运行最多 4 个请求，
  取不到的下次再取。只在公布后不久取（之后官方会修订，就不是首次公布值了）。
- 结算（settle）：展示过、已过期、还没有结算记录的合约，问平台的结果。每次最多 10 个。
- 每周一早上：上周结算的各件（同一个合约只算第一次展示，早晚重复的算一次；阶梯事件算一件
  “公布值落在一半把握区间”，概率取第一次展示的区间概率），应发生几件、正常范围、实际几件；平台结算和官方数据
  对不上的不算，单独说明。外加“定价一边倒”上线以来的累计，每月第一个周一再加一句价格整体的偏差。
  展示时间不早于过期时间的记录一律不算。

安全约定：这里的日志只有计数，没有合约代码、价格和结果；返回的记录只写进加密账本。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from digest.markets.backtest import covers
from digest.markets.kalshi import Kalshi, fed_outcome, settled_value
from digest.markets.ladder import Bound, Distribution
from digest.markets.net import BudgetExceeded, FetchError, Fetcher
from digest.markets.polymarket import Polymarket
from digest.markets.schedule import BJT, ET, first_monday_of_month, is_monday, release_at
from digest.markets.settings import DEFAULT_KEY_DATA, DEFAULT_WEEKLY, MarketsSettings
from digest.markets.sources import bls_series, first_print, nyfed_target_upper
from digest.markets.stats import calibration_in_the_large, corp, pb_range
from digest.markets.types import Line

log = logging.getLogger("digest.markets")

WITNESS_REQUESTS = 4  # 每次运行取官方数据最多这么多个请求（劳工统计局 v1 每天只给 25 次）
SETTLE_LIMIT = 10  # 每次运行最多问这么多个合约的结算
# Kalshi 系列 -> 劳工统计局序列的种类（settings.witnesses.series 的键，也是 sources.first_print 的 kind）
BLS_KINDS = {"KXCPI": "cpi", "KXCPICORE": "core", "KXPAYROLLS": "payrolls", "KXU3": "u3"}
# 公布后多久以内取首次公布值：下个月的报告会修订非农，年初会修订季调系数，过了就不是首次公布值
BLS_MAX_AGE = timedelta(days=25)
# 纽约联储接口只给最近 10 个工作日，声明日太早就看不到声明之前的那一天了
FOMC_MAX_AGE = timedelta(days=12)
FOMC_AFTER_DAYS = 7  # 声明之后第一条记录离声明日不能超过这么多天
GIVE_UP = timedelta(days=90)  # 过期这么久还查不到的合约记一条“查不到”，不再每次去问
VERDICT = "要攒到 250 到 300 件才能下结论"
SOURCE_BLS = "bls_v1"
SOURCE_NYFED = "nyfed"
KALSHI = "kalshi"
POLYMARKET = "polymarket"
# settle 记录的 status：平台的状态原样记；这几个是我们自己的
UNCLEAR = "unclear"  # 结算了，但结果说不清（作废、各档对不上）
MISSING = "missing"  # 过期很久了还查不到这个合约

_FED_CUT_BIG = "降息超过 25 个基点"
_FED_CUT = "降息 25 个基点"
_FED_HOLD = "维持不变"
_FED_HIKE = "加息 25 个基点"
_FED_HIKE_BIG = "加息超过 25 个基点"
_RULES = {rule.series: rule for rule in (*DEFAULT_KEY_DATA, *DEFAULT_WEEKLY)}
_EPS = 1e-9


# ---- 官方首次公布值 ----


@dataclass(frozen=True)
class _Pending:
    series: str  # Kalshi 系列代码
    period: tuple[int, int]
    kind: str  # cpi / core / payrolls / u3 / fomc
    release: datetime


def collect_witnesses(fetcher: Fetcher, settings: MarketsSettings, rows: list[dict], now: datetime) -> list[dict]:
    """取已经公布、账本里还没有的官方数据，返回新的 witness 记录（只有数字，要加密保存）。

    每个劳工统计局序列和纽约联储接口各一个请求（一次就够算好几期），按最早的公布时间排，最多 WITNESS_REQUESTS 个。
    网络错误、接口拒绝或数据还没出来时跳过，下次运行再取；预算用完时停下，返回已经取到的。
    """
    now = _aware(now)
    have = {(r.get("series"), r.get("period")) for r in rows if isinstance(r, Mapping) and r.get("type") == "witness"}
    groups: dict[str, list[_Pending]] = {}
    for item in _pending(settings, now, have):
        groups.setdefault(item.kind, []).append(item)  # 一个来源（一个序列或纽约联储）一个请求
    start = fetcher.requests
    found: list[dict] = []
    for source, items in sorted(groups.items(), key=lambda g: min(i.release for i in g[1])):
        if fetcher.requests - start >= WITNESS_REQUESTS:
            break
        try:
            if source == "fomc":
                values = _fomc_values(nyfed_target_upper(fetcher, settings.witnesses.nyfed), items)
                origin = SOURCE_NYFED
            else:
                series_id = settings.witnesses.series.get(source, "")
                data = bls_series(fetcher, series_id, base=settings.witnesses.bls_v1)
                values = {i: first_print(source, data, i.period) for i in items}
                origin = SOURCE_BLS
        except BudgetExceeded:
            break
        except (FetchError, ValueError) as exc:
            log.warning("市场核对：官方数据这次没取到（%s），下次再试", _reason(exc))
            continue
        for item, value in values.items():
            if value is not None and math.isfinite(value):
                found.append({
                    "type": "witness",
                    "ts": _iso(now),
                    "series": item.series,
                    "period": _period_text(item.period),
                    "value": value + 0.0,
                    "source": origin,
                    "fetched": _iso(now),
                })
    if groups:
        log.info("市场核对：待取官方数据 %d 类，这次取到 %d 条", len(groups), len(found))
    return found


def _pending(settings: MarketsSettings, now: datetime, have: set) -> list[_Pending]:
    """日程表里已经公布、还在可取时间内、账本里还没有见证记录的 (系列, 参考期)。"""
    found: list[_Pending] = []
    for rule in settings.kalshi.key_data:
        if rule.kind == "fed":
            kind, table, max_age = "fomc", "fomc", FOMC_MAX_AGE
        elif rule.series in BLS_KINDS and rule.release in ("cpi", "empsit"):
            kind, table, max_age = BLS_KINDS[rule.series], rule.release, BLS_MAX_AGE
        else:
            continue
        for period in _periods(settings, table):
            release = release_at(settings, table, period)
            if release is None or not (now - max_age <= release <= now):
                continue
            if (rule.series, _period_text(period)) not in have:
                found.append(_Pending(rule.series, period, kind, release))
    return found


def _periods(settings: MarketsSettings, table: str) -> list[tuple[int, int]]:
    releases = settings.releases
    if table == "fomc":
        return sorted({(d.year, d.month) for d in releases.fomc})
    return sorted(releases.cpi if table == "cpi" else releases.empsit)


def _fomc_values(rates: list[tuple[date, float]], items: list[_Pending]) -> dict[_Pending, float | None]:
    """议息的见证：目标区间上限在声明之后第一天和声明当天（或之前最后一天）的差，换成基点。

    新区间从声明的第二天起生效。声明前后两边都要有数据，而且之后那天离声明日不超过 FOMC_AFTER_DAYS 天，否则不算。
    """
    values: dict[_Pending, float | None] = {}
    for item in items:
        statement = item.release.astimezone(ET).date()
        before = [upper for day, upper in rates if day <= statement]
        after = [(day, upper) for day, upper in rates if day > statement]
        if not before or not after or (after[0][0] - statement).days > FOMC_AFTER_DAYS:
            values[item] = None
            continue
        change = (Decimal(str(after[0][1])) - Decimal(str(before[-1]))) * 100
        values[item] = float(change)
    return values


def _fed_label(bp: float) -> str | None:
    """目标区间上限变化（基点）对应的议息结果，和 kalshi.fed_outcome 的中文名一致；不是 25 的整数倍时为 None。"""
    if not isinstance(bp, (int, float)) or isinstance(bp, bool) or not math.isfinite(bp):
        return None
    step = round(bp)
    if abs(bp - step) > 1e-6:
        return None
    if step < -25:
        return _FED_CUT_BIG
    if step > 25:
        return _FED_HIKE_BIG
    return {-25: _FED_CUT, 0: _FED_HOLD, 25: _FED_HIKE}.get(step)


# ---- 平台结算 ----


def collect_settlements(
    kalshi: Kalshi | None,
    poly: Polymarket | None,
    rows: list[dict],
    now: datetime,
    limit: int = SETTLE_LIMIT,
) -> list[dict]:
    """展示过、已过期、还没有结算记录的合约，问平台要结果，返回新的 settle 记录。

    每个合约（venue + key）只问一次，最近过期的先问，最多 limit 个；还没结算完的下次再问。
    阶梯（记录里有 dist）按事件问，结果是公布值；单个合约结果是 1 或 0。结算了但说不清的记 status "unclear"，
    过期 90 天还查不到的记 "missing"，两种都不计分，也不再去问。kalshi 或 poly 为 None 时跳过那个平台。
    """
    now = _aware(now)
    settled = {_unit_key(r) for r in rows if isinstance(r, Mapping) and r.get("type") == "settle"}
    due = [(key, row, expires) for key, (row, expires) in _first_showings(rows).items()
           if key not in settled and expires <= now]
    due.sort(key=lambda item: item[2], reverse=True)
    found: list[dict] = []
    asked = failed = 0
    for (venue, key), row, expires in due:
        if asked >= limit:
            break
        if (venue == KALSHI and kalshi is None) or (venue == POLYMARKET and poly is None):
            continue
        if venue not in (KALSHI, POLYMARKET):
            continue
        asked += 1
        try:
            if venue == KALSHI:
                record = _settle_kalshi(kalshi, row, key, now, expires)
            else:
                record = _settle_poly(poly, row, key, now, expires)
            if record is not None:
                record = {"type": "settle", "ts": _iso(now), "venue": venue, "key": key, **record}
        except BudgetExceeded:
            break
        except Exception as exc:  # 网络错误或平台返回的数据不对：这个合约下次再问，其余照常
            failed += 1
            log.warning("市场结算：有一个合约这次没问到（%s）", _reason(exc))
            continue
        if record is not None:
            found.append(record)
    if due:
        log.info("市场结算：待查 %d 个，这次问了 %d 个，记下 %d 个，失败 %d 个", len(due), asked, len(found), failed)
    return found


def _settle_kalshi(kalshi: Kalshi, row: Mapping, key: str, now: datetime, expires: datetime) -> dict | None:
    series = row.get("series") if isinstance(row.get("series"), str) else ""
    if isinstance(row.get("dist"), Mapping):
        markets = kalshi.markets(key)
        if not markets:
            return _record(row, KALSHI, None, MISSING) if now - expires > GIVE_UP else None
        if any(m.status != "finalized" for m in markets):
            return None
        value = None
        if all(m.result in ("yes", "no") for m in markets):
            value = settled_value(_series_kind(row, series), markets)
        if not isinstance(value, (int, float)):
            return _record(row, KALSHI, None, UNCLEAR)
        return _record(row, KALSHI, float(value) + 0.0, "finalized")
    market = kalshi.market(key)
    if market is None:
        # key 也可能是只有一个合约的事件
        markets = kalshi.markets(key)
        market = markets[0] if len(markets) == 1 else None
    if market is None:
        return _record(row, KALSHI, None, MISSING) if now - expires > GIVE_UP else None
    if market.status != "finalized":
        return None
    if market.result not in ("yes", "no"):
        return _record(row, KALSHI, None, UNCLEAR)
    record = _record(row, KALSHI, 1 if market.result == "yes" else 0, market.status)
    if _series_kind(row, series) == "fed":
        label = fed_outcome(market)
        if label:
            record["label"] = label
    return record


def _settle_poly(poly: Polymarket, row: Mapping, key: str, now: datetime, expires: datetime) -> dict | None:
    condition, token = _poly_ids(row, key)
    if not token:
        return _record(row, POLYMARKET, None, UNCLEAR)
    final = poly.token_final(token)
    if final is None:
        return _record(row, POLYMARKET, None, MISSING) if now - expires > GIVE_UP else None
    if abs(final - 1) <= _EPS:
        outcome: int | None = 1
    elif abs(final) <= _EPS:
        outcome = 0
    else:
        return _record(row, POLYMARKET, None, UNCLEAR)  # 按比例兑付等情况，不算对错
    status, disputed = "resolved", False
    if condition:
        try:
            resolution = poly.resolution(condition)
        except FetchError:
            resolution = None  # 结果已经有了，状态取不到也照记
        if resolution is not None:
            status, disputed = resolution.status or status, resolution.was_disputed
    record = _record(row, POLYMARKET, outcome, status)
    record["disputed"] = disputed
    return record


def _record(row: Mapping, venue: str, outcome: float | int | None, status: str) -> dict:
    """settle 记录的内容（type、ts、venue、key 由调用方补上）。"""
    record: dict[str, Any] = {"outcome": outcome, "source": venue, "status": status}
    for name in ("series", "period", "section"):
        if isinstance(row.get(name), str):
            record[name] = row[name]
    return record


def _series_kind(row: Mapping, series: str) -> str:
    kind = row.get("kind")
    if isinstance(kind, str) and kind:
        return kind
    rule = _RULES.get(series)
    return rule.kind if rule else "ladder"


def _poly_ids(row: Mapping, key: str) -> tuple[str, str]:
    """Polymarket 的 key 是 "condition_id:token_id"；记录里单独有 condition / token 字段时优先用。"""
    condition = row.get("condition") if isinstance(row.get("condition"), str) else ""
    token = row.get("token") if isinstance(row.get("token"), str) else ""
    if not (condition and token):
        for sep in (":", "|", "/"):
            if sep in key:
                head, _, tail = key.partition(sep)
                condition, token = condition or head.strip(), token or tail.strip()
                break
    return condition, token


# ---- 每周一早上的对答案 ----


@dataclass(frozen=True)
class _Unit:
    """一件出了结果的事：概率 p，是否发生 y，什么时候记下的结算，平台和官方是否对不上。"""

    venue: str
    key: str
    section: str
    p: float
    y: int
    settled: datetime
    mismatch: bool

    @property
    def hit(self) -> bool:
        """“应验”：价格看好的那一边发生了（p ≥ 50% 时结果为是，p < 50% 时结果为否）。"""
        return self.y == 1 if self.p >= 0.5 else self.y == 0


def weekly_lines(rows: list[dict], now: datetime) -> list[Line]:
    """周一（北京时间）早间版的“上周对答案”几行；不是周一或没有可说的时返回空列表。调用方只在早间版调用。

    第一行：上周（北京时间周一到周日）记下结算的各件，应发生几件、正常范围、实际几件，对不上的件数；
    第二行：定价一边倒上线以来的累计；每月第一个周一再加一句上个月价格整体偏高还是偏低。
    数字记在第一行的 score 记录里（CORP 分解只进记录，不显示）。
    """
    now = _aware(now)
    if not is_monday(now):
        return []
    units = _units(rows)
    today = now.astimezone(BJT).date()
    week_start = datetime.combine(today - timedelta(days=7), time(0), tzinfo=BJT)
    week_end = datetime.combine(today, time(0), tzinfo=BJT)
    week = [u for u in units if week_start <= u.settled < week_end]
    scored = [u for u in week if not u.mismatch]
    mismatches = len(week) - len(scored)
    record: dict[str, Any] = {
        "type": "score",
        "ts": _iso(now),
        "week": week_start.date().isoformat(),
        "n": len(scored),
        "mismatches": mismatches,
    }
    texts: list[str] = []
    note = f"；{mismatches} 件平台结算和官方数据对不上，没算" if mismatches else ""
    if scored:
        ps = [u.p for u in scored]
        expected = math.fsum(ps)
        low, high = pb_range(ps)
        actual = sum(u.y for u in scored)
        record.update(expected=_round(expected), range=[low, high], actual=actual)
        texts.append(f"· 上周 {len(scored)} 件出了结果：应发生 {_one_decimal(expected)} 件"
                     f"（正常范围 {low} 到 {high}），实际 {actual} 件{note}")
    elif mismatches:
        texts.append(f"· 上周没有能算的结果{note}")
    lopsided = [u for u in units if u.section == "lopsided" and not u.mismatch and u.settled <= now]
    if lopsided:
        hits = sum(u.hit for u in lopsided)
        record["lopsided"] = {"n": len(lopsided), "hit": hits}
        texts.append(f"· 定价一边倒：上线以来 {len(lopsided)} 件应验 {hits} 件（{VERDICT}）")
    if first_monday_of_month(now):
        sentence = _monthly(units, today, record)
        if sentence:
            texts.append(sentence)
    if not texts:
        return []
    return [Line("score", text, rows=(record,) if i == 0 else ()) for i, text in enumerate(texts)]


def _monthly(units: list[_Unit], today: date, record: dict) -> str | None:
    """上个月（北京时间）记下结算的各件：价格整体偏高还是偏低（mean(p) − mean(y)）。CORP 只记进 score 记录。"""
    this_month = today.replace(day=1)
    last_month = (this_month - timedelta(days=1)).replace(day=1)
    start = datetime.combine(last_month, time(0), tzinfo=BJT)
    end = datetime.combine(this_month, time(0), tzinfo=BJT)
    month = [u for u in units if start <= u.settled < end and not u.mismatch]
    if not month:
        return None
    ps, ys = [u.p for u in month], [float(u.y) for u in month]
    citl = calibration_in_the_large(ps, ys)
    record["month"] = {
        "period": f"{last_month.year:04d}-{last_month.month:02d}",
        "n": len(month),
        "citl": _round(citl),
        "corp": {k: (_round(v) if isinstance(v, float) else v) for k, v in corp(ps, ys).items()},
    }
    points = int(Decimal(str(abs(citl) * 100)).quantize(Decimal("1"), ROUND_HALF_UP))
    if points == 0:
        return f"· 上月 {len(month)} 件已结算：价格整体和实际结果相差不到 1 个百分点（件数少，只作记录）"
    direction = "偏高" if citl > 0 else "偏低"
    return f"· 上月 {len(month)} 件已结算：价格整体{direction} {points} 个百分点（件数少，只作记录）"


def _units(rows: Iterable[Any]) -> list[_Unit]:
    """账本里能计分的各件：每个合约（或阶梯事件）第一次有效展示的概率，配上它的结算和官方数据。"""
    rows = [r for r in rows if isinstance(r, Mapping)]
    settles: dict[tuple[str, str], Mapping] = {}
    witnesses: dict[tuple[str, str], float] = {}
    for r in rows:
        if r.get("type") == "settle" and (key := _unit_key(r)) is not None:
            settles.setdefault(key, r)
        elif r.get("type") == "witness":
            value = _number(r.get("value"))
            if value is not None and isinstance(r.get("series"), str) and isinstance(r.get("period"), str):
                witnesses.setdefault((r["series"], r["period"]), value)
    units: list[_Unit] = []
    for key, (row, _) in _first_showings(rows).items():
        settle = settles.get(key)
        settled = _time(settle.get("ts")) if settle is not None else None
        if settle is None or settled is None:
            continue
        witness = witnesses.get((str(row.get("series")), str(row.get("period"))))
        unit = _ladder_unit(row, settle, witness) if isinstance(row.get("dist"), Mapping) else \
            _binary_unit(row, settle, witness)
        if unit is not None:
            p, y, mismatch = unit
            units.append(_Unit(key[0], key[1], str(row.get("section") or ""), p, y, settled, mismatch))
    return units


def _ladder_unit(row: Mapping, settle: Mapping, witness: float | None) -> tuple[float, int, bool] | None:
    """阶梯事件算一件：公布值是否落在第一次展示的一半把握区间里，概率是那个区间的概率。"""
    x = _number(settle.get("outcome"))
    if x is None:
        return None
    if witness is not None and not _same(x, witness):
        return 0.5, 0, True  # 对不上：不计分，只计数
    official = witness if witness is not None else x
    dist = row["dist"]
    series = str(row.get("series") or "")
    interval = _interval(dist, series)
    if interval is None:
        return None
    lo, hi = interval
    p = _mass50(dist, series, lo, hi)
    if p is None:
        return None
    return p, int(covers(lo, hi, official)), False


def _binary_unit(row: Mapping, settle: Mapping, witness: float | None) -> tuple[float, int, bool] | None:
    """单个合约算一件：显示的整数百分比和结果 1 / 0。议息合约再和纽约联储的目标区间变化核对。"""
    p = _number(row.get("p"))
    outcome = settle.get("outcome")
    if p is None or not 0 <= p <= 100 or isinstance(outcome, bool) or outcome not in (0, 1):
        return None
    y = int(outcome)
    mismatch = False
    label = settle.get("label")
    if isinstance(label, str) and label and witness is not None:
        official = _fed_label(witness)
        mismatch = official is None or (official == label) != (y == 1)
    return p / 100, y, mismatch


def _first_showings(rows: Iterable[Any]) -> dict[tuple[str, str], tuple[Mapping, datetime]]:
    """每个合约（venue, key）第一次有效的展示和它的过期时间。展示时间不早于过期时间的记录不算。"""
    shown = [r for r in rows if isinstance(r, Mapping) and r.get("type") == "shown"]
    # 按账本顺序；还没写进账本的记录（没有 seq）排在最后
    shown.sort(key=lambda r: r["seq"] if isinstance(r.get("seq"), int) and not isinstance(r.get("seq"), bool)
               else math.inf)
    found: dict[tuple[str, str], tuple[Mapping, datetime]] = {}
    for r in shown:
        key = _unit_key(r)
        ts, expires = _time(r.get("ts")), _time(r.get("expires"))
        if key is None or key in found or ts is None or expires is None or ts >= expires:
            continue
        found[key] = (r, expires)
    return found


def _unit_key(row: Mapping) -> tuple[str, str] | None:
    key = row.get("key")
    if not isinstance(key, str) or not key.strip():
        return None
    venue = row.get("venue") if isinstance(row.get("venue"), str) and row.get("venue") else KALSHI
    return venue, key.strip()


def _interval(dist: Mapping, series: str) -> tuple[Bound, Bound] | None:
    """第一次展示的一半把握区间：优先用记下的 q25 / q75，没有或写法不认识时从 ks 和 s 重算。"""
    lo, hi = _bound(dist.get("q25")), _bound(dist.get("q75"))
    if lo is not None and hi is not None:
        return lo, hi
    rebuilt = _distribution(dist, series)
    return rebuilt.interval50() if rebuilt is not None else None


def _mass50(dist: Mapping, series: str, lo: Bound, hi: Bound) -> float | None:
    mass = _number(dist.get("mass50"))
    if mass is not None and 0 <= mass <= 1:
        return mass
    rebuilt = _distribution(dist, series)
    if rebuilt is None:
        return None
    try:
        return rebuilt.mass(lo, hi)
    except ValueError:
        return None


def _bound(value: Any) -> Bound | None:
    """区间端点的几种记法：数字、[数值, 开口方向]、{"value": 数值, "open": 开口方向}。"""
    side: Any = None
    if isinstance(value, Mapping):
        side = next((value.get(k) for k in ("open", "open_side", "side") if k in value), None)
        value = value.get("value")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        value, side = value
    number = _number(value)
    if number is None or side not in (None, "below", "above"):
        return None
    return Bound(number, side)


def _distribution(dist: Mapping, series: str) -> Distribution | None:
    rule = _RULES.get(series)
    grid = dist.get("grid") if "grid" in dist else (rule.grid if rule else None)
    try:
        return Distribution(ks=dist["ks"], survival=dist["s"], grid=grid, used_last=False)
    except (KeyError, TypeError, ValueError):
        return None


# ---- 小工具 ----


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(b))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _one_decimal(value: float) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.1"), ROUND_HALF_UP))


def _round(value: float) -> float:
    return round(value, 6) + 0.0


def _period_text(period: tuple[int, int]) -> str:
    return f"{period[0]:04d}-{period[1]:02d}"


def _reason(exc: BaseException) -> str:
    """日志里的失败原因：FetchError 只有主机名和状态码，其他异常只写类型名。"""
    return str(exc) if isinstance(exc, FetchError) else type(exc).__name__


def _time(value: Any) -> datetime | None:
    """账本里的 ISO 时间（"2029-04-11T12:30:00Z" 或带偏移），不带时区的按 UTC。"""
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        return _aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return _aware(dt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
