import random

import pytest

from radar.train import Sample
from radar.tune import fit_weights, score, split

pytest.importorskip("sklearn")


def _samples(n_days=60, per_day=40, seed=3):
    """三個詞：strong 的效果是 weak 的五倍；wrong 在字典裡是正面詞，實際上之後會跌。"""
    rng = random.Random(seed)
    out = []
    for d in range(n_days):
        for i in range(per_day):
            f = {"strong": float(rng.randint(0, 2)), "weak": float(rng.randint(0, 2)), "wrong": float(rng.randint(0, 2)),
                 "rare": 1.0 if rng.random() < 0.002 else 0.0}
            ex = 0.010 * f["strong"] + 0.002 * f["weak"] - 0.006 * f["wrong"] + rng.gauss(0, 0.01)
            out.append(Sample(str(i), f"2026-{1 + d // 28:02}-{1 + d % 28:02}", {k: v for k, v in f.items() if v}, ex=ex))
    return out


def test_split_is_by_time_and_leaves_a_gap():
    train, test = split(_samples(), horizon=5)
    days = sorted({s.day for s in _samples()})
    assert max(s.day for s in train) < min(s.day for s in test)
    gap = days.index(min(s.day for s in test)) - days.index(max(s.day for s in train))
    assert gap >= 5                                   # 訓練期最後幾天的報酬期間會跨進測試期，要空下來
    assert len({s.day for s in test}) == round(len(days) * 0.35)


def test_three_adjustments_recover_strength_and_direction():
    train, _ = split(_samples(), horizon=1)
    f = fit_weights(train, min_count=30)
    assert "rare" not in f["terms"]                   # 太少見的詞不估計，維持權重 1
    assert f["weights"]["只刪詞"] == {"wrong": 0.0}
    pos, free = f["weights"]["調強度、不改方向"], f["weights"]["完全由資料決定"]
    assert pos["strong"] > 3 * pos["weak"] > 0 and pos["wrong"] == 0
    assert free["strong"] > 3 * free["weak"] > 0 and free["wrong"] < 0
    s = Sample("1", "d", {"strong": 1.0, "wrong": 2.0, "rare": 1.0})
    assert score(s, {}) == 4.0                        # 原本：每個詞 ±1
    assert score(s, f["weights"]["只刪詞"]) == 2.0
