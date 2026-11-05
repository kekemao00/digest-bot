from __future__ import annotations

import random

import pytest

from digest.markets.ladder import (
    Bound,
    Distribution,
    Strike,
    build_ladder,
    fill_one_sided,
    normalize_buckets,
    pava_decreasing,
)
from digest.markets.types import Quote

# 这里的数都是编的，只有设计文档 12 节点名的 CPI 例子用了文档里写的概率


def quote(bid=None, ask=None, last=None) -> Quote:
    return Quote(bid=bid, ask=ask, last=last)


def tight(p: float, half: float = 0.01) -> Quote:
    """中间价为 p、价差 2*half 的报价。"""
    return Quote(bid=round(p - half, 4), ask=round(p + half, 4), last=None)


def approx(values):
    return pytest.approx(values, abs=1e-12)


def reference_antitonic(values, weights):
    """单调不增回归的 min-max 公式：f_i = min_{s≤i} max_{t≥i} 加权平均(values[s..t])，用来核对 PAVA。"""
    n = len(values)

    def average(s, t):
        w = sum(weights[s:t + 1])
        return sum(v * x for v, x in zip(values[s:t + 1], weights[s:t + 1])) / w

    return [min(max(average(s, t) for t in range(i, n)) for s in range(i + 1)) for i in range(n)]


def random_distribution(rng: random.Random, grid: float | None) -> Distribution:
    n = rng.randint(2, 8)
    if grid is None:
        ks = sorted(rng.sample(range(-50, 300), n))
        ks = [k * 1000.0 for k in ks]
    else:
        start = rng.randint(-5, 5)
        ks = [round((start + i) * grid, 10) for i in range(n)]
    survival = sorted((rng.random() for _ in range(n)), reverse=True)
    return Distribution(ks=tuple(ks), survival=tuple(survival), grid=grid, used_last=False)


# ---------- PAVA ----------

def test_pava_pools_adjacent_violators():
    assert pava_decreasing([0.9, 0.5, 0.6, 0.2], [1, 1, 1, 1]) == approx([0.9, 0.55, 0.55, 0.2])
    # 合并后还要继续和前面的块比
    assert pava_decreasing([0.3, 0.5, 0.7], [1, 1, 1]) == approx([0.5, 0.5, 0.5])
    # 加权平均：权重大的点几乎不动
    assert pava_decreasing([0.4, 0.6], [3, 1]) == approx([0.45, 0.45])
    assert pava_decreasing([0.40, 0.60], [100, 10_000]) == approx([(0.40 * 100 + 0.60 * 10_000) / 10_100] * 2)


def test_pava_keeps_monotone_input_and_edge_cases():
    assert pava_decreasing([0.8, 0.8, 0.3, 0.0], [1, 2, 3, 4]) == [0.8, 0.8, 0.3, 0.0]
    assert pava_decreasing([], []) == []
    assert pava_decreasing([0.42], [5]) == [0.42]
    with pytest.raises(ValueError):
        pava_decreasing([0.1, 0.2], [1])
    with pytest.raises(ValueError):
        pava_decreasing([0.1, 0.2], [1, 0])
    with pytest.raises(ValueError):
        pava_decreasing([0.1, float("nan")], [1, 1])


def test_pava_matches_min_max_formula():
    rng = random.Random(20261105)
    for _ in range(300):
        n = rng.randint(1, 9)
        values = [rng.random() for _ in range(n)]
        weights = [rng.choice([1.0, 4.0, 100.0, 2500.0]) for _ in range(n)]
        fitted = pava_decreasing(values, weights)
        assert len(fitted) == n
        assert all(b <= a + 1e-12 for a, b in zip(fitted, fitted[1:]))
        assert fitted == pytest.approx(reference_antitonic(values, weights), abs=1e-9)
        # 加权总和不变
        assert sum(f * w for f, w in zip(fitted, weights)) == pytest.approx(sum(v * w for v, w in zip(values, weights)))


# ---------- 离散网格（CPI 等） ----------

