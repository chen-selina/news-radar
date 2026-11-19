"""消融實驗：市場特徵不偷看進場日、補值與標準化只用訓練期、purging、A 和 B 在同一批測試樣本上比較。"""
import math
import random

import pytest

from radar import ablation, db
from radar.train import Sample

DAYS = [f"2026-09-{d:02d}" for d in range(1, 29)]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.executemany("INSERT INTO trading_days (date) VALUES (?)", [(d,) for d in DAYS])
    px = [(d, "TAIEX", 100.0 * 1.01 ** i) for i, d in enumerate(DAYS)]
    px += [(d, "1111", 100.0 * 1.02 ** i) for i, d in enumerate(DAYS)]
    c.executemany("INSERT INTO prices (date, code, close) VALUES (?, ?, ?)", px)
    c.execute("INSERT INTO stocks (code, name, market, industry, shares, updated_at) "
              "VALUES ('1111', '甲', '上市', '半導體業', 1000, 'now')")
    c.commit()
    return c


def test_market_features_stop_at_the_day_before_entry(conn):
    s = Sample("1111", DAYS[25], {})
    (f, ind), = ablation.market_features(conn, [s])
    assert ind == "半導體業"
    assert f["r1"] == pytest.approx(0.02 - 0.01)
    assert f["r5"] == pytest.approx(1.02 ** 5 - 1.01 ** 5)
    assert f["r20"] == pytest.approx(1.02 ** 20 - 1.01 ** 20)
    assert f["vol20"] == pytest.approx(0.0, abs=1e-12)
    assert f["size"] == pytest.approx(math.log(1000 * 100 * 1.02 ** 24))      # 前一日收盤
    # 進場日（含）之後的價格怎麼變，特徵都不能變
    conn.execute("UPDATE prices SET close = close * 3 WHERE code = '1111' AND date >= ?", (DAYS[25],))
    (g, _), = ablation.market_features(conn, [s])
    assert g == f


def test_short_history_gives_missing_not_wrong_values(conn):
    (f, _), = ablation.market_features(conn, [Sample("1111", DAYS[3], {})])
    assert f["r1"] is not None and f["r20"] is None and f["vol20"] is None
    (f, ind), = ablation.market_features(conn, [Sample("9999", DAYS[25], {})])
    assert all(v is None for k, v in f.items() if k != "mkt5") and f["mkt5"] is not None and ind == ""   # 大盤特徵照樣有


def R(day, ex, x, ind="", end=None):
    s = Sample("1", day, {}, 1, ex, None, "up" if ex > 0.01 else "down" if ex < -0.01 else "flat", end or day)
    return ablation.Row(s, x, ind)


def test_design_learns_only_from_training_rows():
    tr = [R("d1", 0.0, {"a": 1.0}, "甲"), R("d1", 0.0, {"a": 3.0}, "甲"), R("d1", 0.0, {"a": None}, "乙")]
    d = ablation.Design(("a",), True).fit(tr)
    assert d.inds == ["乙", "甲"] and d.med[0] == 2.0                          # 中位數只看訓練期
    x = d.transform([R("d2", 0.0, {"a": None}, "丙"), R("d2", 0.0, {"a": 100.0}, "甲")])
    assert x[0][0] == pytest.approx(0.0)                                     # 缺值補訓練期中位數 → 標準化後 0
    assert list(x[0][1:]) == pytest.approx([(0 - 1 / 3) / math.sqrt(2 / 9), (0 - 2 / 3) / math.sqrt(2 / 9)])  # 沒看過的產業


def test_split_purges_labels_that_reach_into_the_test_period():
    rows = [R(f"2026-09-{d:02d}", 0.0, {}, end=f"2026-09-{d + 3:02d}") for d in range(1, 21)]
    tr, te, purged = ablation.split(rows, test_share=0.3)
    assert te[0].sample.day == "2026-09-15" and purged == 2                  # 9/13、9/14 的結束日在 9/15 之後
    assert all(r.sample.end_day <= "2026-09-15" for r in tr)


