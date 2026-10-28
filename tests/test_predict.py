"""預測鎖定：抓到當下就存、存了不能改、改詞典不影響舊預測、重算另外存。"""
import json
import shutil
import sqlite3

import pytest

from radar import db, predict
from radar.analysis import lexicon
from radar.analysis.engine import Analyzer
from radar.crawl import run
from radar.crawl.models import ListItem


@pytest.fixture
def lexfile(tmp_path):
    p = tmp_path / "lexicon.json"
    shutil.copy(lexicon.LEXICON_PATH, p)
    return p


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    rows = [("2330", "台積電", ["台積"], ["半導體業", "晶圓代工"], 6e13),
            ("2408", "南亞科", [], ["半導體業", "記憶體"], 2e11),
            ("2344", "華邦電", [], ["半導體業", "記憶體"], 3e11)]
    c.executemany("INSERT INTO stocks (code, name, aliases, sectors, market, market_cap, updated_at) "
                  "VALUES (?, ?, ?, ?, '上市', ?, 'now')",
                  [(code, n, json.dumps(a, ensure_ascii=False), json.dumps(s, ensure_ascii=False), cap)
                   for code, n, a, s, cap in rows])
    c.commit()
    return c


def add_news(conn, title, body, url="https://money.udn.com/money/story/1/1"):
    cur = conn.execute("INSERT INTO news (url, source, title, body, fetched_at) VALUES (?, 'udn', ?, ?, 'now')",
                       (url, title, body))
    conn.commit()
    return cur.lastrowid


def analyzer(conn, lexfile):
    from radar.stocks.query import all_stocks
    return Analyzer(lexicon.current(conn, lexfile), all_stocks(conn))


def test_prediction_saved_with_lexicon_version(conn, lexfile):
    nid = add_news(conn, "台積電營收創新高", "DRAM 報價上漲，記憶體供不應求。")
    pid = predict.create_prediction(conn, nid, analyzer(conn, lexfile))
    p = conn.execute("SELECT * FROM predictions WHERE id = ?", (pid,)).fetchone()
    assert p["news_id"] == nid and p["lexicon_version"] == 1 and p["label"] == "pos"
    stocks = {r["stock_code"]: r for r in conn.execute("SELECT * FROM prediction_stocks WHERE prediction_id = ?", (pid,))}
    assert stocks["2330"]["type"] == "direct" and stocks["2330"]["label"] == "pos"
    assert stocks["2344"]["type"] == "sector" and stocks["2344"]["sector"] == "記憶體"
    assert "關鍵句" in stocks["2330"]["reason"]
    assert p["stock_count"] == len(stocks) == 3


def test_second_call_returns_original(conn, lexfile):
    nid = add_news(conn, "台積電營收創新高", "")
    an = analyzer(conn, lexfile)
    first = predict.create_prediction(conn, nid, an)
    lexicon.set_term(conn, "營收創新高", -5, lexfile)  # 事後把詞改成利空
    again = predict.create_prediction(conn, nid, analyzer(conn, lexfile))
    assert again == first
    p = conn.execute("SELECT total_score, label, lexicon_version FROM predictions").fetchone()
    assert p["label"] == "pos" and p["lexicon_version"] == 1  # 原始預測沒有被改寫


def test_locked_prediction_rejects_every_change(conn, lexfile):
    nid = add_news(conn, "台積電營收創新高", "")
    pid = predict.create_prediction(conn, nid, analyzer(conn, lexfile))
    for sql in ["UPDATE predictions SET label = 'neg'", "DELETE FROM predictions",
                "UPDATE prediction_stocks SET score = -9", "DELETE FROM prediction_stocks",
                f"INSERT INTO prediction_stocks (prediction_id, stock_code, score, type) VALUES ({pid}, '2408', 5, 'direct')"]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)


def test_prediction_with_no_stocks_is_also_locked(conn, lexfile):
    nid = add_news(conn, "今天天氣晴", "與股市無關。")
    pid = predict.create_prediction(conn, nid, analyzer(conn, lexfile))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO prediction_stocks (prediction_id, stock_code, score, type) VALUES ({pid}, '2330', 5, 'direct')")


def test_recalc_is_separate(conn, lexfile):
    nid = add_news(conn, "台積電營收創新高", "")
    predict.create_prediction(conn, nid, analyzer(conn, lexfile))
    lexicon.set_term(conn, "營收創新高", -5, lexfile)
    an2 = analyzer(conn, lexfile)
    assert predict.recalculate(conn, an2) == 1
    assert predict.recalculate(conn, an2) == 1  # 同版本重算會覆寫，不會重複
    r = conn.execute("SELECT * FROM recalcs").fetchall()
    assert len(r) == 1 and r[0]["lexicon_version"] == 2 and r[0]["label"] == "neg"
    assert conn.execute("SELECT label FROM predictions").fetchone()[0] == "pos"
    assert conn.execute("SELECT COUNT(*) FROM recalc_stocks").fetchone()[0] == 1


def test_predict_pending_only_new(conn, lexfile):
    a = add_news(conn, "台積電營收創新高", "", url="https://a.com/1")
    an = analyzer(conn, lexfile)
    predict.create_prediction(conn, a, an)
    add_news(conn, "南亞科跌停", "", url="https://a.com/2")
    assert len(predict.predict_pending(conn, an)) == 1
    assert predict.predict_pending(conn, an) == []


def test_unversioned_lexicon_refused(conn):
    nid = add_news(conn, "台積電", "")
    an = Analyzer(lexicon.load_file(), [])
    with pytest.raises(ValueError):
        predict.create_prediction(conn, nid, an)


def test_crawl_locks_prediction_on_save(conn, lexfile, monkeypatch):
    item = ListItem(url="https://money.udn.com/money/story/5607/1", source="udn", title="台積電營收創新高",
                    summary="法人看好")
    monkeypatch.setitem(run.FETCHERS, "udn", type("F", (), {"list_items": staticmethod(lambda limit: [item])}))
    monkeypatch.setattr(run.http, "get", lambda url: (_ for _ in ()).throw(run.http.Blocked("x")))
    rep = run.crawl_source(conn, "udn", 5, 3, log=lambda *_: None, analyzer=analyzer(conn, lexfile))
    assert rep.saved == 1
    p = conn.execute("SELECT p.label, p.lexicon_version FROM predictions p JOIN news n ON n.id = p.news_id").fetchone()
    assert (p["label"], p["lexicon_version"]) == ("pos", 1)


def test_dry_run_creates_nothing(conn, lexfile, monkeypatch):
    item = ListItem(url="https://money.udn.com/money/story/5607/2", source="udn", title="台積電", summary="摘要")
    monkeypatch.setitem(run.FETCHERS, "udn", type("F", (), {"list_items": staticmethod(lambda limit: [item])}))
    monkeypatch.setattr(run.http, "get", lambda url: (_ for _ in ()).throw(run.http.Blocked("x")))
    run.crawl_source(conn, "udn", 5, 3, dry_run=True, log=lambda *_: None, analyzer=analyzer(conn, lexfile))
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0
