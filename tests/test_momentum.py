import math
import random

import pytest

from radar.momentum import Past, fama_macbeth, momentum_preds
from radar.train import Sample

pytest.importorskip("numpy")


def test_momentum_follows_base_day_return():
    ss = [Sample(str(i), "d", {}) for i in range(5)]
    past = [Past(x, None) for x in (0.03, -0.02, 0.0, None, 0.01)]
    assert momentum_preds(ss, past, "zero") == ["up", "down", "flat", "flat", "up"]
    assert momentum_preds(ss, past, "median") == ["up", "down", "down", "flat", "up"]    # 中位數 0.005


def test_fama_macbeth_separates_two_correlated_drivers():
    """報酬只由 r0 決定；情緒和 r0 相關。單獨放情緒看起來有效，一起放之後情緒的係數要接近 0。"""
    rng = random.Random(1)
    both, alone = [], []
    for d in range(40):
        for _ in range(30):
            r0 = rng.gauss(0, 1)
            s = 0.7 * r0 + rng.gauss(0, 0.7)
            y = 0.01 * r0 + rng.gauss(0, 0.005)
            both.append((f"d{d:02}", y, [s, r0]))
            alone.append((f"d{d:02}", y, [s]))
    a = fama_macbeth(alone)[0]
    s, r0 = fama_macbeth(both)
    assert a["n"] == 40 and a["mean"] > 0.004 and a["p"] < 0.01
    assert abs(s["mean"]) < 0.001 and s["p"] > 0.05
    assert math.isclose(r0["mean"], 0.01, abs_tol=0.002) and r0["p"] < 0.01


def test_fama_macbeth_skips_thin_or_constant_days():
    rows = [("d1", 0.01, [1.0])] * 3 + [("d2", 0.01, [1.0])] * 12
    assert fama_macbeth(rows) is None