def test_cpi_worked_example_from_design():
    """设计文档的例子：P(>0.2)=0.58，P(>0.3)=0.17。中位数 0.3；只有这两档时 25% 分位数在 0.2 及以下。"""
    strikes = [Strike(0.2, tight(0.58)), Strike(0.3, tight(0.17))]
    dist = build_ladder(strikes, grid=0.1)
    assert dist is not None
    assert dist.ks == (0.2, 0.3)
    assert dist.survival == approx((0.58, 0.17))
    assert dist.median() == Bound(0.3)
    assert dist.interval50() == (Bound(0.2, "below"), Bound(0.3))

    # 加一档 P(>0.1)=0.9：25% 分位数确定是 0.2，75% 分位数是 0.3
    dist = build_ladder([Strike(0.1, tight(0.9))] + strikes, grid=0.1)
    assert dist.median() == Bound(0.3)
    assert dist.interval50() == (Bound(0.2), Bound(0.3))
    # 0.2 和 0.3 两个值的概率：(0.9 − 0.58) + (0.58 − 0.17)
    assert dist.mass(*dist.interval50()) == pytest.approx(0.73)
    assert dist.cdf(0.1) == pytest.approx(0.1)
    assert dist.cdf(0.2) == pytest.approx(0.42)
    assert dist.cdf(0.3) == pytest.approx(0.83)


def test_discrete_open_tails():
    # 第一档就已经过半：中位数不高于 k_1
    dist = Distribution(ks=(1.1, 1.2, 1.3), survival=(0.35, 0.2, 0.05), grid=0.1, used_last=False)
    assert dist.median() == Bound(1.1, "below")
    # 最后一档还不到一半：中位数高于 k_n
    dist = Distribution(ks=(1.1, 1.2, 1.3), survival=(0.95, 0.8, 0.6), grid=0.1, used_last=False)
    assert dist.median() == Bound(1.3, "above")
    assert dist.quantile(0.15) == Bound(1.2)
    assert dist.interval50() == (Bound(1.3), Bound(1.3, "above"))
    # 下端是 1.3：P(X ≥ 1.3) = S(1.2)
    assert dist.mass(*dist.interval50()) == pytest.approx(0.8)
    assert dist.mass(Bound(1.3, "above"), Bound(1.3, "above")) == pytest.approx(0.6)
    assert dist.mass(Bound(1.1, "below"), Bound(1.1, "below")) == pytest.approx(0.05)


def test_discrete_quantile_exactly_at_threshold():
    # cdf 恰好等于 q 时，这个阈值本身就是分位数（X ≤ k 包含 k）
    dist = Distribution(ks=(0.0, 0.1, 0.2), survival=(0.75, 0.5, 0.1), grid=0.1, used_last=False)
    assert dist.median() == Bound(0.1)
    assert dist.quantile(0.25) == Bound(0.0, "below")
    assert dist.quantile(0.2500001) == Bound(0.1)


def test_inclusive_strikes_shift_one_grid_step():
    """X ≥ k 和 X > k − 0.1 是同一件事，两种写法的阶梯应该得到同一个分布。"""
    strict = build_ladder([Strike(0.1, tight(0.8)), Strike(0.2, tight(0.45)), Strike(0.3, tight(0.1))], grid=0.1)
    inclusive = build_ladder([Strike(0.2, tight(0.8), inclusive=True), Strike(0.3, tight(0.45), inclusive=True),
                              Strike(0.4, tight(0.1), inclusive=True)], grid=0.1)
    assert inclusive.ks == strict.ks == (0.1, 0.2, 0.3)
    assert inclusive.survival == approx(strict.survival)
    assert inclusive.median() == strict.median() == Bound(0.2)
    # 开口的端点也用严格阈值表示："不高于 0.1"就是"低于 0.2"
    lowest = build_ladder([Strike(0.2, tight(0.3), inclusive=True), Strike(0.3, tight(0.1), inclusive=True)], 0.1)
    assert lowest.median() == Bound(0.1, "below")


def test_off_grid_strikes_and_float_noise():
    # X > 0.25 在 0.1 网格上就是 X > 0.2；X ≥ 0.25 就是 X ≥ 0.3，也就是 X > 0.2
    dist = build_ladder([Strike(0.25, tight(0.6)), Strike(0.1 + 0.2, tight(0.3))], grid=0.1)
    assert dist.ks == (0.2, 0.3)
    dist = build_ladder([Strike(0.25, tight(0.6), inclusive=True), Strike(0.35, tight(0.3), inclusive=True)], 0.1)
    assert dist.ks == (0.2, 0.3)
    # 负数档位
    dist = build_ladder([Strike(-0.1, tight(0.97)), Strike(0.0, tight(0.9))], grid=0.1)
    assert dist.ks == (-0.1, 0.0)


