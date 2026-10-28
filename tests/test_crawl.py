"""爬蟲：robots.txt、網址整理、列表與 RSS 解析、內文擷取、存檔去重。

網頁與 RSS 測試資料是依各網站結構自行編寫的範例（不複製真實新聞內容）；
robots.txt 是各網站的真實檔案（tests/fixtures/robots）。全部不連網。
"""
import base64
from pathlib import Path

import pytest

from radar import db, http
from radar.crawl import run, store, urls
from radar.crawl.extract import clean_title, extract, to_taipei_iso
from radar.crawl.models import ListItem
from radar.crawl.robots import Robots
from radar.crawl.sources import bw, cw, google, udn, yahoo

ROBOTS = Path(__file__).parent / "fixtures" / "robots"
UA = "TWNewsRadar/0.1 (personal research project)"


def robots(name):
    return Robots((ROBOTS / f"{name}.txt").read_text(encoding="utf-8"), UA)


# ---------- robots.txt ----------

def test_robots_real_sites():
    assert not robots("google").allowed("/rss/search?q=台股")
    assert robots("google").allowed("/")              # Allow: /$
    assert not robots("google").allowed("/rss/articles/CBMi")
    assert robots("udn").allowed("/money/story/5607/9840550")
    assert robots("udn").allowed("/rssfeed/news/1001/5590/5607?ch=money")
    assert robots("cw").allowed("/article/5143447")
    assert not robots("cw").allowed("/search?q=台積電")
    assert robots("yahoo").allowed("/rss?category=tw-market")
    assert not robots("yahoo").allowed("/api/xyz")


def test_robots_rules():
    r = Robots("User-agent: *\nDisallow: /a\nAllow: /a/ok$\nDisallow: /*.pdf$\n", UA)
    assert not r.allowed("/a/x") and r.allowed("/a/ok") and not r.allowed("/a/ok/more")
    assert not r.allowed("/doc/file.pdf") and r.allowed("/doc/file.pdf?x=1")
    named = Robots("User-agent: *\nDisallow: /\n\nUser-agent: TWNewsRadar\nAllow: /\n", UA)
    assert named.allowed("/anything")  # 點名我們的規則優先於 *
    assert Robots("", UA).allowed("/x")  # 沒有 robots.txt＝全部允許


def test_http_get_refuses_blocked_url(monkeypatch):
    monkeypatch.setattr("radar.crawl.robots.allowed", lambda url: False)
    with pytest.raises(http.Blocked):
        http.get("https://example.com/x")


# ---------- 網址 ----------

def test_canonical_strips_tracking():
    assert urls.canonical("https://www.cw.com.tw/article/5143278?rec=es") == "https://www.cw.com.tw/article/5143278"
    assert urls.canonical("https://a.com/x?id=3&utm_source=fb#top") == "https://a.com/x?id=3"
    assert urls.canonical("HTTPS://Money.UDN.com/money/story/1") == "https://money.udn.com/money/story/1"


def test_source_of():
    assert urls.source_of("https://money.udn.com/money/story/5607/1") == "udn"
    assert urls.source_of("https://tw.news.yahoo.com/abc.html") == "yahoo"
    assert urls.source_of("https://www.businessweekly.com.tw/business/blog/1") == "bw"
    assert urls.source_of("https://www.cw.com.tw/article/1") == "cw"
    assert urls.source_of("https://ec.ltn.com.tw/article/1") is None
    assert not urls.is_http("") and not urls.is_http("javascript:void(0)")


# ---------- RSS 與列表頁 ----------

