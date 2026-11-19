"""機器學習預測：模型參數存取、鎖定、和字典預測一起建立、只預測直接提及、驗證與 API。"""
import json
import shutil
import sqlite3

import numpy as np
import pytest
from fastapi.testclient import TestClient

from radar import db, mlmodel, predict, validate
from radar.analysis import lexicon
from radar.api.app import create_app

STOCKS = [("2330", "台積電", ["台積"], ["半導體業", "晶圓代工"], 6e13),
          ("2408", "南亞科", [], ["半導體業", "記憶體"], 2e11),
          ("2344", "華邦電", [], ["半導體業", "記憶體"], 3e11)]


def setup_db(c):
    c.executemany("INSERT INTO stocks (code, name, aliases, sectors, market, market_cap, updated_at) "
                  "VALUES (?, ?, ?, ?, '上市', ?, 'now')",
                  [(code, n, json.dumps(a, ensure_ascii=False), json.dumps(s, ensure_ascii=False), cap)
                   for code, n, a, s, cap in STOCKS])
    c.commit()


def insert_model(c, version=1, horizons=(1, 3, 5, 10)):
    """假模型：分數越高越偏「漲」。特徵順序 pos, neg, score, log_news。"""
    params = {"classes": ["down", "flat", "up"], "coef": [[0, 0, -2, 0], [0, 0, 0, 0], [0, 0, 2, 0]],
              "intercept": [0, 0.5, 0], "med": [0, 0, 0, 0], "mu": [0, 0, 0, 0], "sd": [1, 1, 1, 1], "C": 0.1}
    ev = {"ic": 0.05, "ic_p": 0.01, "balanced_accuracy": 0.35, "hit_rate": 0.5, "all_down_hit_rate": 0.52,
          "spread": 0.008, "net": {"0": 0.008, "0.003": 0.002, "0.00585": -0.004}}
    with c:
        c.executemany("INSERT INTO ml_models (version, horizon, created_at, features, params_json, lexicon_checksum, "
                      "target_version, train_first, train_last, n_train, eval_json) VALUES (?, ?, 'now', ?, ?, 'x', 'v3', "
                      "'2025-11-17', '2026-11-13', 100, ?)",
                      [(version, h, json.dumps(mlmodel.FEATURES), json.dumps(params), json.dumps(ev)) for h in horizons])


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    setup_db(c)
    return c


@pytest.fixture
def lexfile(tmp_path):
    p = tmp_path / "lexicon.json"
    shutil.copy(lexicon.HANDMADE_PATH, p)
    return p


def add_news(c, title, body="", url="https://money.udn.com/money/story/1/1", published="2026-11-20T10:00:00+08:00"):
    cur = c.execute("INSERT INTO news (url, source, title, body, published_at, fetched_at) VALUES (?, 'udn', ?, ?, ?, ?)",
                    (url, title, body, published, published))
    c.commit()
    return cur.lastrowid


def test_proba_matches_sklearn():
    from sklearn.linear_model import LogisticRegression
    rng = np.random.default_rng(0)
    x = rng.normal(size=(300, 4))
    y = np.where(x[:, 2] > 0.5, "up", np.where(x[:, 2] < -0.5, "down", "flat"))
    m = LogisticRegression(C=0.3, max_iter=2000).fit(x, y)
    model = mlmodel.Model(1, 1, list(m.classes_), m.coef_.tolist(), m.intercept_.tolist(),
                          [0.0] * 4, [0.0] * 4, [1.0] * 4, 0.3)
    for row, want in zip(x[:20], m.predict_proba(x[:20])):
        got = model.proba(dict(zip(mlmodel.FEATURES, row)))
        assert [got[k] for k in m.classes_] == pytest.approx(list(want))


def test_decide_needs_margin():
    assert mlmodel.decide(0.36, 0.33) == "neu"        # 只差 3 個百分點
    assert mlmodel.decide(0.38, 0.33) == "pos"        # 剛好 5 個百分點
    assert mlmodel.decide(0.30, 0.36) == "neg"
    assert mlmodel.decide(0.40, 0.20, margin=0.25) == "neu"