def test_same_threshold_twice_is_merged_by_weight():
    # "> 0.2"（价差 0.02）和 ">= 0.3"（价差 0.04）都是 X > 0.2
    dist = build_ladder([Strike(0.2, quote(0.49, 0.51)), Strike(0.3, quote(0.53, 0.57), inclusive=True),
                         Strike(0.3, tight(0.3)), Strike(0.4, tight(0.1))], grid=0.1)
    assert dist.ks == (0.2, 0.3, 0.4)
    w1, w2 = 1 / 0.02 ** 2, 1 / 0.04 ** 2
    assert dist.survival[0] == pytest.approx((0.50 * w1 + 0.55 * w2) / (w1 + w2))


def test_cdf_on_grid():
    dist = Distribution(ks=(0.1, 0.3, 0.4), survival=(0.9, 0.5, 0.2), grid=0.1, used_last=False)
    assert dist.cdf(0.1) == pytest.approx(0.1)
    # 缺了 0.2 这一档：线性插值
    assert dist.cdf(0.2) == pytest.approx(0.3)
    # 不在网格点上的数按不超过它的最大网格点算
    assert dist.cdf(0.35) == pytest.approx(0.5)
    assert dist.cdf(0.1 + 0.2) == pytest.approx(0.5)
    # [k_n, k_n + 0.1) 里仍然是 P(X ≤ k_n)
    assert dist.cdf(0.45) == pytest.approx(0.8)
    for outside in (0.0, 0.05, 0.5, 1.0, float("nan")):
        with pytest.raises(ValueError):
            dist.cdf(outside)


def test_gap_in_grid_skips_the_event():
    # 0.2 那一档没价格被去掉，0.1 和 0.3 之间隔了一档：P(0.1 < X ≤ 0.3) = 0.5 怎么分在 0.2 和 0.3 上不知道，
    # 中位数只能说在 (0.1, 0.3] 里。报一个假装精确的“中位数 0.3%（0.3% 这一档就占一半以上）”不如整个不放
    strikes = [Strike(0.0, tight(0.94)), Strike(0.1, tight(0.75)), Strike(0.2, quote(None, None, None)),
               Strike(0.3, tight(0.25)), Strike(0.4, tight(0.06))]
    assert build_ladder(strikes, grid=0.1) is None
    # 补上 0.2 那一档就正常
    strikes[2] = Strike(0.2, tight(0.5))
    dist = build_ladder(strikes, grid=0.1)
    assert dist.ks == (0.0, 0.1, 0.2, 0.3, 0.4) and dist.median() == Bound(0.2)
    # 两端少几档不算缺口；连续的数（非农）没有网格，阈值之间本来就插值，不受影响
    assert build_ladder(strikes[1:4], grid=0.1).ks == (0.1, 0.2, 0.3)
    assert build_ladder([Strike(0.0, tight(0.9)), Strike(100_000.0, tight(0.2))], grid=None) is not None


# ---------- 连续（非农） ----------

def test_payroll_interpolation():
    dist = Distribution(ks=(0.0, 50_000.0, 100_000.0, 150_000.0), survival=(0.95, 0.8, 0.4, 0.1),
                        grid=None, used_last=False)
    # S(50000)=0.8 ≥ 0.5 ≥ S(100000)=0.4：50000 + 50000 × (0.8 − 0.5) / (0.8 − 0.4)
    assert dist.median() == Bound(pytest.approx(87_500.0))
    q25, q75 = dist.interval50()
    assert q25 == Bound(pytest.approx(56_250.0))
    assert q75 == Bound(pytest.approx(125_000.0))
    assert dist.mass(q25, q75) == pytest.approx(0.5)
    assert dist.cdf(75_000.0) == pytest.approx(0.4)
    assert dist.cdf(150_000.0) == pytest.approx(0.9)
    for outside in (-1.0, 150_001.0):
        with pytest.raises(ValueError):
            dist.cdf(outside)


def test_continuous_open_tails_and_exact_hit():
    dist = Distribution(ks=(-20_000.0, 30_000.0), survival=(0.4, 0.2), grid=None, used_last=False)
    assert dist.median() == Bound(-20_000.0, "below")
    assert dist.quantile(0.7) == Bound(pytest.approx(5_000.0))
    assert dist.quantile(0.9) == Bound(30_000.0, "above")
    assert dist.mass(Bound(-20_000.0, "below"), Bound(30_000.0, "above")) == pytest.approx(1.0)
    dist = Distribution(ks=(10_000.0, 60_000.0), survival=(0.5, 0.3), grid=None, used_last=False)
    assert dist.median() == Bound(10_000.0)
    # 连续的数在端点上没有点概率：[k_1, k_2] 的概率是 F(k_2) − F(k_1)
    assert dist.mass(Bound(10_000.0), Bound(60_000.0)) == pytest.approx(0.2)


