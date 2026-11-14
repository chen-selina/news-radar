"""權重估計：樣本與特徵怎麼組、標籤、依時間切分、模型學得到訊號、資料不足時的提示。"""
import random

import pytest

from radar import backfill, db, train
from radar.analysis import remi
from radar.analysis.lexicon import Lexicon
from radar.train import Sample

DAYS = ["2026-09-01", "2026-09-02", "2026-09-03"]
SMALL = remi.RemiDict({"成長": 1, "虧損": -1}, {})


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(remi, "load", lambda path=None: SMALL)
    c = db.connect(":memory:")
    c.executemany("INSERT INTO trading_days (date) VALUES (?)", [(d,) for d in DAYS])
    px = [(d, "TAIEX", 100.0) for d in DAYS]
    px += [(d, "1111", p) for d, p in zip(DAYS, (100, 100, 106))] + [(d, "2222", p) for d, p in zip(DAYS, (100, 100, 95))]
    c.executemany("INSERT INTO prices (date, code, close) VALUES (?, ?, ?)", px)
    c.executemany("INSERT INTO stocks (code, name, market, updated_at) VALUES (?, ?, '上市', 'now')",
                  [("1111", "甲乙科技"), ("2222", "丙丁電子")])
    c.commit()
    return c


def add(conn, n, title, body, when, code="1111"):
    backfill.save_links(conn, code, when[:10], [{"date": "2026-09-01 00:00:00", "title": title, "source": "測試日報",
                                                  "link": f"https://a.example.com/{n}"}])
    conn.execute("UPDATE backfill_news SET status = 'ok', title = ?, body = ?, published_at = ? WHERE url = ?",
                 (title, body, when, f"https://a.example.com/{n}"))
    conn.commit()


BASE = Lexicon({"漲停": 2.0, "砍單": -2.0}, {})


def test_samples_are_one_per_stock_day_with_term_counts(conn):
    add(conn, 1, "甲乙科技漲停", "甲乙科技營收成長。", "2026-09-02T15:00:00+08:00")
    add(conn, 2, "丙丁電子遭砍單", "丙丁電子出現虧損，未見成長。", "2026-09-02T10:00:00+08:00")
    s = {x.code: x for x in train.build_samples(conn, BASE)}
    assert set(s) == {"1111", "2222"} and s["1111"].day == "2026-09-02"      # 盤中、盤後的基準日都是當天
    assert s["1111"].features == {"漲停": 1.5, "成長": 1.0}                    # 標題 ×1.5；每個詞只取方向，不帶人工強度
    assert s["2222"].features == {"砍單": -1.5, "虧損": -1.0, "成長": -1.0}    # 「未見成長」算負的


def test_same_event_counts_once_and_events_are_averaged(conn):
    add(conn, 1, "甲乙科技董事會決議配息創新高營收成長", "甲乙科技營收成長。", "2026-09-02T15:00:00+08:00")
    add(conn, 2, "甲乙科技董事會決議配息創新高營收成長！", "甲乙科技營收成長，獲利成長，展望成長。", "2026-09-02T15:10:00+08:00")
    add(conn, 3, "甲乙科技海外廠出現虧損引發關注", "甲乙科技海外廠虧損。", "2026-09-02T16:00:00+08:00")
    (s,) = train.build_samples(conn, BASE)
    assert s.news == 3
    # 同事件取情緒詞多的那篇（成長：標題 1.5＋內文 3）；兩個事件各占一半
    assert s.features == {"成長": pytest.approx(4.5 / 2), "虧損": pytest.approx(-2.5 / 2)}


def test_news_before_price_data_and_sector_only_stocks_are_left_out(conn):
    add(conn, 1, "甲乙科技營收成長", "甲乙科技營收成長。", "2026-08-01T10:00:00+08:00")     # 早於價格資料
    add(conn, 2, "產業動態", "今天天氣晴。", "2026-09-02T15:00:00+08:00")                    # 沒有提到公司
    assert train.build_samples(conn, BASE) == []


