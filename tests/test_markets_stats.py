from __future__ import annotations

import itertools
import math
import random

import pytest

from digest.markets.ladder import Distribution, Strike, build_ladder
from digest.markets.stats import (
    brier,
    bss,
    calibration_in_the_large,
    corp,
    pav_increasing,
    pb_range,
    pit,
    poisson_binomial,
    rps,
)
from digest.markets.types import Quote

# 这里的数都是编的，只有设计文档 12 节点名的"12 件各 91%"例子用了文档里写的数

CPI_LIKE = Distribution(ks=(0.1, 0.2, 0.3), survival=(0.9, 0.6, 0.2), grid=0.1, used_last=False)  # F = 0.1, 0.4, 0.8
JOBS_LIKE = Distribution(ks=(0.0, 50_000.0, 100_000.0, 150_000.0), survival=(0.95, 0.8, 0.4, 0.1), grid=None,
                         used_last=False)


def brute_force_pmf(ps):
    n = len(ps)
    pmf = [0.0] * (n + 1)
    for outcome in itertools.product((0, 1), repeat=n):
        weight = math.prod(p if o else 1 - p for p, o in zip(ps, outcome))
        pmf[sum(outcome)] += weight
    return pmf


# ---------- 泊松二项 ----------

def test_poisson_binomial_design_example():
    """设计文档的例子：12 件、每件 91%，应发生 10.92 件，正常范围 9 到 12。"""
    ps = [0.91] * 12
    pmf = poisson_binomial(ps)
    assert len(pmf) == 13
    assert sum(pmf) == pytest.approx(1.0)
    assert sum(k * p for k, p in enumerate(pmf)) == pytest.approx(10.92)
    # 12 件全中只有约 32%
    assert pmf[12] == pytest.approx(0.91 ** 12)
    assert round(pmf[12], 2) == 0.32
    assert pb_range(ps) == (9, 12)
    # 第 8 件以下的累计概率不到 5%，到第 9 件才超过
    assert sum(pmf[:9]) < 0.05 <= sum(pmf[:10])


def test_poisson_binomial_matches_enumeration():
    rng = random.Random(3)
    for _ in range(50):
        ps = [rng.choice([0.0, 1.0, rng.random()]) for _ in range(rng.randint(0, 8))]
        pmf = poisson_binomial(ps)
        assert pmf == pytest.approx(brute_force_pmf(ps), abs=1e-12)
        assert sum(k * p for k, p in enumerate(pmf)) == pytest.approx(sum(ps), abs=1e-12)


def test_poisson_binomial_edges():
    assert poisson_binomial([]) == [1.0]
    assert poisson_binomial([1.0, 1.0, 0.0]) == [0.0, 0.0, 1.0, 0.0]
    assert pb_range([]) == (0, 0)
    assert pb_range([1.0, 1.0, 1.0]) == (3, 3)
    assert pb_range([0.5, 0.5], lo=0.0, hi=1.0) == (0, 2)
    # 累计概率恰好等于下限时就取这个件数
    assert pb_range([0.5, 0.5], lo=0.25, hi=0.75) == (0, 1)
    for bad in ([1.5], [-0.1], [float("nan")]):
        with pytest.raises(ValueError):
            poisson_binomial(bad)
    with pytest.raises(ValueError):
        pb_range([0.5], lo=0.9, hi=0.1)


# ---------- RPS ----------

def test_rps_on_grid():
    # 公布 0.2：>0.1 发生，>0.2 和 >0.3 没发生
    assert rps(CPI_LIKE, 0.2) == pytest.approx(0.1 ** 2 + 0.6 ** 2 + 0.2 ** 2)
    assert rps(CPI_LIKE, 0.4) == pytest.approx(0.1 ** 2 + 0.4 ** 2 + 0.8 ** 2)
    assert rps(CPI_LIKE, -0.1) == pytest.approx(0.9 ** 2 + 0.6 ** 2 + 0.2 ** 2)
    # 浮点误差不能把"等于 0.3"当成"大于 0.3"
    assert rps(CPI_LIKE, 0.1 + 0.2) == pytest.approx(rps(CPI_LIKE, 0.3))
    assert rps(CPI_LIKE, 0.3) == pytest.approx(0.1 ** 2 + 0.4 ** 2 + 0.2 ** 2)


def test_rps_continuous_and_perfect_forecast():
    assert rps(JOBS_LIKE, 120_000.0) == pytest.approx(0.05 ** 2 + 0.2 ** 2 + 0.6 ** 2 + 0.1 ** 2)
    sure = Distribution(ks=(0.0, 1.0), survival=(1.0, 0.0), grid=None, used_last=False)
    assert rps(sure, 0.5) == 0.0
    with pytest.raises(ValueError):
        rps(CPI_LIKE, float("nan"))