def test_flat_segment_takes_first_point():
    dist = Distribution(ks=(0.0, 1000.0, 2000.0, 3000.0), survival=(0.9, 0.5, 0.5, 0.1), grid=None, used_last=False)
    assert dist.median() == Bound(1000.0)


# ---------- build_ladder ----------

def test_needs_two_usable_strikes():
    assert build_ladder([], grid=0.1) is None
    assert build_ladder([Strike(0.2, tight(0.5))], grid=0.1) is None
    # 没有任何可用价格的合约被丢掉
    assert build_ladder([Strike(0.2, tight(0.5)), Strike(0.3, quote(0.1, 0.5))], grid=0.1) is None
    assert build_ladder([Strike(0.2, tight(0.5)), Strike(float("nan"), tight(0.3))], grid=0.1) is None
    with pytest.raises(ValueError):
        build_ladder([Strike(0.2, tight(0.5)), Strike(0.3, tight(0.3))], grid=0.0)


def test_last_trade_used_and_weighted_as_wide_spread():
    # 0.2 那档价差 0.40 太宽，用成交价 0.30（按价差 0.10 算权重 100）；0.3 那档 0.50（价差 0.02，权重 2500）
    dist = build_ladder([Strike(0.2, quote(0.10, 0.50, 0.30)), Strike(0.3, quote(0.49, 0.51))], grid=0.1)
    assert dist.used_last is True
    pooled = (0.30 * 100 + 0.50 * 2500) / 2600
    assert dist.survival == approx((pooled, pooled))
    # 价差上限放宽后就用中间价
    dist = build_ladder([Strike(0.2, quote(0.10, 0.50, 0.30)), Strike(0.3, quote(0.09, 0.11))],
                        grid=0.1, max_spread=0.5)
    assert dist.used_last is False
    assert dist.survival == approx((0.30, 0.10))


def test_fill_one_sided_only_next_to_zero_or_one():
    # 没有买单、卖价贴着 0：买价当作 0；没有卖单、买价贴着 1：卖价当作 1；成交价原样留着
    assert fill_one_sided(quote(None, 0.02, 0.4)) == quote(0.0, 0.02, 0.4)
    assert fill_one_sided(quote(0.97, None, 0.5)) == quote(0.97, 1.0, 0.5)
    assert fill_one_sided(quote(None, 0.10)) == quote(0.0, 0.10)  # 正好在价差上限上也补
    # 另一边离 0 或 1 还远、两边都没有、两边都有：都不动
    for q in (quote(None, 0.3, 0.2), quote(0.6, None, 0.7), quote(None, None, 0.3), quote(0.4, 0.42)):
        assert fill_one_sided(q) is q
    assert fill_one_sided(quote(None, 0.2), max_spread=0.2) == quote(0.0, 0.2)


def test_one_sided_tails_priced_from_the_book_not_stale_trades():
    # 最低一档只有买单、最高一档只有卖单，成交价都是很久以前的（0.83、0.11）
    low, high = quote(0.98, None, 0.83), quote(None, 0.02, 0.11)
    middle = [Strike(0.1, tight(0.7)), Strike(0.2, tight(0.4))]
    raw = build_ladder([Strike(0.0, low), *middle, Strike(0.3, high)], grid=0.1)
    assert raw.used_last is True  # 不补的话两端都退回成交价
    dist = build_ladder([Strike(0.0, fill_one_sided(low)), *middle, Strike(0.3, fill_one_sided(high))], grid=0.1)
    assert dist.used_last is False
    assert dist.survival == approx((0.99, 0.7, 0.4, 0.01))


def test_min_spread_floor_on_weights():
    # 价差为 0 的报价权重按 0.01 封顶（10000），不会是无穷大
    dist = build_ladder([Strike(1000.0, quote(0.40, 0.40)), Strike(2000.0, quote(0.39, 0.47))], grid=None)
    w1, w2 = 1 / 0.01 ** 2, 1 / 0.08 ** 2
    pooled = (0.40 * w1 + 0.43 * w2) / (w1 + w2)
    assert dist.survival == approx((pooled, pooled))