RSS = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>t</title>
<item><title><![CDATA[範例公司營收創新高]]></title><link>https://tw.news.yahoo.com/sample-1.html</link>
<pubDate>Tue, 27 Oct 2026 16:04:00 GMT</pubDate><description><![CDATA[<p>這是摘要&amp;說明</p>]]></description></item>
<item><title>沒有連結</title><link></link></item>
</channel></rss>"""


def test_rss_feeds():
    items = yahoo.parse_feed(RSS, 10)
    assert len(items) == 1  # 沒有連結的略過
    it = items[0]
    assert it.title == "範例公司營收創新高" and it.summary == "這是摘要&說明"
    assert it.published_at == "2026-10-28T00:04:00+08:00"  # GMT 轉台北時間
    assert udn.parse_feed(RSS, 10)[0].source == "udn"


def test_bw_list_only_regular_articles():
    html = """<h2 class="news-item__title"><a href="/business/blog/3000001">財經文章一</a></h2>
    <a href="/business/blog/3000001"><img alt=""></a>
    <h2><a href="/business/indep/1000001">廣編稿</a></h2>
    <a href="/international/blog/3000002">國際文章</a>"""
    items = bw.parse_list(html, 10)
    assert [(i.url, i.title) for i in items] == [("https://www.businessweekly.com.tw/business/blog/3000001", "財經文章一")]


def test_cw_list_skips_recommendations():
    html = """<a href="https://www.cw.com.tw/article/5000001?rec=es">推薦的舊文章</a>
    <a class="card__item" href="https://www.cw.com.tw/article/5000002"><img alt=""></a>
    <h3><a href="https://www.cw.com.tw/article/5000002">今日新文章</a></h3>"""
    assert [(i.url, i.title) for i in cw.parse_list(html, 10)] == [("https://www.cw.com.tw/article/5000002", "今日新文章")]


def _old_google_token(url: str) -> str:
    raw = b'\x08\x13"' + bytes([len(url)]) + url.encode() + b"\xd2\x01\x00"
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_google_url_decoding():
    tok = _old_google_token("https://ec.ltn.com.tw/article/breakingnews/5520293")
    assert google.decode_google_url(f"https://news.google.com/rss/articles/{tok}?oc=5") == \
        "https://ec.ltn.com.tw/article/breakingnews/5520293"
    new = base64.urlsafe_b64encode(b'\x08\x13"KAU_yqLOhpxIWlu9oXQ3zDRjfc').decode().rstrip("=")
    assert google.decode_google_url(f"https://news.google.com/rss/articles/{new}") is None


def test_google_feed_keeps_both_urls_and_outlet():
    tok = _old_google_token("https://ec.ltn.com.tw/article/1")
    xml = f"""<rss><channel><item><title>南亞科跌停 - 自由財經</title>
    <link>https://news.google.com/rss/articles/{tok}?oc=5</link><pubDate>Tue, 28 Jul 2026 06:53:00 GMT</pubDate>
    <source url="https://ec.ltn.com.tw">自由財經</source></item></channel></rss>"""
    items, skipped = google.parse_feed(xml, 10)
    it = items[0]
    assert (it.url, it.title, it.via_outlet, skipped) == ("https://ec.ltn.com.tw/article/1", "南亞科跌停", "自由財經", 0)
    assert it.google_url.startswith("https://news.google.com/rss/articles/")


# ---------- 文章頁 ----------

PARA = "這是一段測試用的新聞內文，內容描述某家公司的營收表現與法人看法，長度足夠被視為正文段落。"
ARTICLE = f"""<html><head><title>範例標題 - 商周</title>
<meta property="og:title" content="範例標題｜天下雜誌"><meta property="og:description" content="網頁摘要">
<script type="application/ld+json">{{"@type":"NewsArticle","headline":"範例新聞標題很長一點","datePublished":"2026-10-27T20:13:40+08:00",
"publisher":{{"name":"商業周刊"}},"isAccessibleForFree":"false"}}</script></head>
<body><nav><p>{PARA}導覽</p></nav><div class="article-body"><p>{PARA}</p><p>延伸閱讀：別篇文章的標題文字很長很長</p><p>{PARA}第二段</p></div>
<footer><p>{PARA}頁尾</p></footer></body></html>"""


def test_extract_article():
    p = extract(ARTICLE, "bw")
    assert p.title == "範例新聞標題很長一點"
    assert p.published_at == "2026-10-27T20:13:40+08:00"
    assert p.paywalled and not p.partial
    assert p.body == f"{PARA}\n{PARA}第二段"  # 只取內文區塊，去掉延伸閱讀、導覽、頁尾


def test_extract_falls_back_to_summary():
    p = extract('<html><head><meta property="og:description" content="只有摘要可以讀"></head><body><p>登入</p></body></html>', "cw")
    assert p.partial and p.body == "只有摘要可以讀"


def test_extract_yahoo_provider():
    html = ('<script type="application/ld+json">{"@type":"NewsArticle","headline":"轉載新聞的標題文字",'
            '"provider":{"name":"三立新聞網"},"publisher":{"name":"Yahoo奇摩新聞"}}</script>'
            f'<div class="caas-body"><p>{PARA}</p><p>{PARA}二</p></div>')
    assert extract(html, "yahoo").via_outlet == "三立新聞網"


def test_cjk_line_breaks_are_joined():
    html = ('<div class="caas-body"><p>金融總會建議擴大債券ETF免徵證交稅範\n圍，營收\n成長可期，'
            '外資 Morgan Stanley 看好台股 46000 點。</p><p>' + PARA + '</p></div>')
    first = extract(html, "yahoo").body.split("\n")[0]
    assert "範圍" in first and "營收成長" in first
    assert "Morgan Stanley 看好" in first and "46000 點" in first  # 英文與數字旁的空白保留


def test_disclaimers_are_dropped():
    html = (f'<div class="caas-body"><p>{PARA}</p><p>三立新聞網提醒您：</p>'
            '<p>內容僅供參考，投資人於決策時應審慎評估風險，並就投資結果自行負責。</p>'
            f'<p>{PARA}二</p><p>（本文轉載自「常春月刊」，版權所有，本刊圖文非經同意不得轉載或公開傳播。）</p></div>')
    body = extract(html, "yahoo").body
    assert "審慎" not in body and "版權所有" not in body and body.endswith("二")


def test_outlet_domain_suffix_removed():
    html = ('<script type="application/ld+json">{"@type":"NewsArticle","provider":{"name":"三立新聞網 setn.com"}}</script>'
            f'<div class="caas-body"><p>{PARA}</p><p>{PARA}二</p></div>')
    assert extract(html, "yahoo").via_outlet == "三立新聞網"


def test_time_and_title_helpers():
    assert to_taipei_iso("2026-10-27 08:30:00") == "2026-10-27T08:30:00+08:00"
    assert to_taipei_iso("2026-10-27T00:30:00Z") == "2026-10-27T08:30:00+08:00"
    assert to_taipei_iso("亂寫") is None
    assert clean_title("航空股布局時機到了？法人看好 | 經濟日報") == "航空股布局時機到了？法人看好"
    assert clean_title("短 - 商周") == "短 - 商周"


# ---------- 組合與存檔 ----------

def test_build_article_prefers_page_then_summary_then_title():
    item = ListItem(url="https://tw.news.yahoo.com/a.html", source="yahoo", title="列表標題", summary="RSS 摘要文字",
                    published_at="2026-10-27T10:00:00+08:00")
    a = run.build_article(item, ARTICLE)
    assert a.body_from == "page" and a.title == "範例新聞標題很長一點" and a.published_at == item.published_at
    b = run.build_article(item, None)  # 文章頁讀不到
    assert (b.body_from, b.body, b.title) == ("summary", "RSS 摘要文字", "列表標題")
    c = run.build_article(ListItem(url="https://x.com/1", source="bw", title="只有標題"), None)
    assert (c.body_from, c.body) == ("title", "")


def test_via_outlet_only_for_aggregators():
    html = ('<script type="application/ld+json">{"@type":"NewsArticle","publisher":{"name":"商業周刊"}}</script>'
            f'<div class="article-body"><p>{PARA}</p><p>{PARA}二</p></div>')
    assert run.build_article(ListItem(url="https://b.com/1", source="bw", title="t"), html).via_outlet is None


@pytest.fixture
def conn():
    return db.connect(":memory:")


def _fake_source(monkeypatch, items, pages):
    monkeypatch.setitem(run.FETCHERS, "cw", type("F", (), {"list_items": staticmethod(lambda limit: items)}))
    monkeypatch.setattr(run.http, "get", lambda url: type("R", (), {"text": pages[url]})())


def test_crawl_dedupes_by_url_and_title(conn, monkeypatch):
    items = [ListItem(url="https://www.cw.com.tw/article/1?rec=es", source="cw", title="同一則新聞！"),
             ListItem(url="https://www.cw.com.tw/article/1", source="cw", title="同一則新聞！"),
             ListItem(url="https://www.cw.com.tw/article/2", source="cw", title="同一則 新聞"),
             ListItem(url="", source="cw", title="沒網址"),
             ListItem(url="https://www.cw.com.tw/article/3", source="cw", title="另一則")]
    pages = {f"https://www.cw.com.tw/article/{i}": f'<div class="article__content"><p>{PARA}{i}</p><p>{PARA}</p></div>'
             for i in (1, 2, 3)}
    _fake_source(monkeypatch, items, pages)
    rep = run.crawl_source(conn, "cw", 10, 3, log=lambda *_: None)
    assert (rep.saved, rep.duplicate, rep.no_url) == (2, 2, 1)
    rows = conn.execute("SELECT url, title FROM news ORDER BY id").fetchall()
    assert [r["url"] for r in rows] == ["https://www.cw.com.tw/article/1", "https://www.cw.com.tw/article/3"]
    rep2 = run.crawl_source(conn, "cw", 10, 3, log=lambda *_: None)  # 再跑一次不會重複收錄
    assert rep2.saved == 0 and conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 2


def test_transient_failure_is_retried_later(conn, monkeypatch):
    item = ListItem(url="https://www.cw.com.tw/article/9", source="cw", title="暫時讀不到")
    monkeypatch.setitem(run.FETCHERS, "cw", type("F", (), {"list_items": staticmethod(lambda limit: [item])}))

    def down(url):
        raise RuntimeError("讀取失敗：連線中斷")
    monkeypatch.setattr(run.http, "get", down)
    rep = run.crawl_source(conn, "cw", 10, 3, log=lambda *_: None)
    assert (rep.saved, rep.retry_later) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 0  # 沒存，下次會重抓


def test_crawl_reports_blocked_source(conn, monkeypatch):
    def blocked(limit):
        raise http.Blocked("robots.txt 不允許讀取")
    monkeypatch.setitem(run.FETCHERS, "google", type("F", (), {"list_items": staticmethod(blocked)}))
    rep = run.crawl_source(conn, "google", 10, 3)
    assert rep.error and rep.saved == 0


def test_title_key():
    assert store.title_key("台積電 創新高！") == store.title_key("台積電創新高")


def test_migration_from_v2(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    old = sqlite3.connect(p)
    old.executescript(db.SCHEMA.replace(",\n    body_from    TEXT NOT NULL DEFAULT 'page'  -- page＝文章內文；summary＝只有摘要；title＝只有標題；manual＝使用者貼上或修改", ""))
    old.execute("INSERT INTO news (url, source, title, fetched_at) VALUES ('https://a.com', 'cw', 't', 'now')")
    old.execute("PRAGMA user_version = 2")
    old.commit()
    old.close()
    conn = db.connect(p)
    assert conn.execute("SELECT body_from FROM news").fetchone()[0] == "page"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
