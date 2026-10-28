"""網站 API：用暫存資料庫與暫存詞典，不連網（讀網址的部分換成假的回應）。"""
import json
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from radar import db, http
from radar.analysis import lexicon
from radar.api.app import create_app
from radar.crawl import single

ARTICLE = """<html><head><title>台積電營收創新高 | 經濟日報</title>
<script type="application/ld+json">{"@type": "NewsArticle", "headline": "台積電營收創新高",
 "datePublished": "2026-10-27T09:30:00+08:00"}</script></head><body><div id="article_body">
<p>台積電公布九月營收創新高，法人看好第四季獲利持續成長，記憶體族群同步走強。</p>
<p>分析師指出先進製程需求強勁，訂單能見度已看到明年下半年，供不應求的情況短期不會改變。</p>
</div></body></html>"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "radar.db"
    lexfile = tmp_path / "lexicon.json"
    shutil.copy(lexicon.LEXICON_PATH, lexfile)
    c = db.connect(path)
    rows = [("2330", "台積電", ["台積"], ["半導體業", "晶圓代工"], 6e13),
            ("2408", "南亞科", [], ["半導體業", "記憶體"], 2e11),
            ("2344", "華邦電", [], ["半導體業", "記憶體"], 3e11)]
    c.executemany("INSERT INTO stocks (code, name, aliases, sectors, market, market_cap, updated_at) "
                  "VALUES (?, ?, ?, ?, '上市', ?, 'now')",
                  [(code, n, json.dumps(a, ensure_ascii=False), json.dumps(s, ensure_ascii=False), cap)
                   for code, n, a, s, cap in rows])
    c.commit()
    c.close()
    monkeypatch.setattr(single, "resolve", lambda host: ["93.184.216.34"])
    return TestClient(create_app(path, lexfile))


def fake_get(status=200, text=ARTICLE, ctype="text/html; charset=utf-8", exc=None):
    def get(url, **kw):
        if exc:
            raise exc
        req = httpx.Request("GET", url)
        resp = httpx.Response(status, text=text, headers={"content-type": ctype}, request=req)
        if status >= 400:
            err = httpx.HTTPStatusError("x", request=req, response=resp)
            raise RuntimeError(f"讀取失敗：{url}") from err
        return resp
    return get


# ---------- 分析 ----------

def test_analyze_text(client):
    r = client.post("/api/analyze-text", json={"title": "台積電營收創新高", "body": "記憶體供不應求。"})
    assert r.status_code == 200
    d = r.json()
    assert d["label"] == "pos" and d["lexicon_version"] == 1
    codes = {v["code"]: v["type"] for v in d["impacts"]}
    assert codes["2330"] == "direct" and codes["2344"] == "sector"


def test_analyze_text_empty(client):
    r = client.post("/api/analyze-text", json={"title": " ", "body": ""})
    assert r.status_code == 400 and "標題或內文" in r.json()["detail"]


def test_analyze_url_ok(client, monkeypatch):
    monkeypatch.setattr(http, "get", fake_get())
    d = client.post("/api/analyze-url", json={"url": "https://money.udn.com/money/story/5607/1?utm_source=x"}).json()
    assert d["ok"] and d["source"] == "udn" and d["source_name"] == "經濟日報"
    assert d["url"] == "https://money.udn.com/money/story/5607/1"  # 追蹤參數拿掉
    assert d["title"] == "台積電營收創新高" and "先進製程" in d["body"]
    assert d["analysis"]["label"] == "pos" and d["saved_id"] is None


def test_analyze_url_other_site(client, monkeypatch):
    monkeypatch.setattr(http, "get", fake_get())
    d = client.post("/api/analyze-url", json={"url": "https://example.com/a"}).json()
    assert d["ok"] and d["source"] == "other" and d["source_name"] == "其他網站"


@pytest.mark.parametrize("kw, words", [
    ({"status": 403}, "拒絕讀取"),
    ({"status": 404}, "找不到這個網頁"),
    ({"exc": http.Blocked("robots")}, "robots.txt"),
    ({"exc": RuntimeError("讀取失敗：逾時")}, "連不上網站"),
    ({"ctype": "application/pdf"}, "不是網頁"),
    ({"text": "<html><body><script>app()</script></body></html>"}, "讀不到文字"),
])
def test_analyze_url_failures_explain_why(client, monkeypatch, kw, words):
    monkeypatch.setattr(http, "get", fake_get(**kw))
    d = client.post("/api/analyze-url", json={"url": "https://www.cw.com.tw/article/1"}).json()
    assert d["ok"] is False and words in d["reason"] and d["analysis"] is None
    assert d["source_name"] == "天下雜誌"  # 讀不到也告訴使用者是哪個來源


def test_analyze_url_summary_only_is_flagged(client, monkeypatch):
    html = '<html><head><meta property="og:title" content="南亞科跌停"><meta property="og:description" ' \
           'content="市場擔心競爭加劇，南亞科開盤即跌停。"></head><body></body></html>'
    monkeypatch.setattr(http, "get", fake_get(text=html))
    d = client.post("/api/analyze-url", json={"url": "https://example.com/b"}).json()
    assert d["ok"] and d["partial"] and "摘要" in d["reason"] and d["analysis"]["label"] == "neg"


@pytest.mark.parametrize("url, words", [("ftp://x.com/a", "http"), ("新聞", "http"),
                                         ("http://127.0.0.1:8000/api/meta", "內部網路")])
def test_analyze_url_rejects_bad_urls(client, monkeypatch, url, words):
    monkeypatch.setattr(single, "resolve", lambda host: ["127.0.0.1"])
    r = client.post("/api/analyze-url", json={"url": url})
    assert r.status_code == 400 and words in r.json()["detail"]


# ---------- 新聞 ----------

def test_add_news_locks_prediction_and_lists(client):
    r = client.post("/api/news", json={"url": "https://example.com/n1?fbclid=1", "title": "台積電營收創新高",
                                       "body": "記憶體供不應求。", "published_at": "2026-10-27T09:30:00+08:00"})
    assert r.status_code == 201
    item = r.json()
    assert item["source"] == "other" and item["body_from"] == "manual" and item["url"] == "https://example.com/n1"
    p = item["prediction"]
    assert p["label"] == "pos" and p["lexicon_version"] == 1 and p["stocks"][0]["code"] == "2330"

    again = client.post("/api/news", json={"url": "https://example.com/n1", "title": "x"})
    assert again.status_code == 409

    client.post("/api/news", json={"url": "https://money.udn.com/money/story/1/2", "title": "南亞科跌停",
                                   "body": "市場擔心競爭加劇。", "published_at": "2026-10-28T10:00:00+08:00"})
    all_ = client.get("/api/news").json()
    assert all_["total"] == 2 and all_["items"][0]["title"] == "南亞科跌停"  # 新的在前
    assert [i["title"] for i in client.get("/api/news?label=pos").json()["items"]] == ["台積電營收創新高"]
    assert client.get("/api/news?source=udn").json()["total"] == 1
    assert client.get("/api/news?q=競爭").json()["total"] == 1
    assert client.get("/api/news?stock=2330").json()["total"] == 1
    assert client.get("/api/news?source=abc").status_code == 400

    one = client.get(f"/api/news/{item['id']}").json()
    assert any(m["term"] == "台積電" for m in one["highlight"]["matches"])
    assert client.get("/api/news/999").status_code == 404


def test_add_news_requires_url_and_title(client):
    assert client.post("/api/news", json={"url": "", "title": "x"}).status_code == 400
    assert client.post("/api/news", json={"url": "https://a.com/1", "title": " "}).status_code == 400
    r = client.post("/api/news", json={"title": "沒有網址"})
    assert r.status_code == 422 and "url" in r.json()["detail"]


def test_saved_prediction_unchanged_after_lexicon_edit(client):
    item = client.post("/api/news", json={"url": "https://a.com/1", "title": "台積電營收創新高"}).json()
    client.post("/api/lexicon", json={"term": "創新高", "weight": -3})
    again = client.get(f"/api/news/{item['id']}").json()
    assert again["prediction"] == item["prediction"]  # 鎖定的預測不受詞典修改影響
    assert client.post("/api/analyze-text", json={"title": "台積電營收創新高"}).json()["lexicon_version"] == 2


# ---------- 詞典 ----------

def test_lexicon_add_change_delete_make_versions(client):
    d = client.get("/api/lexicon").json()
    assert d["version"] == 1 and any(t["term"] == "創新高" for t in d["sentiment"])
    d = client.post("/api/lexicon", json={"term": "測試詞", "weight": 2.5}).json()
    assert d["version"] == 2 and {"term": "測試詞", "weight": 2.5} in d["sentiment"]
    d = client.delete("/api/lexicon", params={"term": "測試詞"}).json()
    assert d["version"] == 3 and all(t["term"] != "測試詞" for t in d["sentiment"])
    assert [v["version"] for v in d["versions"]] == [3, 2, 1]


def test_lexicon_errors(client):
    assert client.post("/api/lexicon", json={"term": "x", "weight": 0}).status_code == 400
    assert client.post("/api/lexicon", json={"term": "x", "weight": 9}).status_code == 400
    assert client.delete("/api/lexicon", params={"term": "沒有這個詞"}).status_code == 404


# ---------- 驗證、其他 ----------

def test_validation_and_params(client):
    d = client.get("/api/validation?horizon=1").json()
    assert d["params"]["horizon"] == 1 and d["summary"]["n"] == 0
    assert client.get("/api/validation?benchmark=xx").status_code == 422
    assert client.get("/api/validation?horizon=0").status_code == 422
    assert client.get("/api/validation?lexicon_version=7").status_code == 404


def test_recalc_then_validate_version(client):
    client.post("/api/news", json={"url": "https://a.com/1", "title": "台積電營收創新高"})
    assert client.post("/api/recalc").json() == {"lexicon_version": 1, "count": 1}
    assert client.get("/api/validation?lexicon_version=1").status_code == 200


def test_crawl_rejects_unknown_source(client):
    assert client.post("/api/crawl", json={"sources": ["abc"]}).status_code == 400


def test_prices_import(client):
    d = client.post("/api/prices/import", json={"csv": "日期,代號,收盤價\n2026-10-27,2330,1500\n"}).json()
    assert d["rows"] == 1
    assert client.post("/api/prices/update", json={"start": "10/1"}).status_code == 400


def test_meta_and_stocks(client):
    m = client.get("/api/meta").json()
    assert m["sources"]["other"] == "其他網站" and m["counts"]["stocks"] == 3
    s = client.get("/api/stocks?q=台積").json()
    assert s[0]["code"] == "2330" and s[0]["aliases"] == ["台積"]
    assert len(client.get("/api/stocks").json()) == 3


def test_no_stocks_gives_clear_error(tmp_path):
    lexfile = tmp_path / "lexicon.json"
    shutil.copy(lexicon.LEXICON_PATH, lexfile)
    c = TestClient(create_app(tmp_path / "empty.db", lexfile))
    r = c.post("/api/analyze-text", json={"title": "台積電"})
    assert r.status_code == 503 and "stocks.load" in r.json()["detail"]


def test_feed_filters_highlight_and_summary(client):
    client.post("/api/news", json={"url": "https://a.com/1", "title": "台積電營收創新高",
                                   "published_at": "2026-10-20T09:00:00+08:00"})
    client.post("/api/news", json={"url": "https://a.com/2", "title": "南亞科跌停",
                                   "published_at": "2026-10-27T09:00:00+08:00"})
    assert client.get("/api/news?since=2026-10-25").json()["total"] == 1
    assert client.get("/api/news?since=10/25").status_code == 422
    assert client.get("/api/news?q=2408").json()["items"][0]["title"] == "南亞科跌停"  # 搜代號也找得到
    items = client.get("/api/news?highlight=true").json()["items"]
    assert all(i["highlight"]["matches"] for i in items)
    assert "highlight" not in client.get("/api/news").json()["items"][0]

    s = client.get("/api/news/summary").json()
    assert s["counts"]["pos"] == 1 and s["counts"]["neg"] == 1 and s["total"] == 2
    assert s["up"][0]["code"] == "2330" and s["down"][0]["code"] == "2408" and s["up"][0]["n"] == 1
    assert client.get("/api/news/summary?since=2026-10-25").json()["up"] == []


def test_meta_recalc_versions(client):
    client.post("/api/news", json={"url": "https://a.com/1", "title": "台積電營收創新高"})
    assert client.get("/api/meta").json()["recalc_versions"] == []
    client.post("/api/recalc")
    m = client.get("/api/meta").json()
    assert m["recalc_versions"] == [1] and m["last_fetched"]


def test_web_page_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "台股新聞雷達" in r.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/api/nope").status_code == 404


def test_crawl_busy_while_scheduled_job_runs(tmp_path):
    from radar import jobs
    lexfile = tmp_path / "lexicon.json"
    shutil.copy(lexicon.LEXICON_PATH, lexfile)
    lock = tmp_path / "jobs.lock"
    c = TestClient(create_app(tmp_path / "r.db", lexfile, lock))
    with jobs.Lock(lock):
        r = c.post("/api/crawl", json={"sources": ["udn"]})
    assert r.status_code == 409 and "排程" in r.json()["detail"]


def test_meta_last_job(client):
    assert client.get("/api/meta").json()["last_job"] is None
