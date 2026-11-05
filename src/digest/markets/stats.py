"""给预测打分的统计量：泊松二项分布（每周"应发生几件"和正常范围）、RPS、PIT、Brier 和 BSS、
CORP 可靠性分解、整体偏差。

纯计算，只用标准库，不联网、不写日志；异常消息是固定的中文，不含任何数值。
二元结果的概率 ps 和结果 ys 都要求在 0 到 1 之间（ys 一般是 0 或 1，True/False 也行）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from digest.markets.ladder import Distribution, pava_decreasing

# 累计概率比较的容差，吸收逐项相加的浮点误差
_EPS = 1e-9
# 和 ladder 一致：离网格点不超过 grid 的这个比例就算在网格点上
_GRID_TOL = 1e-6
# CORP 的分解项理论上不小于 0，浮点误差带来的极小负数归零
_NOISE = 1e-12


def poisson_binomial(ps: list[float]) -> list[float]:
    """若干独立事件各自以 p_i 发生，发生件数的分布：返回 0..n 每个件数的概率（动态规划）。"""
    pmf = [1.0]
    for p in ps:
        p = _prob(p)
        nxt = [0.0] * (len(pmf) + 1)
        for count, d in enumerate(pmf):
            nxt[count] += d * (1 - p)
            nxt[count + 1] += d * p
        pmf = nxt
    return pmf


def pb_range(ps: list[float], lo: float = 0.05, hi: float = 0.95) -> tuple[int, int]:
    """发生件数的"正常范围"：(累计概率第一次达到 lo 的件数, 第一次达到 hi 的件数)。"""
    if not 0 <= lo <= hi <= 1:
        raise ValueError("范围的上下限必须满足 0 ≤ lo ≤ hi ≤ 1")
    pmf = poisson_binomial(ps)
    a = b = None
    cumulative = 0.0
    for count, d in enumerate(pmf):
        cumulative += d
        if a is None and cumulative >= lo - _EPS:
            a = count
        if cumulative >= hi - _EPS:
            b = count
            break
    n = len(pmf) - 1
    # 浮点误差让总和略小于 1 时，没达到的那一端取最大件数
    return (n if a is None else a), (n if b is None else b)


def rps(dist: Distribution, outcome: float) -> float:
    """排序概率得分 Σ_k (S(k) − 1{outcome > k})²，k 取分布的各个阈值；越小越好。"""
    x = _finite(outcome)
    total = 0.0
    for k, s in zip(dist.ks, dist.survival):
        hit = 1.0 if x > k + _tol(dist, k) else 0.0
        total += (s - hit) ** 2
    return total


def pit(dist: Distribution, outcome: float, u: float = 0.5) -> float:
    """概率积分变换：公布值在市场分布里的位置，校准良好时在 0 到 1 上均匀分布。

    连续（非农）：阶梯范围内是 F(x)。离散网格：F(x−) + u·(F(x) − F(x−))，x 先取不超过它的最大网格点。
    阶梯两端之外的概率只知道总量，把整段当成一格：x 落在 k_1 及以下时是 u·F(k_1)，
    高于 k_n 时是 F(k_n) + u·S(k_n)。
    """
    u = float(u)
    if not 0 <= u <= 1:
        raise ValueError("u 必须在 0 和 1 之间")
    x = _finite(outcome)
    ks, survival = dist.ks, dist.survival
    first, last = 1 - survival[0], 1 - survival[-1]
    if dist.grid is None:
        if x < ks[0] - _tol(dist, ks[0]):
            return u * first
        if x > ks[-1] + _tol(dist, ks[-1]):
            return last + u * survival[-1]
        return dist.cdf(x)
    x = round(math.floor(x / dist.grid + _GRID_TOL) * dist.grid, 10)
    tol = _tol(dist, x)
    if x <= ks[0] + tol:
        return u * first
    if x > ks[-1] + tol:
        return last + u * survival[-1]
    below = dist.cdf(x - dist.grid)
    return below + u * (dist.cdf(x) - below)


def brier(ps: Sequence[float], ys: Sequence[float]) -> float:
    """Brier 分数：mean((p − y)²)，越小越好。"""
    ps, ys = _pairs(ps, ys)
    return _mean([(p - y) ** 2 for p, y in zip(ps, ys)])


def bss(ps: Sequence[float], ys: Sequence[float], ref_ps: Sequence[float] | float) -> float | None:
    """Brier 技能分 1 − BS / BS_ref；ref_ps 是参照预测（每件一个，或一个数用于所有件，如历史基准率）。
    参照的 Brier 分数为 0 时没有定义，返回 None。"""
    if isinstance(ref_ps, (int, float)):
        ref_ps = [ref_ps] * len(ps)
    reference = brier(ref_ps, ys)
    if reference <= 0:
        return None
    return 1 - brier(ps, ys) / reference


def pav_increasing(xs: Sequence[float], ys: Sequence[float]) -> list[float]:
    """y 对 x 的单调不减回归（PAV），用于 CORP。x 相同的点先合并（拟合值相同），返回值和输入一一对应。"""
    if len(xs) != len(ys):
        raise ValueError("两组数的个数不一样")
    xs = [_finite(x) for x in xs]
    ys = [_finite(y) for y in ys]
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    groups: list[list[int]] = []
    for i in order:
        if groups and xs[groups[-1][0]] == xs[i]:
            groups[-1].append(i)
        else:
            groups.append([i])
    means = [_mean([ys[i] for i in group]) for group in groups]
    # 单调不减 = 取负后单调不增
    fitted = [-v for v in pava_decreasing([-m for m in means], [float(len(group)) for group in groups])]
    out = [0.0] * len(xs)
    for group, value in zip(groups, fitted):
        for i in group:
            out[i] = value
    return out


def corp(ps: Sequence[float], ys: Sequence[float]) -> dict:
    """CORP 可靠性分解（以 Brier 分数 S 为准）：MCB = S(p) − S(PAV)（校准偏差），DSC = S(ȳ) − S(PAV)（区分度），
    UNC = S(ȳ)（不确定性），BS = S(p) = MCB − DSC + UNC。不用固定分箱。"""
    ps, ys = _pairs(ps, ys)
    fitted = pav_increasing(ps, ys)
    ybar = _mean(ys)
    score = brier(ps, ys)
    score_pav = _mean([(f - y) ** 2 for f, y in zip(fitted, ys)])
    score_bar = _mean([(ybar - y) ** 2 for y in ys])
    return {
        "n": len(ps),
        "mcb": _nonneg(score - score_pav),
        "dsc": _nonneg(score_bar - score_pav),
        "unc": score_bar,
        "bs": score,
    }


def calibration_in_the_large(ps: Sequence[float], ys: Sequence[float]) -> float:
    """整体偏差 mean(p) − mean(y)：正数表示价格整体偏高。"""
    ps, ys = _pairs(ps, ys)
    return _mean(ps) - _mean(ys)


def _tol(dist: Distribution, x: float) -> float:
    if dist.grid is not None:
        return dist.grid * _GRID_TOL
    return _EPS * max(1.0, abs(x))


def _finite(x: float) -> float:
    x = float(x)
    if not math.isfinite(x):
        raise ValueError("必须是有限的数")
    return x


def _prob(p: float) -> float:
    p = _finite(p)
    if not 0 <= p <= 1:
        raise ValueError("概率必须在 0 到 1 之间")
    return p


def _pairs(ps: Sequence[float], ys: Sequence[float]) -> tuple[list[float], list[float]]:
    """检查一组预测和结果：个数相同、不为空、都在 0 到 1 之间。"""
    if len(ps) != len(ys):
        raise ValueError("预测和结果的个数不一样")
    if not ps:
        raise ValueError("没有可算的数据")
    return [_prob(p) for p in ps], [_prob(y) for y in ys]


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def _nonneg(x: float) -> float:
    return 0.0 if -_NOISE < x < 0 else x