def test_label_uses_validation_rules(conn):
    samples = [Sample("1111", "2026-09-02", {"成長": 1.0}), Sample("2222", "2026-09-02", {"虧損": -1.0}),
               Sample("1111", "2026-09-01", {"成長": 1.0}), Sample("1111", "2026-09-03", {"成長": 1.0})]
    train.label(conn, samples, horizon=1)
    assert [s.y for s in samples] == [1, 0, None, None]     # 9/1→9/2 持平不用；9/3 之後還沒有價格
    assert samples[0].ex == pytest.approx(0.06) and samples[1].ex == pytest.approx(-0.05)


def synthetic(days=20, per_day=30, seed=1):
    """「好」出現之後多半漲、「壞」出現之後多半跌、「雜」沒有關係；「罕」只出現幾次。"""
    rng = random.Random(seed)
    out = []
    for d in range(days):
        for i in range(per_day):
            good, bad = rng.random() < 0.5, rng.random() < 0.5
            p = 0.5 + 0.35 * good - 0.35 * bad
            y = int(rng.random() < p)
            f = {"雜": float(rng.randint(0, 2))}
            if good:
                f["好"] = 1.0
            if bad:
                f["壞"] = -1.0          # 負面詞的特徵是負的（和引擎一樣）
            if d == 0 and i < 3:
                f["罕"] = 1.0
            out.append(Sample(f"{i:04d}", f"2026-{9 + d // 28:02d}-{d % 28 + 1:02d}", f, 1, 0.03 if y else -0.03, y))
    return out


def test_model_learns_signal_and_beats_baselines():
    pytest.importorskip("sklearn")
    res = train.train(synthetic(), min_count=15, test_share=0.3)
    assert res.n_days == 20 and res.n_train == 14 * 30 and res.n_test == 6 * 30
    assert res.train_days[1] < res.test_days[0]                         # 測試期全部在訓練期之後
    assert res.weights["好"] > 0.5 and res.weights["壞"] > 0.5           # 負面詞的特徵是負的，所以係數也是正的
    assert abs(res.weights["雜"]) < min(res.weights["好"], res.weights["壞"]) / 2
    assert "罕" not in res.weights                                       # 出現太少的詞不納入
    m = res.metrics
    assert m["模型（估計的權重）"]["accuracy"] > m["全猜多數"]["accuracy"] + 0.1
    assert m["模型（估計的權重）"]["spread"] > 0.02


def test_no_signal_means_no_edge():
    pytest.importorskip("sklearn")
    rng = random.Random(7)
    data = [Sample("1", f"2026-09-{d + 1:02d}", {"雜": float(rng.randint(0, 2)), "訊": float(rng.randint(-1, 1))}, 1,
                   0.02 if (y := rng.random() < 0.5) else -0.02, int(y)) for d in range(20) for _ in range(40)]
    res = train.train(data, min_count=5)
    assert abs(res.metrics["模型（估計的權重）"]["accuracy"] - 0.5) < 0.1    # 沒有訊號就學不到東西


def test_train_refuses_when_data_is_too_thin():
    pytest.importorskip("sklearn")
    few = [Sample("1", f"2026-09-0{d}", {"好": 1.0}, 1, 0.02, 1) for d in (1, 2, 3)]
    with pytest.raises(ValueError, match="交易日"):
        train.train(few)
    one_side = [Sample("1", f"2026-09-{d:02d}", {"好": 1.0}, 1, 0.02, 1) for d in range(1, 11)]
    with pytest.raises(ValueError, match="只有漲或只有跌"):
        train.train(one_side)
    with pytest.raises(ValueError, match="min-count"):
        train.train(synthetic(days=8, per_day=4), min_count=500)


def test_save_writes_readable_json(tmp_path):
    pytest.importorskip("sklearn")
    res = train.train(synthetic(), min_count=15)
    train.save(res, tmp_path / "w.json")
    import json
    d = json.loads((tmp_path / "w.json").read_text(encoding="utf-8"))
    assert d["weights"][0]["weight"] >= d["weights"][-1]["weight"] and {"term", "weight", "samples"} <= set(d["weights"][0])
    assert d["n_train"] == res.n_train and "模型（估計的權重）" in d["metrics"]