def test_rps_uses_strict_thresholds_of_inclusive_ladders():
    # "≥ 0.2"、"≥ 0.3" 的阶梯：公布 0.2 时第一档发生，第二档没发生
    dist = build_ladder([Strike(0.2, Quote(0.69, 0.71, None), inclusive=True),
                         Strike(0.3, Quote(0.29, 0.31, None), inclusive=True)], grid=0.1)
    assert rps(dist, 0.2) == pytest.approx(0.3 ** 2 + 0.3 ** 2)


# ---------- PIT ----------

def test_pit_on_grid():
    # 公布 0.2 那一格是 (0.1, 0.2]：F(0.1) + u × (F(0.2) − F(0.1))
    assert pit(CPI_LIKE, 0.2) == pytest.approx(0.1 + 0.5 * 0.3)
    assert pit(CPI_LIKE, 0.2, u=0.0) == pytest.approx(0.1)
    assert pit(CPI_LIKE, 0.2, u=1.0) == pytest.approx(0.4)
    assert pit(CPI_LIKE, 0.3) == pytest.approx(0.4 + 0.5 * 0.4)
    # 两端之外整段算一格
    assert pit(CPI_LIKE, 0.1) == pytest.approx(0.5 * 0.1)
    assert pit(CPI_LIKE, -0.3) == pytest.approx(0.5 * 0.1)
    assert pit(CPI_LIKE, 0.4) == pytest.approx(0.8 + 0.5 * 0.2)
    assert pit(CPI_LIKE, 0.9, u=1.0) == pytest.approx(1.0)
    assert pit(CPI_LIKE, 0.1 + 0.2) == pytest.approx(pit(CPI_LIKE, 0.3))
    for bad in (-0.1, 1.1):
        with pytest.raises(ValueError):
            pit(CPI_LIKE, 0.2, u=bad)


def test_pit_continuous():
    assert pit(JOBS_LIKE, 75_000.0) == pytest.approx(0.4)
    assert pit(JOBS_LIKE, 150_000.0) == pytest.approx(0.9)
    assert pit(JOBS_LIKE, 0.0) == pytest.approx(0.05)
    assert pit(JOBS_LIKE, -30_000.0) == pytest.approx(0.5 * 0.05)
    assert pit(JOBS_LIKE, 210_000.0) == pytest.approx(0.9 + 0.5 * 0.1)
    # u 只影响两端之外
    assert pit(JOBS_LIKE, 75_000.0, u=0.9) == pytest.approx(0.4)


def test_randomized_pit_is_uniform_under_the_forecast():
    """X 按分布本身抽取、u 均匀时，随机化 PIT 在 0 到 1 上均匀：均值 1/2，平方的均值 1/3。"""
    rng = random.Random(5)
    for _ in range(100):
        n = rng.randint(1, 7)
        start = rng.randint(-3, 3)
        ks = tuple(round((start + i) * 0.1, 10) for i in range(n))
        dist = Distribution(ks=ks, survival=tuple(sorted((rng.random() for _ in ks), reverse=True)),
                            grid=0.1, used_last=False)
        cdf = [0.0] + [1 - s for s in dist.survival] + [1.0]
        # 每一格挑一个代表值：第一格 k_1，中间各格 k_i，最后一格 k_n + 0.1
        values = [ks[0]] + list(ks[1:]) + [round(ks[-1] + 0.1, 10)]
        mean = second = 0.0
        for i, x in enumerate(values):
            mass = cdf[i + 1] - cdf[i]
            a, b = pit(dist, x, u=0.0), pit(dist, x, u=1.0)
            assert (a, b) == pytest.approx((cdf[i], cdf[i + 1]))
            mean += mass * pit(dist, x)
            second += mass * (a * a + a * b + b * b) / 3
        assert mean == pytest.approx(0.5)
        assert second == pytest.approx(1 / 3)


def test_pit_is_monotone_in_outcome():
    previous = -1.0
    for x in range(-50_000, 200_001, 5_000):
        value = pit(JOBS_LIKE, float(x))
        assert 0 <= value <= 1 and value >= previous
        previous = value


# ---------- Brier / BSS / 整体偏差 ----------

