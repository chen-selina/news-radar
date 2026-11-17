"""時事新聞：抓「可能影響股市的時事」，用來檢驗時事新聞能不能預測大盤與產業。

既有的回補（radar/backfill.py）是向 FinMind 查「某檔個股的新聞」，沒提到那些公司的時事新聞收不到。
這裡改從鉅亨網的時事分類直接抓，含過去一年的歷史：

| 分類代號 | 鉅亨網分類 | 內容 |
|---|---|---|
| tw_macro | 台灣政經 | 政府政策、選舉、兩岸、經濟數據 |
| wd_macro | 國際政經 | 美國政策與關稅、Fed、地緣政治、各國經濟 |

資料來源是鉅亨網新聞列表的 JSON（api.cnyes.com，網站自己的新聞列表也用它），一次回傳 30 則、含全文，
所以不必再讀文章頁。規則和其他爬蟲相同：先看 robots.txt（api.cnyes.com 沒有 robots.txt＝全部允許；
news.cnyes.com 允許一般爬蟲、沒有禁止 AI 爬蟲），同網站請求間隔 1.5 秒，固定的 User-Agent，不偽裝身分。

為什麼不是其他來源（2026-11-17 查證）：中央社、自由時報的 robots.txt 都點名禁止 AI 爬蟲；
公視允許讀文章頁但禁止 /api/，也沒有歷史列表。

資料存在 events_news，和網站的 news、回補的 backfill_news 都分開：這些是事後抓的，沒有「收錄當下鎖定」的預測，
只用來研究（python -m radar.events_eval）。新聞內文是鉅亨網的著作，只留在自己電腦的資料庫，不要另外散布。

執行：python -m radar.events                    抓最近 365 天（已抓過的區段會跳過，可中斷後接續）
      python -m radar.events --from 2026-01-01 --to 2026-03-31
      python -m radar.events --status
"""
from __future__ import annotations

import argparse
import html
import re
import sqlite3
import sys
from datetime import date, datetime, time, timedelta

from . import config, db, http

SOURCE = "cnyes"
API = "https://api.cnyes.com/media/api/v1/newslist/category/{cat}"
ARTICLE_URL = "https://news.cnyes.com/news/id/{id}"
CATEGORIES = {"tw_macro": "台灣政經", "wd_macro": "國際政經"}
WINDOW_DAYS = 7          # 一次查幾天（每天幾十則，7 天約 1～6 頁）
PAGE_LIMIT = 30          # API 一頁最多 30 則
MAX_PAGES = 50           # 防止 API 異常時無限翻頁
BODY_MAX = 5000

_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(p|div|h[1-6]|li|figcaption)>|<br\s*/?>", re.I)
_FIGURE = re.compile(r"<figure.*?</figure>", re.S | re.I)


def to_text(content: str | None) -> str:
    """鉅亨網的 content 是跳脫過的 HTML：還原、去掉圖片與標籤，段落之間空一行。"""
    raw = html.unescape(content or "")
    raw = _FIGURE.sub("", raw)
    raw = _BLOCK_END.sub("\n", raw)
    text = html.unescape(_TAG.sub("", raw))
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)[:BODY_MAX]


def _epoch(d: date, end: bool = False) -> int:
    return int(datetime.combine(d, time(23, 59, 59) if end else time(0, 0), config.TAIPEI).timestamp())


def published_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, config.TAIPEI).isoformat(timespec="seconds")


def list_window(cat: str, start: date, end: date, get=None) -> list[dict]:
    """這個分類在 start～end（含）之間的所有新聞。get：測試時換成假的。"""
    get = get or http.get_json
    out, page = [], 1
    while page <= MAX_PAGES:
        url = (f"{API.format(cat=cat)}?limit={PAGE_LIMIT}&startAt={_epoch(start)}"
               f"&endAt={_epoch(end, end=True)}&page={page}")
        items = get(url)["items"]
        out.extend(items.get("data") or [])
        if page >= int(items.get("last_page") or 1):
            break
        page += 1
    return out