def test_validate_applies_margin_not_stored_argmax(conn):
    days = ["2026-11-02", "2026-11-03"]
    conn.executemany("INSERT INTO trading_days (date) VALUES (?)", [(d,) for d in days])
    conn.executemany("INSERT INTO prices (date, code, close) VALUES (?, ?, ?)",
                     [(d, "TAIEX", 100.0) for d in days] + [("2026-11-02", "2330", 100.0), ("2026-11-03", "2330", 90.0)])
    nid = add_news(conn, "台積電", published="2026-11-02T10:00:00+08:00")
    # 存的是原始最大類別「利空」，但下跌只比上漲多 2 個百分點 → 網站判中性、不計入命中率
    conn.execute("INSERT INTO ml_predictions (news_id, stock_code, horizon, model_version, label, p_up, p_flat, p_down, "
                 "features_json, created_at) VALUES (?, '2330', 1, 1, 'neg', 0.32, 0.34, 0.34, '{}', '2026-11-02T10:00:00+08:00')",
                 (nid,))
    conn.commit()
    r = validate.validate(conn, horizon=1, predictor="ml")
    assert r["rows"][0]["pred"] == "flat" and r["rows"][0]["strength"] == "低" and r["summary"]["n"] == 0


def test_load_roundtrip_and_missing_value_uses_median(conn):
    insert_model(conn)
    models = mlmodel.load(conn)
    assert sorted(models) == [1, 3, 5, 10]
    m = models[10]
    assert m.predict({"pos": 3, "neg": 0, "score": 3, "log_news": 0.7})[0] == "pos"
    assert m.predict({"pos": 0, "neg": 3, "score": -3, "log_news": 0.7})[0] == "neg"
    assert m.predict({"pos": 0, "neg": 0, "score": None, "log_news": 0.7})[0] == "neu"   # 補值 0 → 持平最高
    assert mlmodel.load(conn, 99) == {}


def test_prediction_creates_locked_ml_rows(conn, lexfile):
    insert_model(conn)
    nid = add_news(conn, "台積電營收創新高", "台積電獲利大幅成長。")
    predict.create_prediction(conn, nid, predict.current_analyzer(conn, lexfile))
    rows = conn.execute("SELECT * FROM ml_predictions WHERE news_id = ?", (nid,)).fetchall()
    assert {r["horizon"] for r in rows} == {1, 3, 5, 10}
    assert {r["stock_code"] for r in rows} == {"2330"}
    r = rows[0]
    assert r["label"] == "pos" and r["p_up"] + r["p_flat"] + r["p_down"] == pytest.approx(1.0)
    assert json.loads(r["features_json"])["news"] == 1
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE ml_predictions SET label = 'neg' WHERE news_id = ?", (nid,))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM ml_predictions WHERE news_id = ?", (nid,))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE ml_models SET n_train = 1")
    # 再呼叫一次不會重建或改變
    assert mlmodel.create_for_news(conn, nid) == 0


def test_without_model_dictionary_prediction_unchanged(conn, lexfile):
    nid = add_news(conn, "台積電營收創新高")
    predict.create_prediction(conn, nid, predict.current_analyzer(conn, lexfile))
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ml_predictions").fetchone()[0] == 0


def test_ml_error_does_not_block_dictionary_prediction(conn, lexfile, monkeypatch):
    insert_model(conn)

    def boom(*a, **k):
        raise RuntimeError("壞掉了")
    monkeypatch.setattr(mlmodel, "create_for_news", boom)
    nid = add_news(conn, "台積電營收創新高")
    predict.create_prediction(conn, nid, predict.current_analyzer(conn, lexfile))
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1


def test_only_direct_mentions_with_sentiment_terms(conn, lexfile):
    insert_model(conn)
    nid = add_news(conn, "DRAM 報價大漲，記憶體族群受惠")           # 只有產業連動，沒有直接提及
    predict.create_prediction(conn, nid, predict.current_analyzer(conn, lexfile))
    assert conn.execute("SELECT COUNT(*) FROM prediction_stocks WHERE type = 'sector'").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM ml_predictions").fetchone()[0] == 0
    nid2 = add_news(conn, "台積電今天召開董事會", url="https://money.udn.com/money/story/1/2")   # 直接提及但沒有情緒詞
    predict.create_prediction(conn, nid2, predict.current_analyzer(conn, lexfile))
    assert conn.execute("SELECT COUNT(*) FROM ml_predictions").fetchone()[0] == 0


def test_news_count_feature_accumulates_same_entry_day(conn, lexfile):
    insert_model(conn)
    an = predict.current_analyzer(conn, lexfile)
    a = add_news(conn, "台積電營收創新高", url="https://money.udn.com/money/story/1/1")
    b = add_news(conn, "台積電獲利創新高", url="https://money.udn.com/money/story/1/2")
    predict.create_prediction(conn, a, an)
    predict.create_prediction(conn, b, an)
    f = {r["news_id"]: json.loads(r["features_json"]) for r in conn.execute(
        "SELECT news_id, features_json FROM ml_predictions WHERE horizon = 1")}
    assert f[a]["news"] == 1 and f[b]["news"] == 2
    assert f[a]["entry"] == f[b]["entry"]


