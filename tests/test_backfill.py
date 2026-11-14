"""回補歷史新聞：查網址（假的 FinMind）、略過規則、抓內文（假的網頁）、中斷接續、進度。全部不連網。"""
from datetime import date

import pytest

from radar import backfill, compare, db
from radar.analysis.lexicon import Lexicon
from radar.crawl.single import BadUrl, Fetched


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.executemany("INSERT INTO stocks (code, name, market, market_cap, updated_at) VALUES (?, ?, '上市', ?, 'now')",
                  [("1111", "甲乙科技", 300.0), ("2222", "丙丁電子", 200.0), ("3333", "戊己金控", 100.0)])
    c.commit()
    return c


def item(link, title="甲乙科技營收創新高 - 測試日報", source="測試日報", when="2026-09-01 01:02:00", code="1111"):
    return {"date": when, "stock_id": code, "title": title, "source": source, "link": link}


def fake_finmind(table, calls=None, limit_after=None):
    def fetch(code, day, token=None):
        if calls is not None:
            if limit_after is not None and len(calls) >= limit_after:
                raise backfill.RateLimited("到上限")
            calls.append((code, day))
        return table.get((code, day), [])
    return fetch


def page(url, title="甲乙科技營收創新高", body="甲乙科技今天公布營收，創下歷史新高。", ok=True, partial=False,
         reason=None, published="2026-09-01T09:30:00+08:00"):
    return Fetched(url, "other", "其他網站", ok=ok, reason=reason, title=title, body=body,
                   published_at=published, partial=partial)


D1, D2 = date(2026, 9, 1), date(2026, 9, 2)


def test_time_is_converted_from_utc_to_taipei():
    assert backfill.to_taipei("2026-09-01 01:02:00") == "2026-09-01T09:02:00+08:00"
    assert backfill.to_taipei("2026-09-01 20:30:00") == "2026-09-02T04:30:00+08:00"   # 跨日
    assert backfill.to_taipei("亂寫") is None and backfill.to_taipei(None) is None


def test_skip_rules():
    assert "Google" in backfill.skip_reason("https://news.google.com/rss/articles/CBMi")
    assert "討論區" in backfill.skip_reason("https://www.cmoney.tw/forum/article/1")
    assert backfill.skip_reason("https://cmnews.com.tw/article/abc") is None      # CMoney 的新聞站照抓
    assert backfill.skip_reason("https://news.example.com/a/1") is None
    assert backfill.skip_reason("javascript:alert(1)") == "網址格式不對"


def test_list_title_drops_outlet_suffix():
    assert backfill.clean_list_title("甲乙科技營收創新高 - 測試日報", "測試日報") == "甲乙科技營收創新高"
    assert backfill.clean_list_title("A - B 合作案", "測試日報") == "A - B 合作案"


def test_collect_saves_links_tags_and_skips(conn):
    table = {("1111", "2026-09-01"): [item("https://news.example.com/a/1?utm_source=x"),
                                      item("https://news.google.com/rss/articles/CBMi"),
                                      item("https://www.cmoney.tw/forum/article/9")],
             ("2222", "2026-09-01"): [item("https://news.example.com/a/1", code="2222")]}   # 同一篇也列在另一檔底下
    rep = backfill.collect(conn, ["1111", "2222"], D1, D1, fetch=fake_finmind(table), log=lambda m: None)
    assert rep == {"queried": 2, "new": 3, "left": 0, "limited": False}
    rows = {r["url"]: r for r in conn.execute("SELECT * FROM backfill_news")}
    assert rows["https://news.example.com/a/1"]["status"] == "pending"            # 追蹤參數拿掉了
    assert rows["https://news.example.com/a/1"]["list_title"] == "甲乙科技營收創新高"
    assert rows["https://news.example.com/a/1"]["listed_at"] == "2026-09-01T09:02:00+08:00"
    assert rows["https://news.google.com/rss/articles/CBMi"]["status"] == "skip"
    assert rows["https://www.cmoney.tw/forum/article/9"]["status"] == "skip"
    tags = {r["code"] for r in conn.execute("SELECT code FROM backfill_tags WHERE news_id = ?",
                                            (rows["https://news.example.com/a/1"]["id"],))}
    assert tags == {"1111", "2222"}


def test_collect_resumes_after_rate_limit(conn):
    calls: list = []
    rep = backfill.collect(conn, ["1111", "2222"], D1, D2, fetch=fake_finmind({}, calls, limit_after=3), log=lambda m: None)
    assert rep["limited"] and rep["queried"] == 3 and rep["left"] == 1
    assert calls[0] == ("1111", "2026-09-02")                       # 由新到舊
    again: list = []
    rep = backfill.collect(conn, ["1111", "2222"], D1, D2, fetch=fake_finmind({}, again), log=lambda m: None)
    assert again == [("2222", "2026-09-01")] and rep["left"] == 0   # 查過的不再查