def test_stale_strike_dropped():
    # 更高阈值 0.4 的买价 0.62 高于 0.3 的卖价 0.56：0.4 的报价过期了，去掉
    strikes = [Strike(0.2, tight(0.8)), Strike(0.3, quote(0.54, 0.56)), Strike(0.4, quote(0.62, 0.64))]
    dist = build_ladder(strikes, grid=0.1)
    assert dist.ks == (0.2, 0.3)
    assert dist.survival == approx((0.8, 0.55))
    # 去掉的是中间一档时网格上缺了一档，整个阶梯不用（见 test_gap_in_grid_skips_the_event）
    strikes.append(Strike(0.5, tight(0.1)))
    assert build_ladder(strikes, grid=0.1) is None
    # 只比较卖价和买价：买卖价交叠但没超过时不算过期，交给 PAVA
    strikes[2] = Strike(0.4, quote(0.55, 0.58))
    dist = build_ladder(strikes, grid=0.1)
    assert dist.ks == (0.2, 0.3, 0.4, 0.5)
    assert all(b <= a for a, b in zip(dist.survival, dist.survival[1:]))


def test_build_ladder_sorts_and_clips():
    strikes = [Strike(0.3, quote(None, None, 1.2)), Strike(0.1, tight(0.6)), Strike(0.2, quote(None, None, -0.1))]
    dist = build_ladder(strikes, grid=0.1)
    assert dist.ks == (0.1, 0.2, 0.3)
    assert all(0 <= s <= 1 for s in dist.survival)
    assert all(b <= a for a, b in zip(dist.survival, dist.survival[1:]))


def test_random_ladders_are_monotone_distributions():
    rng = random.Random(7)
    for _ in range(300):
        grid = rng.choice([0.1, None])
        n = rng.randint(2, 9)
        ks = [round(i * 0.1, 10) for i in range(-3, -3 + n)] if grid else [i * 25_000.0 for i in range(n)]
        strikes = []
        for k in ks:
            p = rng.random()
            half = rng.choice([0.0, 0.005, 0.01, 0.03, 0.2])
            last = rng.choice([None, rng.random()])
            bid, ask = max(0.0, p - half), min(1.0, p + half)
            strikes.append(Strike(k, Quote(bid, ask, last), inclusive=bool(grid) and rng.random() < 0.3))
        rng.shuffle(strikes)
        dist = build_ladder(strikes, grid=grid)
        if dist is None:
            continue
        assert list(dist.ks) == sorted(set(dist.ks))
        assert all(0 <= s <= 1 for s in dist.survival)
        assert all(b <= a for a, b in zip(dist.survival, dist.survival[1:]))


# ---------- 分位数和概率的性质 ----------

def test_discrete_masses_sum_to_one():
    rng = random.Random(11)
    for _ in range(200):
        dist = random_distribution(rng, 0.1)
        ks = dist.ks
        parts = [dist.mass(Bound(ks[0], "below"), Bound(ks[0], "below"))]
        parts += [dist.mass(Bound(k), Bound(k)) for k in ks[1:]]
        parts.append(dist.mass(Bound(ks[-1], "above"), Bound(ks[-1], "above")))
        assert all(p >= 0 for p in parts)
        assert sum(parts) == pytest.approx(1.0)
        assert dist.mass(Bound(ks[0], "below"), Bound(ks[-1], "above")) == pytest.approx(1.0)


def test_discrete_quantiles_are_thresholds_and_monotone():
    rng = random.Random(13)
    for _ in range(200):
        dist = random_distribution(rng, 0.1)
        previous = None
        for q in (0.05, 0.25, 0.5, 0.75, 0.95):
            bound = dist.quantile(q)
            assert bound.value in dist.ks
            if bound.open_side is None:
                i = dist.ks.index(bound.value)
                assert dist.cdf(bound.value) >= q - 1e-9 > dist.cdf(dist.ks[i - 1])
            elif bound.open_side == "below":
                assert bound.value == dist.ks[0] and dist.cdf(dist.ks[0]) >= q - 1e-9
            else:
                assert bound.value == dist.ks[-1] and dist.cdf(dist.ks[-1]) < q
            rank = (dist.ks.index(bound.value), {"below": -1, None: 0, "above": 1}[bound.open_side])
            assert previous is None or rank >= previous
            previous = rank