def test_brier_bss_and_calibration_in_the_large():
    ps, ys = [0.9, 0.2], [1, 0]
    assert brier(ps, ys) == pytest.approx((0.01 + 0.04) / 2)
    assert brier([0.7], [True]) == pytest.approx(0.09)
    # 对基准率 0.5：BS_ref = 0.25
    assert bss(ps, ys, 0.5) == pytest.approx(1 - 0.025 / 0.25)
    assert bss(ps, ys, [0.6, 0.4]) == pytest.approx(1 - 0.025 / 0.16)
    # 参照完全正确时没有定义
    assert bss(ps, ys, [1.0, 0.0]) is None
    assert calibration_in_the_large([0.9, 0.8, 0.7], [1, 0, 1]) == pytest.approx(0.8 - 2 / 3)
    assert calibration_in_the_large([0.25, 0.75], [0, 1]) == pytest.approx(0.0)


@pytest.mark.parametrize("ps, ys", [([], []), ([0.5], []), ([1.2], [1]), ([0.5], [2]), ([float("nan")], [0])])
def test_binary_scores_reject_bad_input(ps, ys):
    for fn in (brier, corp, calibration_in_the_large):
        with pytest.raises(ValueError):
            fn(ps, ys)


# ---------- PAV 和 CORP ----------

def test_pav_increasing_pools_and_keeps_order():
    assert pav_increasing([0.1, 0.5, 0.9], [0, 1, 0]) == pytest.approx([0.0, 0.5, 0.5])
    # 输入顺序打乱，x 相同的点拟合值相同
    assert pav_increasing([0.3, 0.1, 0.2, 0.2], [0, 1, 0, 1]) == pytest.approx([0.5, 0.5, 0.5, 0.5])
    assert pav_increasing([0.9, 0.1, 0.5], [1, 0, 0]) == pytest.approx([1.0, 0.0, 0.0])
    assert pav_increasing([], []) == []
    with pytest.raises(ValueError):
        pav_increasing([0.1], [])


def test_pav_increasing_properties():
    rng = random.Random(9)
    for _ in range(200):
        n = rng.randint(1, 12)
        xs = [rng.choice([0.05, 0.2, 0.35, 0.5, 0.65, 0.8, 0.95]) for _ in range(n)]
        ys = [float(rng.random() < x) for x in xs]
        fitted = pav_increasing(xs, ys)
        pairs = sorted(zip(xs, fitted))
        assert all(b[1] >= a[1] - 1e-12 for a, b in zip(pairs, pairs[1:]))
        for x in set(xs):
            assert len({round(f, 12) for xi, f in zip(xs, fitted) if xi == x}) == 1
        assert sum(fitted) == pytest.approx(sum(ys))
        assert all(0 <= f <= 1 for f in fitted)


def test_corp_on_perfectly_calibrated_toy_set():
    # 每个预测值上的实际发生比例正好等于预测值
    ps, ys = [], []
    for p, hits in ((0.1, 1), (0.3, 3), (0.6, 6), (0.9, 9)):
        ps += [p] * 10
        ys += [1] * hits + [0] * (10 - hits)
    result = corp(ps, ys)
    assert result["n"] == 40
    assert result["mcb"] == pytest.approx(0.0, abs=1e-12)
    ybar = 19 / 40
    assert result["unc"] == pytest.approx(ybar * (1 - ybar))
    assert result["bs"] == pytest.approx(brier(ps, ys))
    assert result["bs"] == pytest.approx(result["mcb"] - result["dsc"] + result["unc"])
    assert result["dsc"] > 0


def test_corp_decomposition_with_miscalibration():
    # 一律 90%，实际一半：偏差 0.4² = 0.16，没有区分度
    result = corp([0.9] * 4, [1, 0, 1, 0])
    assert result == pytest.approx({"n": 4, "mcb": 0.16, "dsc": 0.0, "unc": 0.25, "bs": 0.41})
    # 过度自信：PAV 把中间两个合并成 0.5
    result = corp([0.1, 0.4, 0.6, 0.9], [0, 1, 0, 1])
    assert result["mcb"] == pytest.approx(0.185 - 0.125)
    assert result["dsc"] == pytest.approx(0.25 - 0.125)
    assert result["bs"] == pytest.approx(0.185)


def test_corp_identity_on_random_data():
    rng = random.Random(21)
    for _ in range(100):
        n = rng.randint(1, 30)
        ps = [round(rng.random(), 2) for _ in range(n)]
        ys = [int(rng.random() < 0.5) for _ in range(n)]
        result = corp(ps, ys)
        assert result["mcb"] >= 0 and result["dsc"] >= 0
        assert result["bs"] == pytest.approx(result["mcb"] - result["dsc"] + result["unc"], abs=1e-12)