def test_collect_requeries_today(conn, monkeypatch):
    monkeypatch.setattr(backfill.config, "now_taipei", lambda: __import__("datetime").datetime(2026, 9, 2, 12, 0))
    calls: list = []
    for _ in range(2):
        backfill.collect(conn, ["1111"], D2, D2, fetch=fake_finmind({}, calls), log=lambda m: None)
    assert calls == [("1111", "2026-09-02")] * 2


def test_collect_keeps_going_when_one_query_fails(conn):
    def fetch(code, day, token=None):
        if code == "1111":
            raise RuntimeError("連不上")
        return [item("https://news.example.com/a/2", code=code)]
    rep = backfill.collect(conn, ["1111", "2222"], D1, D1, fetch=fetch, log=lambda m: None)
    assert rep["queried"] == 1 and rep["new"] == 1
    assert backfill.todo_days(conn, ["1111", "2222"], D1, D1) == [("1111", "2026-09-01")]   # 失敗的下次再查


def add_links(conn, links):
    backfill.save_links(conn, "1111", "2026-09-01", [item(u) for u in links])


def test_fetch_pending_stores_body_and_status(conn):
    add_links(conn, ["https://a.example.com/1", "https://a.example.com/2", "https://a.example.com/3", "not a url"])
    pages = {"https://a.example.com/1": page("https://a.example.com/1"),
             "https://a.example.com/2": page("https://a.example.com/2", body="", partial=True, published=None,
                                             reason="只讀到標題與摘要"),
             "https://a.example.com/3": page("https://a.example.com/3", ok=False, title="", body="", reason="網站拒絕讀取（HTTP 403）")}
    rep = backfill.fetch_pending(conn, fetch=lambda u: pages[u], log=lambda m: None)
    assert (rep["ok"], rep["partial"], rep["fail"], rep["left"]) == (1, 1, 1, 0)
    rows = {r["url"]: r for r in conn.execute("SELECT * FROM backfill_news")}
    ok = rows["https://a.example.com/1"]
    assert ok["status"] == "ok" and "歷史新高" in ok["body"] and ok["published_at"] == "2026-09-01T09:30:00+08:00"
    part = rows["https://a.example.com/2"]
    assert part["status"] == "partial" and part["published_at"] == part["listed_at"]     # 網頁沒有時間就用查到的時間
    assert rows["https://a.example.com/3"]["status"] == "fail" and "403" in rows["https://a.example.com/3"]["reason"]
    assert rows["not a url"]["status"] == "skip"


def test_fetch_pending_limit_and_resume(conn):
    add_links(conn, [f"https://a.example.com/{i}" for i in range(5)])
    seen: list = []

    def fetch(u):
        seen.append(u)
        return page(u)
    rep = backfill.fetch_pending(conn, limit=2, fetch=fetch, log=lambda m: None)
    assert rep["ok"] == 2 and rep["left"] == 3
    rep = backfill.fetch_pending(conn, fetch=fetch, log=lambda m: None)
    assert rep["ok"] == 3 and rep["left"] == 0 and len(set(seen)) == 5      # 沒有重複抓


def test_host_that_keeps_failing_is_left_for_next_run(conn):
    add_links(conn, [f"https://bad.example.com/{i}" for i in range(8)] + ["https://good.example.com/1"])
    seen: list = []

    def fetch(u):
        seen.append(u)
        if "bad." in u:
            return page(u, ok=False, title="", body="", reason="網站拒絕讀取（HTTP 403）")
        return page(u)
    logs: list = []
    rep = backfill.fetch_pending(conn, fetch=fetch, log=logs.append)
    assert sum("bad." in u for u in seen) == backfill.HOST_FAIL_LIMIT
    assert rep["fail"] == 5 and rep["ok"] == 1 and rep["skipped_hosts"] == {"bad.example.com": 3} and rep["left"] == 3
    assert any("bad.example.com" in m for m in logs)


def test_bad_url_and_unexpected_error_do_not_stop_the_run(conn):
    add_links(conn, ["https://a.example.com/1", "https://b.example.com/1", "https://c.example.com/1"])

    def fetch(u):
        if "a." in u:
            raise BadUrl("這個網址指向本機或內部網路，不能讀取")
        if "b." in u:
            raise ValueError("怪網頁")
        return page(u)
    rep = backfill.fetch_pending(conn, fetch=fetch, log=lambda m: None)
    assert (rep["ok"], rep["fail"]) == (1, 2)