def save_items(conn: sqlite3.Connection, cat: str, items: list[dict]) -> int:
    """存進 events_news，回傳新增幾則。沒有編號、標題或時間的不收；同一則（來源編號或網址相同）不重複收。"""
    now, new = config.now_iso(), 0
    for it in items:
        nid, title, ts = it.get("newsId"), (it.get("title") or "").strip(), it.get("publishAt")
        if not nid or not title or not ts:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO events_news (source, source_id, category, url, title, body, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (SOURCE, str(nid), cat, ARTICLE_URL.format(id=nid), title, to_text(it.get("content")),
             published_iso(int(ts)), now))
        new += cur.rowcount
    conn.commit()
    return new


def windows(start: date, end: date, size: int = WINDOW_DAYS) -> list[tuple[date, date]]:
    out, d = [], start
    while d <= end:
        out.append((d, min(d + timedelta(days=size - 1), end)))
        d += timedelta(days=size)
    return out


def collect(conn: sqlite3.Connection, start: date, end: date, cats: list[str] | None = None,
            get=None, today: date | None = None, log=print) -> dict:
    """抓 start～end 的時事新聞。已經完整抓過的區段跳過；還沒結束的區段（含今天）每次都重抓，不記為完成。"""
    today = today or config.now_taipei().date()
    end = min(end, today)
    stats = {"windows": 0, "skipped": 0, "found": 0, "new": 0, "errors": 0}
    for cat in cats or list(CATEGORIES):
        done = {r[0] for r in conn.execute(
            "SELECT start_date FROM events_windows WHERE source = ? AND category = ?", (SOURCE, cat))}
        for a, b in windows(start, end):
            if a.isoformat() in done:
                stats["skipped"] += 1
                continue
            try:
                items = list_window(cat, a, b, get)
            except (RuntimeError, http.Blocked, KeyError, ValueError) as e:
                stats["errors"] += 1
                log(f"  {CATEGORIES.get(cat, cat)} {a}～{b}：讀取失敗，下次再試（{e}）")
                continue
            new = save_items(conn, cat, items)
            if b < today:
                conn.execute("INSERT OR REPLACE INTO events_windows VALUES (?, ?, ?, ?, ?, ?)",
                             (SOURCE, cat, a.isoformat(), b.isoformat(), len(items), config.now_iso()))
                conn.commit()
            stats["windows"] += 1
            stats["found"] += len(items)
            stats["new"] += new
            log(f"  {CATEGORIES.get(cat, cat)} {a}～{b}：{len(items)} 則，新增 {new} 則")
    return stats


def status(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT category, COUNT(*) AS n, MIN(published_at) AS first, MAX(published_at) AS last, "
        "AVG(LENGTH(body)) AS avg_len FROM events_news WHERE source = ? GROUP BY category ORDER BY category",
        (SOURCE,)).fetchall()
    return [dict(r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="抓時事新聞（鉅亨網台灣政經、國際政經），研究用")
    ap.add_argument("--days", type=int, default=365, help="抓最近幾天（預設 365）")
    ap.add_argument("--from", dest="start", help="開始日期 YYYY-MM-DD（指定時忽略 --days）")
    ap.add_argument("--to", dest="end", help="結束日期 YYYY-MM-DD（預設今天）")
    ap.add_argument("--category", action="append", choices=list(CATEGORIES), help="只抓某個分類，可重複指定")
    ap.add_argument("--status", action="store_true", help="只顯示目前收了多少")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    if not args.status:
        end = date.fromisoformat(args.end) if args.end else config.now_taipei().date()
        start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days - 1)
        print(f"抓取時事新聞 {start}～{end}（鉅亨網 {'、'.join(CATEGORIES[c] for c in args.category or CATEGORIES)}）")
        s = collect(conn, start, end, args.category)
        print(f"完成：查了 {s['windows']} 個區段（略過已抓過的 {s['skipped']} 個），"
              f"共 {s['found']} 則、新增 {s['new']} 則" + (f"；{s['errors']} 個區段失敗，下次再試" if s["errors"] else ""))
    for r in status(conn):
        print(f"{CATEGORIES.get(r['category'], r['category'])}：{r['n']} 則，{r['first'][:10]}～{r['last'][:10]}，"
              f"平均內文 {r['avg_len']:.0f} 字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