def test_continuous_quantiles_stay_inside_brackets():
    rng = random.Random(17)
    for _ in range(200):
        dist = random_distribution(rng, None)
        ks, survival = dist.ks, dist.survival
        for q in (0.1, 0.25, 0.5, 0.75, 0.9):
            bound = dist.quantile(q)
            if bound.open_side == "below":
                assert bound.value == ks[0] and 1 - survival[0] > q
            elif bound.open_side == "above":
                assert bound.value == ks[-1] and 1 - survival[-1] < q
            else:
                assert ks[0] <= bound.value <= ks[-1]
                i = max(j for j, k in enumerate(ks) if k <= bound.value)
                if bound.value < ks[-1]:
                    # 落在 [k_i, k_{i+1}] 里，而且这一段的 S 跨过了 1 − q
                    assert survival[i] >= 1 - q - 1e-9 >= survival[i + 1] - 2e-9
                assert dist.cdf(bound.value) == pytest.approx(q, abs=1e-9)
        q25, q75 = dist.interval50()
        if q25.open_side is None and q75.open_side is None:
            assert dist.mass(q25, q75) == pytest.approx(0.5, abs=1e-9)


# ---------- Distribution 直接构造 ----------

def test_distribution_from_stored_lists():
    dist = Distribution(ks=[0.1, 0.2], survival=[0.7, 0.3], grid=0.1, used_last=True)
    assert dist.ks == (0.1, 0.2) and dist.survival == (0.7, 0.3)
    # 网格点上的浮点误差被规整掉
    dist = Distribution(ks=(0.1 + 0.2, 0.4), survival=(0.5, 0.2), grid=0.1, used_last=False)
    assert dist.ks == (0.3, 0.4)


@pytest.mark.parametrize("ks, survival, grid", [
    ((), (), None),
    ((0.1, 0.2), (0.5,), None),
    ((0.2, 0.1), (0.5, 0.4), None),
    ((0.1, 0.1), (0.5, 0.4), None),
    ((0.1, 0.2), (0.4, 0.5), None),
    ((0.1, 0.2), (1.2, 0.5), None),
    ((0.1, 0.25), (0.5, 0.4), 0.1),
    ((0.1, 0.2), (0.5, 0.4), -0.1),
    ((0.1, float("inf")), (0.5, 0.4), None),
])
def test_distribution_rejects_bad_input(ks, survival, grid):
    with pytest.raises(ValueError):
        Distribution(ks=ks, survival=survival, grid=grid, used_last=False)


def test_bound_and_quantile_validation():
    with pytest.raises(ValueError):
        Bound(0.3, "left")
    dist = Distribution(ks=(0.1, 0.2), survival=(0.6, 0.3), grid=0.1, used_last=False)
    for q in (0.0, 1.0, -0.5):
        with pytest.raises(ValueError):
            dist.quantile(q)


# ---------- 互斥结果 ----------

def test_normalize_buckets():
    quotes = [quote(0.08, 0.12), quote(0.58, 0.62), quote(0.23, 0.27)]
    assert normalize_buckets(quotes) == pytest.approx([0.10 / 0.95, 0.60 / 0.95, 0.25 / 0.95])
    assert sum(normalize_buckets(quotes)) == pytest.approx(1.0)
    # 中间价总和偏离 1 恰好 0.10 还能用（买价总和恰好 1 也不算交叉）
    assert normalize_buckets([quote(0.50, 0.60), quote(0.50, 0.60)]) == pytest.approx([0.5, 0.5])


@pytest.mark.parametrize("quotes", [
    [],
    [quote(0.97, 0.99)],
    # 中间价总和 1.15
    [quote(0.39, 0.41), quote(0.39, 0.41), quote(0.34, 0.36)],
    # 中间价总和 0.80
    [quote(0.39, 0.41), quote(0.39, 0.41)],
    # 卖价总和 0.98 < 1
    [quote(0.47, 0.49), quote(0.47, 0.49)],
    # 买价总和 1.02 > 1
    [quote(0.51, 0.53), quote(0.51, 0.53)],
    # 有一个结果没有可用价格
    [quote(0.49, 0.51), quote(0.10, 0.60), quote(None, None, None)],
])
def test_normalize_buckets_rejects(quotes):
    assert normalize_buckets(quotes) is None


def test_normalize_buckets_checks_only_complete_sides():
    # 有一个结果没有卖价：不检查卖价总和；这里用成交价补上，中间价总和 0.96
    quotes = [quote(0.47, 0.49), quote(0.47, None, 0.48)]
    assert normalize_buckets(quotes) == pytest.approx([0.5, 0.5])
    # 价差太宽时用成交价，放宽价差上限后改用中间价
    quotes = [quote(0.20, 0.50, 0.30), quote(0.65, 0.67)]
    assert normalize_buckets(quotes) == pytest.approx([0.30 / 0.96, 0.66 / 0.96])
    assert normalize_buckets(quotes, max_spread=0.5) == pytest.approx([0.35 / 1.01, 0.66 / 1.01])