def test_status_and_top_codes(conn):
    assert backfill.top_codes(conn, 2) == ["1111", "2222"]
    out: list = []
    backfill.print_status(conn, out.append)
    assert out == ["還沒有回補資料。"]
    add_links(conn, ["https://a.example.com/1", "https://news.google.com/rss/articles/x"])
    backfill.fetch_pending(conn, fetch=page, log=lambda m: None)
    s = backfill.status(conn)
    assert s["by_status"] == {"ok": 1, "skip": 1} and s["hosts"]["a.example.com"]["ok"] == 1
    assert s["days"] == {"first": "2026-09-01", "last": "2026-09-01", "queried": 1, "codes": 1}
    backfill.print_status(conn, out.append)
    assert any("a.example.com" in m for m in out) and any("Google" in m for m in out)


def test_run_alternates_and_waits_when_limited(conn, monkeypatch):
    calls: list = []
    budget = {"n": 1}

    def fm(code, day, token=None):
        if budget["n"] <= 0:
            raise backfill.RateLimited("到上限")
        budget["n"] -= 1
        calls.append((code, day))
        return [item(f"https://a.example.com/{code}-{day}", code=code)]
    monkeypatch.setattr(backfill, "finmind_news", fm)
    monkeypatch.setattr(backfill.single, "fetch", page)
    naps: list = []

    def nap(sec):
        naps.append(sec)
        budget["n"] = 5          # 等過之後又可以查了
    backfill.run(conn, ["1111"], D1, D2, log=lambda m: None, sleep=nap)
    assert naps == [backfill.LIMIT_WAIT]                               # 只等了一次
    assert calls == [("1111", "2026-09-02"), ("1111", "2026-09-01")]
    done = conn.execute("SELECT COUNT(*) FROM backfill_news WHERE status = 'ok'").fetchone()[0]
    assert done == 2                                                   # 等待之前就先把第一篇內文抓了


def test_finmind_rate_limit_is_recognised(monkeypatch):
    import httpx

    def boom(url, **kw):
        resp = httpx.Response(402, request=httpx.Request("GET", url))
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError("讀取失敗") from e
    monkeypatch.setattr(backfill.http, "get", boom)
    with pytest.raises(backfill.RateLimited):
        backfill.finmind_news("1111", "2026-09-01")


def test_finmind_request_shape(monkeypatch):
    got = {}

    def ok(url, **kw):
        got.update(url=url, **kw)
        return type("R", (), {"json": lambda self: {"status": 200, "data": [item("https://a.example.com/1")]}})()
    monkeypatch.setattr(backfill.http, "get", ok)
    assert len(backfill.finmind_news("1111", "2026-09-01", token="t0ken")) == 1
    assert got["params"] == {"dataset": "TaiwanStockNews", "data_id": "1111", "start_date": "2026-09-01"}
    assert got["headers"] == {"Authorization": "Bearer t0ken"}
    backfill.finmind_news("1111", "2026-09-01")
    assert got["headers"] == {}


def test_backfilled_news_feed_the_comparison_but_not_the_site(conn):
    conn.executemany("INSERT INTO trading_days (date) VALUES (?)", [("2026-09-01",), ("2026-09-02",)])
    conn.executemany("INSERT INTO prices (date, code, close) VALUES (?, ?, ?)",
                     [("2026-09-01", "TAIEX", 100.0), ("2026-09-02", "TAIEX", 100.0),
                      ("2026-09-01", "1111", 100.0), ("2026-09-02", "1111", 106.0)])
    add_links(conn, ["https://a.example.com/1", "https://a.example.com/2"])
    pages = {"https://a.example.com/1": page("https://a.example.com/1", title="甲乙科技漲停", body="甲乙科技今天漲停。",
                                             published="2026-09-01T15:00:00+08:00"),
             "https://a.example.com/2": page("https://a.example.com/2", ok=False, title="", body="", reason="HTTP 404")}
    backfill.fetch_pending(conn, fetch=lambda u: pages[u], log=lambda m: None)
    rows = compare.predict_rows(conn, Lexicon({"漲停": 2.0}, {}), data="backfill")
    assert [(r.code, r.pred, r.date, r.time) for r in rows] == [("1111", "up", "2026-09-01", "15:00")]
    res = compare.compare(conn, Lexicon({"漲停": 2.0}, {}), data="backfill")
    one = next(r for r in res if r["variant"] == "手工詞典" and r["horizon"] == 1)
    assert one["n"] == 1 and one["hit_rate"] == 1.0
    assert conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 0          # 網站用的資料表沒有被動到
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0