def day_of(d: int) -> str:
    """測試用日曆：每個月 28 個交易日。"""
    return f"2026-{9 + d // 28:02d}-{d % 28 + 1:02d}"


def synthetic(signal_in_news: bool, days=40, per_day=25, seed=5, horizon=1):
    rng = random.Random(seed)
    rows = []
    for d in range(days):
        day, end = day_of(d), day_of(d + horizon)
        for _ in range(per_day):
            x = {k: rng.gauss(0, 1) for k in ablation.MARKET}
            news = rng.gauss(0, 1)
            ex = 0.02 * (news if signal_in_news else 0) + 0.02 * x["r5"] + rng.gauss(0, 0.02)
            x.update({"pos": max(news, 0), "neg": max(-news, 0), "score": news, "log_news": 0.0})
            rows.append(R(day, ex, x, rng.choice(["甲", "乙"]), end))
    return rows


def test_news_with_extra_signal_beats_market_only_on_the_same_rows():
    pytest.importorskip("sklearn")
    res = ablation.evaluate(synthetic(True), horizon=1)
    a, b = res.models["A：只用市場"], res.models["B：市場＋新聞"]
    assert a["cls"]["n_labeled"] == b["cls"]["n_labeled"] == res.n_test           # 同一批測試樣本
    assert a["ic"]["mean"] > 0.1                                                 # r5 本身有訊號
    assert b["ic"]["mean"] > a["ic"]["mean"] + 0.1 and res.b_vs_a["p"] < 0.01
    assert b["cls"]["balanced_accuracy"] > a["cls"]["balanced_accuracy"]


def test_news_without_extra_signal_does_not_help():
    pytest.importorskip("sklearn")
    res = ablation.evaluate(synthetic(False), horizon=1)
    assert abs(res.b_vs_a["mean"]) < 0.02                            # 加新聞的 IC 幾乎不變（p 值會偶爾碰巧小於 0.05）
    assert res.models["N：只用新聞"]["ic"]["p"] > 0.05


def test_monthly_folds_train_only_on_the_past_and_purge_overlaps():
    rows = synthetic(False, days=84, per_day=2, horizon=5)             # 9、10、11 月各 28 個交易日
    folds = ablation.monthly_folds(rows, min_train_days=28)
    assert [m for m, *_ in folds] == ["2026-10", "2026-11"]           # 9 月之前沒有訓練資料，不測
    for m, tr, te, purged in folds:
        start = min(r.sample.day for r in te)
        assert all(r.sample.day[:7] == m for r in te)
        assert all(r.sample.day < start and r.sample.end_day <= start for r in tr)
        assert purged == 4 * 2                                        # 月底前 4 天的 5 日標籤跨進測試月
    assert len(folds[1][1]) > len(folds[0][1])                        # expanding window


def test_walk_forward_pools_every_test_month():
    pytest.importorskip("sklearn")
    res = ablation.walk_forward(synthetic(True, days=84, per_day=20), horizon=1, min_train_days=28)
    assert [f["month"] for f in res.folds] == ["2026-10", "2026-11"]
    assert all(f["no_overlap"] for f in res.folds)
    assert res.n_test == sum(f["n_test"] for f in res.folds) == 56 * 20
    a, b = res.models["A：只用市場"], res.models["B：市場＋新聞"]
    assert b["ic"]["mean"] > a["ic"]["mean"] + 0.1 and res.b_vs_a["p"] < 0.01
    assert b["cls"]["n_labeled"] == res.n_test
    # 扣成本：每天的報酬差減掉多空兩邊的來回成本
    gross, net = b["spread"]["mean"], b["net"][0.003]["mean"]
    assert net == pytest.approx(gross - 0.006) and b["net"][0.0]["mean"] == pytest.approx(gross)


def test_walk_forward_needs_enough_history():
    with pytest.raises(ValueError, match="walk-forward"):
        ablation.walk_forward(synthetic(False, days=20, per_day=2), horizon=1, min_train_days=60)
