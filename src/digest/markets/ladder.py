"""把一组"高于 k"的阶梯合约变成一个概率分布：单调修正（PAVA）、分位数、区间概率；互斥结果的归一化。

纯计算，只用标准库，不联网、不写日志；异常消息是固定的中文，不含任何数值。

约定：
- 分布里的阈值都是"严格大于"的阈值：S(k) = P(X > k)，P(X ≤ k) = 1 − S(k)。
- 离散网格（CPI、核心 CPI、失业率，间距 0.1）上，公布值 X 只取网格上的数。"大于等于 k"的合约换算成
  严格阈值 k − grid（X ≥ k 和 X > k − grid 是同一件事）；不在网格上的 k 也按同样的道理换算到网格点。
  连续的数（非农）里 X ≥ k 和 X > k 的概率相同，阈值直接用 k。
- 阶梯两端之外只知道总概率，不知道怎么分布：分位数落在外面时返回开口的 Bound（"不高于 k_1" /
  "高于 k_n"），不外推，也不算均值；cdf 在两端之外没有定义，直接报错。
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass

from digest.markets.types import Quote

# 用成交价定价的合约，权重按这个价差算（设计 4.1）
LAST_TRADE_SPREAD = 0.10
# 价差的下限：价差为 0 时权重也是有限的
MIN_SPREAD = 0.01
# 互斥结果的中间价加起来离 1 超过这么多，就认为报价不可信，整个事件跳过
MAX_BUCKET_GAP = 0.10
OPEN_SIDES = (None, "below", "above")

# 概率比较的容差（价格只有两位小数，浮点误差远小于它）
_EPS = 1e-9
# 一个数离网格点不超过 grid 的这个比例，就算落在网格点上
_GRID_TOL = 1e-6

_UNKNOWN = "这个位置在阶梯两端之外，概率未知"


def pava_decreasing(values: list[float], weights: list[float]) -> list[float]:
    """加权的单调不增回归（相邻违反者合并）：在单调不增的序列里找加权平方误差最小的那个，长度不变。"""
    if len(values) != len(weights):
        raise ValueError("数值和权重的个数不一样")
    # 每一块：[加权平均, 总权重, 个数]
    blocks: list[list[float]] = []
    for value, weight in zip(values, weights):
        value, weight = float(value), float(weight)
        if not math.isfinite(value):
            raise ValueError("数值必须是有限的数")
        if not (math.isfinite(weight) and weight > 0):
            raise ValueError("权重必须是正数")
        blocks.append([value, weight, 1])
        # 前一块比新块小就违反了单调不增，合并后再和更前面的块比
        while len(blocks) > 1 and blocks[-2][0] < blocks[-1][0]:
            mean2, weight2, count2 = blocks.pop()
            mean1, weight1, count1 = blocks.pop()
            total = weight1 + weight2
            blocks.append([(mean1 * weight1 + mean2 * weight2) / total, total, count1 + count2])
    fitted: list[float] = []
    for mean, _, count in blocks:
        fitted.extend([mean] * int(count))
    return fitted


@dataclass(frozen=True)
class Strike:
    """阶梯里的一个"高于 k"合约：X > k 时兑付；inclusive=True 表示 X ≥ k 时兑付。"""

    k: float
    quote: Quote
    inclusive: bool = False


@dataclass(frozen=True)
class Bound:
    """分位数或区间端点。

    open_side 为 None：就是 value 这个数；
    "below"：落在阶梯最低阈值以下，意思是 X ≤ value（显示"不高于 value"）；
    "above"：落在阶梯最高阈值以上，意思是 X > value（显示"高于 value"）。
    """

    value: float
    open_side: str | None = None

    def __post_init__(self) -> None:
        if self.open_side not in OPEN_SIDES:
            raise ValueError("open_side 只能是 None、below 或 above")


@dataclass(frozen=True)
class Distribution:
    """一个阶梯修正后的分布。ks 是严格递增的严格阈值，survival[i] = P(X > ks[i])，单调不增，在 0 到 1 之间。

    grid 为离散网格的间距（CPI 等是 0.1），连续的数（非农）为 None。也可以用账本里存下的 ks 和 s
    直接构造（列表会转成元组，并检查顺序和范围）。
    """

    ks: tuple[float, ...]
    survival: tuple[float, ...]
    grid: float | None
    used_last: bool  # 有合约用了成交价（显示时标"成交价"）

    def __post_init__(self) -> None:
        grid = self.grid
        if grid is not None:
            grid = float(grid)
            if not (math.isfinite(grid) and grid > 0):
                raise ValueError("网格间距必须是正数")
        ks = tuple(float(k) for k in self.ks)
        survival = tuple(float(s) for s in self.survival)
        if not ks or len(ks) != len(survival):
            raise ValueError("阈值和概率的个数不对")
        if not all(math.isfinite(k) for k in ks):
            raise ValueError("阈值必须是有限的数")
        if grid is not None:
            ks = tuple(_on_grid(k, grid) for k in ks)
        if any(b <= a for a, b in zip(ks, ks[1:])):
            raise ValueError("阈值必须严格递增")
        if not all(math.isfinite(s) and -_EPS <= s <= 1 + _EPS for s in survival):
            raise ValueError("概率必须在 0 到 1 之间")
        if any(b > a + _EPS for a, b in zip(survival, survival[1:])):
            raise ValueError("概率必须随阈值单调不增")
        # 去掉容差以内的浮点误差：截到 [0, 1]，再保证严格的单调不增
        cleaned: list[float] = []
        for s in survival:
            s = min(1.0, max(0.0, s))
            cleaned.append(min(s, cleaned[-1]) if cleaned else s)
        object.__setattr__(self, "grid", grid)
        object.__setattr__(self, "ks", ks)
        object.__setattr__(self, "survival", tuple(cleaned))

    def cdf(self, x: float) -> float:
        """P(X ≤ x)。

        离散网格上按 X 只取网格值处理：x 不在网格点上时等于不超过 x 的最大网格点处的值。
        相邻阈值之间（连续的数，或网格上缺了档的地方）线性插值。阶梯两端之外概率未知，报 ValueError
        （网格上 x 落在 [k_n, k_n + grid) 时仍等于 P(X ≤ k_n)）。
        """
        x = float(x)
        if not math.isfinite(x):
            raise ValueError(_UNKNOWN)
        if self.grid is not None:
            x = _floor_grid(x, self.grid)
        tol = self._tol(x)
        if x < self.ks[0] - tol or x > self.ks[-1] + tol:
            raise ValueError(_UNKNOWN)
        return self._interp(x)

    def quantile(self, q: float) -> Bound:
        """q 分位数（0 < q < 1）：使 P(X ≤ x) ≥ q 的最小 x。

        离散网格：只在阈值里找，第一个达到 q 的阈值就是答案；第一个阈值就已达到时是 Bound(k_1, "below")
        （不高于 k_1），所有阈值都没达到时是 Bound(k_n, "above")（高于 k_n）。
        连续：在相邻两个阈值之间线性插值；落在阶梯之外同样返回开口的 Bound，不外推。
        """
        q = float(q)
        if not 0 < q < 1:
            raise ValueError("分位数必须在 0 和 1 之间")
        ks, survival = self.ks, self.survival
        index = next((i for i, s in enumerate(survival) if 1 - s >= q - _EPS), None)
        if index is None:
            return Bound(ks[-1], "above")
        if self.grid is not None:
            # X ≤ k_i 包含 k_i 本身，所以 cdf 第一次达到 q 的阈值就是分位数；第一个阈值以下的数不知道怎么分布
            return Bound(ks[0], "below") if index == 0 else Bound(ks[index])
        if index == 0:
            # 恰好在 k_1 达到 q 时分位数就是 k_1；超过 q 说明分位数在 k_1 以下，具体在哪不知道
            return Bound(ks[0]) if 1 - survival[0] <= q + _EPS else Bound(ks[0], "below")
        lo, hi = ks[index - 1], ks[index]
        s_lo, s_hi = survival[index - 1], survival[index]
        # S(k_lo) > 1 - q ≥ S(k_hi)，分母一定大于 0
        frac = (s_lo - (1 - q)) / (s_lo - s_hi)
        return Bound(lo + (hi - lo) * min(1.0, max(0.0, frac)))

    def median(self) -> Bound:
        return self.quantile(0.5)

    def interval50(self) -> tuple[Bound, Bound]:
        """一半把握的区间：(25% 分位数, 75% 分位数)。"""
        return self.quantile(0.25), self.quantile(0.75)

    def mass(self, lo: Bound, hi: Bound) -> float:
        """X 落在闭区间 [lo, hi] 的概率。开口的端点按它表示的范围算：Bound(k_1, "below") 作下端时
        从负无穷算起，Bound(k_n, "above") 作上端时算到正无穷；"above" 作下端表示 X > value。
        端点落在阶梯两端之外、概率未知时报 ValueError（quantile 给出的端点不会出现这种情况）。
        """
        upper = 1.0 if hi.open_side == "above" else self.cdf(hi.value)
        if lo.open_side == "below":
            lower = 0.0
        elif lo.open_side == "above":
            lower = self.cdf(lo.value)
        else:
            lower = self._below(lo.value)
        return min(1.0, max(0.0, upper - lower))

    def _below(self, v: float) -> float:
        """P(X < v)。连续时等于 P(X ≤ v)；网格上等于比 v 小的最大网格点处的 P(X ≤ ·)。"""
        if self.grid is None:
            return self.cdf(v)
        # 不小于 v 的最小网格点，再往下一格
        steps = math.ceil(float(v) / self.grid - _GRID_TOL) - 1
        return self.cdf(round(steps * self.grid, 10))

    def _tol(self, x: float) -> float:
        if self.grid is not None:
            return self.grid * _GRID_TOL
        return _EPS * max(1.0, abs(x))

    def _interp(self, x: float) -> float:
        """阶梯范围内（含两端）的 P(X ≤ x)：在阈值上取原值，阈值之间线性插值。"""
        ks, survival = self.ks, self.survival
        tol = self._tol(x)
        i = bisect_left(ks, x - tol)
        if i < len(ks) and abs(ks[i] - x) <= tol:
            return 1 - survival[i]
        lo, hi = ks[i - 1], ks[i]
        frac = (x - lo) / (hi - lo)
        return 1 - (survival[i - 1] + frac * (survival[i] - survival[i - 1]))


@dataclass(frozen=True)
class _Point:
    t: float  # 严格阈值
    p: float  # 价格（P(X > t)）
    w: float  # 权重
    last: bool  # 用了成交价
    bid: float | None
    ask: float | None


def fill_one_sided(quote: Quote, max_spread: float = 0.10) -> Quote:
    """补全只有一边报价的冷门合约：没有买单而卖价已经贴着 0（不超过 max_spread）时买价当作 0；
    没有卖单而买价已经贴着 1 时卖价当作 1。另一边离 0 或 1 还远时不补，免得把真正空着的盘口当成有价。

    阶梯两端和互斥结果里冷门的一档几乎总是这样（买价 0 / 卖价 1 在接口里就是“没有挂单”）。不补的话
    Quote.mid 会退回很久以前的成交价。面板和回测都先过这一步，两边的归一化和分布才一致。
    """
    bid, ask = quote.bid, quote.ask
    if bid is None and ask is not None and ask <= max_spread + _EPS:
        bid = 0.0
    if ask is None and bid is not None and bid >= 1 - max_spread - _EPS:
        ask = 1.0
    if bid is quote.bid and ask is quote.ask:
        return quote
    return Quote(bid, ask, quote.last)


def build_ladder(strikes: list[Strike], grid: float | None, max_spread: float = 0.10) -> Distribution | None:
    """阶梯合约 -> 分布。可用的合约少于两个（去掉没价格的和报价过期的之后）时返回 None。

    价格用 Quote.mid(max_spread)：价差不超过 max_spread 时取买卖价中间值，否则取成交价。
    权重 1 / max(价差, 0.01)²，用成交价的合约价差按 0.10 算。若阈值更高的合约买价高于阈值更低的
    合约卖价，说明前者的报价过期了，先去掉它。同一个严格阈值出现多次时按权重合并。最后用加权
    PAVA 修成单调不增。
    离散网格上，留下的相邻阈值之间缺了档（中间某档没价格或报价过期被去掉）时也返回 None：缺口里的
    概率怎么分不知道，分位数落进去就只能报一个假装精确的数，宁可整个事件不放。
    """
    if grid is not None and not (math.isfinite(grid) and grid > 0):
        raise ValueError("网格间距必须是正数")
    points: list[_Point] = []
    for strike in strikes:
        k = float(strike.k)
        if not math.isfinite(k):
            continue
        priced = strike.quote.mid(max_spread)
        if priced is None or not math.isfinite(priced[0]):
            continue
        price, last = priced
        # 用买卖价中间值时两边一定都在，价差不会是 None
        spread = LAST_TRADE_SPREAD if last else (strike.quote.spread or 0.0)
        weight = 1.0 / max(spread, MIN_SPREAD) ** 2
        points.append(_Point(_threshold(k, strike.inclusive, grid), min(1.0, max(0.0, price)), weight, last,
                             strike.quote.bid, strike.quote.ask))
    points.sort(key=lambda point: point.t)
    merged = _merge_ties(_drop_stale(points))
    if len(merged) < 2 or (grid is not None and _has_gap([point.t for point in merged], grid)):
        return None
    fitted = pava_decreasing([point.p for point in merged], [point.w for point in merged])
    return Distribution(
        ks=tuple(point.t for point in merged),
        survival=tuple(min(1.0, max(0.0, s)) for s in fitted),
        grid=grid,
        used_last=any(point.last for point in merged),
    )


def normalize_buckets(quotes: Sequence[Quote], max_spread: float = 0.10) -> list[float] | None:
    """互斥结果（议息结果、标普年底区间）的概率：p_i = mid_i / Σmid，不做 Shin 或幂次调整。

    任何一个结果没有可用价格、少于两个结果、|Σmid − 1| > 0.10、所有卖价都在而 Σask < 1、
    或所有买价都在而 Σbid > 1 时返回 None（跳过这个事件）。
    """
    quotes = list(quotes)
    if len(quotes) < 2:
        return None
    mids: list[float] = []
    for quote in quotes:
        priced = quote.mid(max_spread)
        if priced is None or not math.isfinite(priced[0]):
            return None
        mids.append(min(1.0, max(0.0, priced[0])))
    total = sum(mids)
    if abs(total - 1) > MAX_BUCKET_GAP + _EPS:
        return None
    asks = [quote.ask for quote in quotes]
    if all(ask is not None for ask in asks) and sum(asks) < 1 - _EPS:
        return None
    bids = [quote.bid for quote in quotes]
    if all(bid is not None for bid in bids) and sum(bids) > 1 + _EPS:
        return None
    return [mid / total for mid in mids]


def _threshold(k: float, inclusive: bool, grid: float | None) -> float:
    """合约的严格阈值 t，使"兑付"等价于 X > t。"""
    if grid is None:
        return k
    if inclusive:
        # X ≥ k 等价于 X ≥ 不小于 k 的最小网格点，也就是 X > 它往下一格
        steps = math.ceil(k / grid - _GRID_TOL) - 1
    else:
        # X > k 等价于 X > 不超过 k 的最大网格点
        steps = math.floor(k / grid + _GRID_TOL)
    return round(steps * grid, 10)


def _drop_stale(points: list[_Point]) -> list[_Point]:
    """按阈值从低到高，去掉买价高于某个更低阈值（已保留的）合约卖价的合约。"""
    kept: list[_Point] = []
    for point in points:
        stale = point.bid is not None and any(
            other.ask is not None and other.t < point.t and point.bid > other.ask + _EPS for other in kept
        )
        if not stale:
            kept.append(point)
    return kept


def _has_gap(ts: Sequence[float], grid: float) -> bool:
    """网格上相邻两个阈值之间隔了不止一格。ts 已按从小到大排好。"""
    return any(b - a > grid * (1 + _GRID_TOL) for a, b in zip(ts, ts[1:]))


def _merge_ties(points: list[_Point]) -> list[_Point]:
    """同一个严格阈值的几个合约合并成一个：价格按权重平均，权重相加。points 已按阈值排好序。"""
    merged: list[_Point] = []
    for point in points:
        if merged and merged[-1].t == point.t:
            prev = merged[-1]
            weight = prev.w + point.w
            merged[-1] = _Point(prev.t, (prev.p * prev.w + point.p * point.w) / weight, weight,
                                prev.last or point.last, None, None)
        else:
            merged.append(point)
    return merged


def _floor_grid(x: float, grid: float) -> float:
    """不超过 x 的最大网格点（容许浮点误差）。"""
    return round(math.floor(x / grid + _GRID_TOL) * grid, 10)


def _on_grid(k: float, grid: float) -> float:
    """把落在网格点上的 k 规整成干净的浮点数；不在网格点上时报错。"""
    steps = round(k / grid)
    if abs(k - steps * grid) > grid * _GRID_TOL:
        raise ValueError("离散网格上的阈值必须是网格点")
    return round(steps * grid, 10)