def test_recalc_is_separate_and_overwrites_same_version(conn, lexfile):
    nid = add_news(conn, "台積電營收創新高")
    predict.create_prediction(conn, nid, predict.current_analyzer(conn, lexfile))   # 這時還沒有模型
    insert_model(conn)
    n = mlmodel.recalc(conn, log=lambda _: None)
    assert n == 4
    assert mlmodel.recalc(conn, log=lambda _: None) == 4
    assert conn.execute("SELECT COUNT(*) FROM ml_recalcs").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM ml_predictions").fetchone()[0] == 0


def test_validate_reads_ml_rows_for_that_horizon(conn):
    days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    conn.executemany("INSERT INTO trading_days (date) VALUES (?)", [(d,) for d in days])
    px = [(d, "TAIEX", 100.0) for d in days] + [(d, "2330", p) for d, p in zip(days, (100, 105, 106, 107))]
    conn.executemany("INSERT INTO prices (date, code, close) VALUES (?, ?, ?)", px)
    nid = add_news(conn, "台積電", published="2026-11-02T10:00:00+08:00")
    rows = [(nid, "2330", 1, 1, "pos", 0.6, 0.2, 0.2), (nid, "2330", 3, 1, "neu", 0.3, 0.43, 0.27)]
    conn.executemany("INSERT INTO ml_predictions (news_id, stock_code, horizon, model_version, label, p_up, p_flat, "
                     "p_down, features_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', '2026-11-02T10:00:00+08:00')",
                     rows)
    conn.commit()
    r1 = validate.validate(conn, horizon=1, predictor="ml")
    assert len(r1["rows"]) == 1 and r1["rows"][0]["pred"] == "up" and r1["rows"][0]["strength"] == "高"
    assert r1["rows"][0]["hit"] is True and r1["summary"]["n"] == 1
    r3 = validate.validate(conn, horizon=3, predictor="ml")
    assert r3["rows"][0]["pred"] == "flat" and r3["summary"]["n"] == 0      # 中性不計入命中率
    assert validate.validate(conn, horizon=5, predictor="ml")["rows"] == []
    with pytest.raises(ValueError):
        validate.validate(conn, predictor="xx")


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "radar.db"
    lexfile = tmp_path / "lexicon.json"
    shutil.copy(lexicon.HANDMADE_PATH, lexfile)
    c = db.connect(path)
    setup_db(c)
    insert_model(c)
    c.close()
    return TestClient(create_app(path, lexfile, tmp_path / "jobs.lock"))


def test_api_news_meta_validation_and_recalc(client):
    r = client.post("/api/news", json={"url": "https://a.com/1", "title": "台積電營收創新高"})
    assert r.status_code == 201
    item = r.json()
    ml = item["ml"]
    assert set(ml) == {"1", "3", "5", "10"} and ml["10"][0]["code"] == "2330" and ml["10"][0]["recalc"] is False
    meta = client.get("/api/meta").json()
    assert meta["ml"]["models"][0]["version"] == 1 and meta["ml"]["locked"] == 1
    v = client.get("/api/validation?horizon=1&predictor=ml").json()
    assert v["params"]["predictor"] == "ml" and len(v["rows"]) == 1
    assert client.get("/api/validation?horizon=1&predictor=lexicon").json()["params"]["predictor"] == "lexicon"
    assert client.get("/api/validation?predictor=ml&ml_version=1").status_code == 404     # 還沒重算
    rc = client.post("/api/ml/recalc").json()
    assert rc == {"model_version": 1, "count": 4}
    assert client.get("/api/validation?predictor=ml&ml_version=1").status_code == 200
    t = client.post("/api/analyze-text", json={"title": "台積電營收創新高", "body": ""}).json()
    assert t["ml"]["10"][0]["code"] == "2330" and t["ml"]["10"][0]["features"]["news"] == 1


def test_api_uses_recalc_for_news_without_locked_ml(tmp_path):
    path = tmp_path / "radar.db"
    lexfile = tmp_path / "lexicon.json"
    shutil.copy(lexicon.HANDMADE_PATH, lexfile)
    c = db.connect(path)
    setup_db(c)
    c.close()
    client = TestClient(create_app(path, lexfile, tmp_path / "jobs.lock"))
    client.post("/api/news", json={"url": "https://a.com/1", "title": "台積電營收創新高"})   # 沒有模型時收錄
    c = db.connect(path)
    insert_model(c)
    c.close()
    assert client.get("/api/news").json()["items"][0]["ml"] == {}
    client.post("/api/ml/recalc")
    ml = client.get("/api/news").json()["items"][0]["ml"]
    assert ml["10"][0]["recalc"] is True