# ---------- 試跑後加的規則 ----------

def test_clean_body_drops_trailing_headlines_and_promos():
    body = "\n".join(["甲乙科技今天公布營收，創下歷史新高。", "法人看好後續出貨動能，目標價上調。", "公司表示訂單能見度已到明年。",
                      "延伸閱讀：丙丁電子遭砍單，股價重挫", "戊己金控獲利創高。"])
    assert backfill.clean_body(body).split("\n")[-1] == "公司表示訂單能見度已到明年。"
    heads = "\n".join(["甲乙科技今天公布營收，創下歷史新高。", "法人看好後續出貨動能。",
                       "丙丁電子遭砍單！股價重挫跌停", "影／明星演唱會激動落淚？粉絲笑翻"])
    assert backfill.clean_body(heads) == "甲乙科技今天公布營收，創下歷史新高。\n法人看好後續出貨動能。"


def test_clean_body_keeps_normal_articles_and_short_ones():
    body = "甲乙科技推出新款 APP，下載量創新高。\n抽獎活動帶動買氣，營收成長。\n法人看好。"
    assert backfill.clean_body(body) == body             # 前兩段出現「抽獎」不算結尾廣告
    assert backfill.clean_body("只有一段沒有句號") == "只有一段沒有句號"
    assert backfill.clean_body("") == "" and backfill.clean_body(None) == ""


def test_refusing_host_is_skipped_for_good(conn):
    add_links(conn, [f"https://wall.example.com/{i}" for i in range(8)] + ["https://good.example.com/1"])

    def fetch(u):
        if "wall." in u:
            return page(u, ok=False, title="", body="", reason="網站拒絕讀取（HTTP 403），可能需要登入")
        return page(u)
    backfill.fetch_pending(conn, fetch=fetch, log=lambda m: None)
    assert backfill.refused_hosts(conn) == {"wall.example.com"}
    seen: list = []
    rep = backfill.fetch_pending(conn, fetch=lambda u: seen.append(u) or page(u), log=lambda m: None)
    assert seen == [] and rep["left"] == 0               # 第二次執行不再去碰它
    left = conn.execute("SELECT COUNT(*) FROM backfill_news WHERE status = 'skip' AND reason = ?", (backfill.REFUSED,)).fetchone()[0]
    assert left == 3
    backfill.save_links(conn, "1111", "2026-09-02", [item("https://wall.example.com/new")], backfill.refused_hosts(conn))
    assert conn.execute("SELECT status FROM backfill_news WHERE url LIKE '%/new'").fetchone()[0] == "skip"


def test_host_with_some_failures_but_more_successes_is_not_refused(conn):
    add_links(conn, [f"https://mixed.example.com/{i}" for i in range(12)])
    backfill.fetch_pending(conn, log=lambda m: None, fetch=lambda u: page(u) if int(u.rsplit("/", 1)[1]) % 2 else
                           page(u, ok=False, title="", body="", reason="網站拒絕讀取（HTTP 403）"))
    assert backfill.refused_hosts(conn) == set()


def test_apply_rules_reclassifies_rows_already_fetched(conn, monkeypatch):
    add_links(conn, ["https://promo.example.com/1"])
    backfill.fetch_pending(conn, fetch=page, log=lambda m: None)
    monkeypatch.setitem(backfill.NOT_NEWS, "promo.example.com", ("", "行銷文章，不是新聞"))
    assert backfill.apply_rules(conn) == 1
    assert conn.execute("SELECT status, reason FROM backfill_news").fetchone()[:] == ("skip", "行銷文章，不是新聞")
    assert compare.predict_rows(conn, Lexicon({"創新高": 1.0}, {}), data="backfill") == []


def test_comparison_uses_list_title_and_cleaned_body(conn):
    backfill.save_links(conn, "1111", "2026-09-01", [item("https://a.example.com/1", title="甲乙科技漲停 - 測試日報")])
    body = "甲乙科技今天股價表現強勁。\n成交量明顯放大。\n法人持續觀察。\n延伸閱讀：丙丁電子漲停"
    backfill.fetch_pending(conn, fetch=lambda u: page(u, title="證期權", body=body), log=lambda m: None)
    rows = compare.predict_rows(conn, Lexicon({"漲停": 2.0}, {}), data="backfill")
    assert [(r.code, r.title) for r in rows] == [("1111", "甲乙科技漲停")]   # 丙丁電子只出現在延伸閱讀，不算
