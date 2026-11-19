"""面板迴歸：日期固定效果、Driscoll–Kraay 標準誤、規模分組、回補涵蓋範圍。"""
import random

import numpy as np
import pytest

from radar import db, panel


def test_date_fixed_effect_removes_common_daily_shocks():
    rng = random.Random(1)
    y, x, dates = [], [], []
    for d in range(60):
        shock = rng.gauss(0, 5)              # 當天所有股票一起動，而且和 x 的平均相關
        for _ in range(20):
            xi = rng.gauss(shock, 1)
            y.append(0.5 * xi + shock * 3 + rng.gauss(0, 0.1))
            x.append([xi])
            dates.append(f"d{d:03d}")
    res = panel.fe_ols(y, x, dates, absorb_date=True)
    assert res["beta"][0] == pytest.approx(0.5, abs=0.02)
    naive = panel.fe_ols(y, x, dates, absorb_date=False)
    assert abs(naive["beta"][0] - 0.5) > 1          # 不吸收日期就被共同衝擊帶偏


def test_lag0_equals_date_clustered_se():
    rng = np.random.default_rng(2)
    n_days, per = 40, 15
    dates = [f"d{d:02d}" for d in range(n_days) for _ in range(per)]
    x = rng.normal(size=(n_days * per, 2))
    y = x @ np.array([0.3, -0.2]) + rng.normal(size=n_days * per)
    res = panel.fe_ols(y, x, dates, absorb_date=True, lags=0)
    # 手算：按日期群聚的 sandwich
    di = np.repeat(np.arange(n_days), per)
    xd = x - np.array([x[di == g].mean(axis=0) for g in range(n_days)])[di]
    yd = y - np.array([y[di == g].mean() for g in range(n_days)])[di]
    inv = np.linalg.inv(xd.T @ xd)
    b = inv @ xd.T @ yd
    e = yd - xd @ b
    meat = sum(np.outer(xd[di == g].T @ e[di == g], xd[di == g].T @ e[di == g]) for g in range(n_days))
    v = inv @ meat @ inv * n_days / (n_days - 1)
    assert res["beta"] == pytest.approx(list(b))
    assert res["se"] == pytest.approx(list(np.sqrt(np.diag(v))))


def test_overlap_lags_widen_se_when_shocks_persist():
    rng = np.random.default_rng(3)
    n_days, per = 120, 10
    dates = [f"d{d:03d}" for d in range(n_days) for _ in range(per)]
    common = np.repeat(rng.normal(size=n_days), per)       # x 每天的橫斷面有個共同斜率衝擊，並且逐日持續
    persist = np.convolve(np.repeat(rng.normal(size=n_days), 1), np.ones(5), "same")
    x = rng.normal(size=n_days * per)
    y = x * (0.1 + np.repeat(persist, per)) + common + rng.normal(size=n_days * per)
    se0 = panel.fe_ols(y, x[:, None], dates, lags=0)["se"][0]
    se4 = panel.fe_ols(y, x[:, None], dates, lags=4)["se"][0]
    assert se4 > se0


def test_all_zero_column_after_absorbing_is_dropped_not_crashing():
    dates = ["a", "a", "b", "b", "c", "c"]
    x = [[1.0, 5.0], [2.0, 5.0], [3.0, 5.0], [1.0, 5.0], [2.0, 5.0], [4.0, 5.0]]   # 第二欄是常數
    res = panel.fe_ols([1, 2, 3, 1, 2, 4], x, dates)
    assert res["beta"][0] == pytest.approx(1.0)
    assert np.isnan(res["beta"][1])


def test_groups_by_market_cap_rank():
    assert panel.group_of(1).startswith("大型")
    assert panel.group_of(50).startswith("大型")
    assert panel.group_of(51).startswith("中型")
    assert panel.group_of(100).startswith("中型")
    assert panel.group_of(101) is None


def test_covered_codes_needs_complete_backfill():
    c = db.connect(":memory:")
    rows = [("1111", f"2026-01-{d:02d}", 1, "now") for d in range(1, 31)]
    rows += [("2222", f"2026-01-{d:02d}", 1, "now") for d in range(1, 28)]    # 27/30 = 90%
    rows += [("3333", f"2026-01-{d:02d}", 1, "now") for d in range(1, 11)]    # 只查了 10 天
    c.executemany("INSERT INTO backfill_days (code, date, found, fetched_at) VALUES (?, ?, ?, ?)", rows)
    assert panel.covered_codes(c) == {"1111", "2222"}
