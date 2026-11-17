"""時事新聞：內文轉純文字、翻頁、存檔去重、區段接續、大盤檢定。全部不連網（假的 API）。"""
from datetime import date, datetime, timedelta

import pytest

from radar import config, db, events
from radar.events_eval import market_test, ols_nw


@pytest.fixture
def conn():
    return db.connect(":memory:")


def ts(day: str, hhmm: str = "10:00") -> int:
    return int(datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=config.TAIPEI).timestamp())


def item(nid, title="美國宣布加徵關稅", day="2026-09-01", content="&lt;p&gt;第一段。&lt;/p&gt;"):
    return {"newsId": nid, "title": title, "publishAt": ts(day), "content": content}


def fake_api(pages: dict, calls: list | None = None):
    """pages：(分類, 開始日期) → [第 1 頁的新聞, 第 2 頁的新聞, …]"""
    def get(url):
        if calls is not None:
            calls.append(url)
        cat = url.split("/category/")[1].split("?")[0]
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        start = datetime.fromtimestamp(int(q["startAt"]), config.TAIPEI).date().isoformat()
        all_pages = pages.get((cat, start), [[]])
        page = int(q["page"])
        return {"items": {"data": all_pages[page - 1], "last_page": len(all_pages)}}
    return get


def test_content_becomes_plain_text():
    raw = ("&lt;p&gt;美國總統宣布&amp;nbsp;加徵關稅。&lt;/p&gt;\n&lt;figure&gt;&lt;img src=&quot;x.jpg&quot;&gt;"
           "&lt;figcaption&gt;圖說&lt;/figcaption&gt;&lt;/figure&gt;&lt;p&gt;市場&lt;br&gt;震盪。&lt;/p&gt;")
    assert events.to_text(raw) == "美國總統宣布\xa0加徵關稅。\n市場\n震盪。"
    assert events.to_text(None) == ""
    assert len(events.to_text("&lt;p&gt;" + "字" * 9000 + "&lt;/p&gt;")) == events.BODY_MAX


def test_time_window_uses_taipei_midnight_to_end_of_day():
    calls = []
    events.list_window("wd_macro", date(2026, 9, 1), date(2026, 9, 7), fake_api({}, calls))
    q = dict(p.split("=") for p in calls[0].split("?")[1].split("&"))
    assert datetime.fromtimestamp(int(q["startAt"]), config.TAIPEI).isoformat() == "2026-09-01T00:00:00+08:00"
    assert datetime.fromtimestamp(int(q["endAt"]), config.TAIPEI).isoformat() == "2026-09-07T23:59:59+08:00"


def test_list_window_follows_pages():
    api = fake_api({("wd_macro", "2026-09-01"): [[item(1), item(2)], [item(3)]]})
    assert [x["newsId"] for x in events.list_window("wd_macro", date(2026, 9, 1), date(2026, 9, 7), api)] == [1, 2, 3]


def test_save_keeps_link_and_skips_duplicates_and_bad_items(conn):
    assert events.save_items(conn, "wd_macro", [item(1), item(1), {"newsId": 2, "title": ""}, item(3)]) == 2
    r = conn.execute("SELECT * FROM events_news WHERE source_id = '1'").fetchone()
    assert r["url"] == "https://news.cnyes.com/news/id/1"
    assert r["published_at"] == "2026-09-01T10:00:00+08:00" and r["body"] == "第一段。"
    assert events.save_items(conn, "wd_macro", [item(1)]) == 0


def test_windows_cover_range_without_gaps():
    w = events.windows(date(2026, 9, 1), date(2026, 9, 16), 7)
    assert w == [(date(2026, 9, 1), date(2026, 9, 7)), (date(2026, 9, 8), date(2026, 9, 14)),
                 (date(2026, 9, 15), date(2026, 9, 16))]


def test_collect_skips_finished_windows_but_refetches_current_one(conn):
    pages = {("tw_macro", "2026-09-01"): [[item(1, day="2026-09-02")]],
             ("tw_macro", "2026-09-08"): [[item(2, day="2026-09-09")]]}
    calls = []
    s = events.collect(conn, date(2026, 9, 1), date(2026, 9, 14), ["tw_macro"], fake_api(pages, calls),
                       today=date(2026, 9, 10), log=lambda *_: None)
    assert s["new"] == 2 and len(calls) == 2
    # 第一段已經過去，記為完成；第二段含「今天」，不記
    assert [r[0] for r in conn.execute("SELECT start_date FROM events_windows")] == ["2026-09-01"]
    calls.clear()
    events.collect(conn, date(2026, 9, 1), date(2026, 9, 14), ["tw_macro"], fake_api(pages, calls),
                   today=date(2026, 9, 10), log=lambda *_: None)
    assert len(calls) == 1 and "startAt" in calls[0]


def test_collect_keeps_going_after_a_failed_window(conn):
    def get(url):
        raise RuntimeError("連不上")
    s = events.collect(conn, date(2026, 9, 1), date(2026, 9, 14), ["tw_macro"], get,
                       today=date(2026, 10, 1), log=lambda *_: None)
    assert s["errors"] == 2
    assert conn.execute("SELECT COUNT(*) FROM events_windows").fetchone()[0] == 0   # 失敗的不記為完成


def test_ols_recovers_slope():
    xs = [float(i % 7) for i in range(60)]
    ys = [0.5 * x + (0.01 if i % 2 else -0.01) for i, x in enumerate(xs)]
    r = ols_nw(ys, [xs])[0]
    assert r["coef"] == pytest.approx(0.5, abs=1e-3) and r["p"] < 0.001


def test_market_test_hit_rates_against_index():
    days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(80)]
    index, level = {}, 100.0
    daily = {}
    for i, d in enumerate(days):
        index[d] = level
        up = i % 3 != 0
        daily[d] = [1.0 if up else -1.0]            # 情緒說漲的日子，之後一天真的漲
        level *= 1.01 if up else 0.99
    r = market_test(daily, days, index, 1)
    assert r["days"] >= 70 and r["hit_vs_zero"] == 1.0 and r["hit_vs_mean"] == 1.0
    assert r["alone"]["coef"] > 0 and r["alone"]["p"] < 0.01
    assert market_test({days[1]: [1.0]}, days, index, 1) is None    # 天數太少不檢定
