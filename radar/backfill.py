"""回補歷史新聞：把過去幾個月的新聞（含內文）抓回來，當訓練與回測的資料。

平常的爬蟲只讀各網站的「最新新聞」列表，往回翻不到舊新聞。這裡分兩步：
1. 查網址：向 FinMind（開放的台股資料平台）查「某檔個股、某一天」有哪些新聞，拿到標題與原文網址。
2. 抓內文：逐篇讀原文網頁，規則和平常的爬蟲相同：先看該網站的 robots.txt、同網站請求有間隔、不偽裝身分。

資料存在 backfill_news（和網站用的 news 分開）。這些新聞是事後補抓的，沒有「收錄當下鎖定」的預測，
只能拿來訓練與回測（python -m radar.compare --backfill），不會出現在網站上。

執行：python -m radar.backfill --top 20 --days 90     （市值前 20 檔、最近 90 天；可中斷，下次接著做）
      python -m radar.backfill --status               （看目前進度與各網站的成功率）
      python -m radar.backfill --codes 2330,2454 --from 2026-09-01 --to 2026-09-30
      python -m radar.backfill --links-only           （只查網址）
      python -m radar.backfill --fetch-only --limit 200 （只抓內文，最多 200 篇）

限制：
- FinMind 一次只能查一檔一天，免費每小時 300 次；到上限時先去抓內文，再等下一個小時。
  到 https://finmindtrade.com 註冊後把金鑰放進環境變數 FINMIND_TOKEN，上限提高到每小時 600 次。
- 2025 年下半年以前的網址是 Google新聞的轉址連結，Google 的 robots.txt 不允許程式讀取，一律略過。
- 討論區貼文、個股資訊頁不是新聞，略過。
- 某個網站連續失敗 5 次，這次執行就不再讀它；確認是擋程式（HTTP 403）的網站，之後都略過、不再嘗試。
- 內文後面夾帶的「延伸閱讀」標題與站內廣告，分析時用 clean_body() 去掉（資料庫保留原樣）。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from . import config, db, http
from .crawl import single, urls

API = "https://api.finmindtrade.com/api/v4/data"
LIMIT_WAIT = 10 * 60        # 到 FinMind 上限後，隔多久再試一次（秒）
HOST_FAIL_LIMIT = 5         # 同一個網站連續失敗幾次就先不讀
# 不是新聞的頁面：網域（含子網域）＋路徑開頭。空字串＝整個網站都略過。
NOT_NEWS = {
    "news.google.com": ("", "Google新聞的轉址連結（robots.txt 不允許程式讀取）"),
    "www.cmoney.tw": ("", "討論區貼文或個股資訊頁，不是新聞"),
    "www.ptt.cc": ("", "討論區貼文"), "www.dcard.tw": ("", "討論區貼文"), "www.mobile01.com": ("", "討論區貼文"),
    "www.youtube.com": ("", "影片"), "youtu.be": ("", "影片"), "www.facebook.com": ("", "社群貼文"),
    "www.sinotrade.com.tw": ("", "券商的教學與行銷文章，不是新聞"),
}
REFUSED = "這個網站拒絕程式讀取（HTTP 403），不再嘗試"
# 內文後面常接著「延伸閱讀」的其他新聞標題與站內廣告；出現這些字樣的段落起，後面都不算內文
_TAIL_START = re.compile(r"延伸閱讀|相關新聞|相關報導|推薦閱讀|熱門新聞|更多.{0,8}(?:新聞|報導|文章)|看更多|"
                         r"上一篇|下一篇|下載\s*APP|用\s*APP\s*看新聞|新聞雲\s*APP|東森幣|抽獎|簽到|禮券|即將截止|即日起至|點我|按我|加入.{0,6}(?:LINE|粉絲)")
_SENTENCE = re.compile(r"。")   # 新聞內文的段落幾乎都有句號；標題用的是驚嘆號、問號或沒有標點


class RateLimited(Exception):
    """FinMind 這個小時的查詢次數用完了。"""


def to_taipei(utc_text: str) -> str | None:
    """FinMind 的時間是 UTC（09:00 開盤的快訊標成 01:02），轉成台北時間 ISO 8601。"""
    try:
        t = datetime.strptime(utc_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return t.astimezone(config.TAIPEI).isoformat(timespec="seconds")


def skip_reason(url: str) -> str | None:
    """這個網址不抓的原因；None＝可以抓。"""
    if not urls.is_http(url):
        return "網址格式不對"
    u = urlsplit(url)
    rule = NOT_NEWS.get((u.hostname or "").lower())
    if rule and u.path.startswith("/" + rule[0].lstrip("/")):
        return rule[1]
    return None


def clean_body(body: str) -> str:
    """去掉內文後面夾帶的其他新聞標題與站內廣告（各網站用通用規則擷取時常一起被抓進來）。

    1. 第 3 段以後，出現「延伸閱讀」「上一篇」「下載APP」這類字樣的段落起，後面全部去掉。
    2. 結尾連續幾段沒有句號「。」的段落（多半是其他新聞的標題）去掉。
    只在分析時套用，資料庫裡保留原樣，規則可以再調整。
    """
    paras = [p for p in (body or "").split("\n") if p.strip()]
    for i, p in enumerate(paras):
        if i >= 2 and _TAIL_START.search(p):
            paras = paras[:i]
            break
    while len(paras) > 1 and not _SENTENCE.search(paras[-1]):
        paras.pop()
    return "\n".join(paras)


def refused_hosts(conn: sqlite3.Connection) -> set[str]:
    """擋程式的網站：被拒絕（HTTP 401／403）至少 5 篇，而且比讀到的還多。"""
    stat: dict[str, list[int]] = {}
    for r in conn.execute("SELECT url, status, reason FROM backfill_news WHERE status IN ('ok', 'partial', 'fail')"):
        s = stat.setdefault(urlsplit(r["url"]).hostname or "", [0, 0])
        if r["status"] == "fail":
            s[1] += "HTTP 403" in (r["reason"] or "") or "HTTP 401" in (r["reason"] or "")
        else:
            s[0] += 1
    return {h for h, (ok, no) in stat.items() if no >= HOST_FAIL_LIMIT and no > ok}


def apply_rules(conn: sqlite3.Connection) -> int:
    """用目前的規則重新檢查還沒讀與已讀的網址：不是新聞的、來自擋程式網站的，改標成略過。回傳改了幾則。"""
    refused = refused_hosts(conn)
    changed = 0
    with conn:
        for r in conn.execute("SELECT id, url, status FROM backfill_news WHERE status != 'skip'").fetchall():
            why = skip_reason(r["url"])
            if not why and r["status"] == "pending" and (urlsplit(r["url"]).hostname or "") in refused:
                why = REFUSED
            if why:
                conn.execute("UPDATE backfill_news SET status = 'skip', reason = ? WHERE id = ?", (why, r["id"]))
                changed += 1
    return changed


def clean_list_title(title: str, outlet: str | None) -> str:
    """FinMind 的標題結尾帶「 - 媒體名稱」，拿掉。"""
    title = (title or "").strip()
    if outlet and title.endswith(" - " + outlet):
        title = title[: -len(outlet) - 3].rstrip()
    return title


# ---------- 第一步：查網址 ----------

def finmind_news(code: str, day: str, token: str | None = None) -> list[dict]:
    """FinMind 上這檔個股這一天的新聞：[{date, stock_id, title, source, link}]。"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = http.get(API, params={"dataset": "TaiwanStockNews", "data_id": code, "start_date": day},
                     headers=headers, retries=1)
    except RuntimeError as e:
        cause = e.__cause__
        if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in (402, 429):
            raise RateLimited("FinMind 這個小時的查詢次數用完了") from None
        raise
    data = r.json()
    if data.get("status") not in (200, None) and not data.get("data"):
        raise RuntimeError(f"FinMind 回應：{data.get('msg')}")
    return data.get("data") or []


def save_links(conn: sqlite3.Connection, code: str, day: str, items: list[dict],
               refused: set[str] | frozenset = frozenset()) -> int:
    """把查到的新聞網址記下來（同一個網址只記一次，但會補上它也屬於這檔個股），回傳新增幾則。"""
    new = 0
    with conn:
        for it in items:
            raw = (it.get("link") or "").strip()
            if not raw:
                continue
            url = urls.canonical(raw) if urls.is_http(raw) else raw
            outlet = (it.get("source") or "").strip() or None
            row = conn.execute("SELECT id FROM backfill_news WHERE url = ?", (url,)).fetchone()
            if row:
                nid = row["id"]
            else:
                why = skip_reason(url) or (REFUSED if (urlsplit(url).hostname or "") in refused else None)
                nid = conn.execute(
                    "INSERT INTO backfill_news (url, outlet, list_title, listed_at, status, reason) VALUES (?, ?, ?, ?, ?, ?)",
                    (url, outlet, clean_list_title(it.get("title"), outlet), to_taipei(it.get("date")),
                     "skip" if why else "pending", why)).lastrowid
                new += 1
            conn.execute("INSERT OR IGNORE INTO backfill_tags (news_id, code) VALUES (?, ?)", (nid, code))
        conn.execute("INSERT OR REPLACE INTO backfill_days (code, date, found, fetched_at) VALUES (?, ?, ?, ?)",
                     (code, day, len(items), config.now_iso()))
    return new


def todo_days(conn: sqlite3.Connection, codes: list[str], start: date, end: date) -> list[tuple[str, str]]:
    """還沒查過的（個股, 日期），由新到舊。今天的新聞還會增加，不記成查過，所以每次都會重查。"""
    done = {(r["code"], r["date"]) for r in conn.execute("SELECT code, date FROM backfill_days")}
    out = []
    d = end
    while d >= start:
        out += [(c, d.isoformat()) for c in codes if (c, d.isoformat()) not in done]
        d -= timedelta(days=1)
    return out


def collect(conn: sqlite3.Connection, codes: list[str], start: date, end: date, token: str | None = None,
            log=print, fetch=None) -> dict:
    """查網址，直到全部查完或 FinMind 到上限。回傳 {queried, new, left, limited}。"""
    fetch = fetch or finmind_news
    todo = todo_days(conn, codes, start, end)
    today = config.now_taipei().date().isoformat()
    rep = {"queried": 0, "new": 0, "left": len(todo), "limited": False}
    refused = refused_hosts(conn)
    for i, (code, day) in enumerate(todo):
        try:
            items = fetch(code, day, token)
        except RateLimited:
            rep["limited"] = True
            break
        except (RuntimeError, ValueError, http.Blocked) as e:
            log(f"  {code} {day} 查詢失敗，下次再試：{e}")
            rep["left"] -= 1
            continue
        rep["new"] += save_links(conn, code, day, items, refused)
        if day >= today:   # 今天的新聞還會變多：網址先存，但不記成「查過」
            with conn:
                conn.execute("DELETE FROM backfill_days WHERE code = ? AND date = ?", (code, day))
        rep["queried"] += 1
        rep["left"] -= 1
        if rep["queried"] % 50 == 0:
            log(f"  已查 {rep['queried']} 個（個股, 日期），新增 {rep['new']} 則網址，還剩 {len(todo) - i - 1} 個")
    return rep


# ---------- 第二步：抓內文 ----------

def fetch_pending(conn: sqlite3.Connection, limit: int | None = None, log=print, fetch=None) -> dict:
    """讀還沒抓的新聞內文。回傳 {ok, partial, fail, skipped_hosts, left}。每篇各自存檔，可隨時中斷。"""
    fetch = fetch or single.fetch
    apply_rules(conn)   # 上次確認擋程式的網站、後來才列為「不是新聞」的網址，先標成略過
    rows = conn.execute("SELECT id, url FROM backfill_news WHERE status = 'pending' ORDER BY listed_at DESC, id").fetchall()
    rep = {"ok": 0, "partial": 0, "fail": 0, "skipped_hosts": {}, "left": 0}
    streak: dict[str, int] = {}
    done = 0
    for r in rows:
        if limit is not None and done >= limit:
            break
        host = urlsplit(r["url"]).hostname or ""
        if streak.get(host, 0) >= HOST_FAIL_LIMIT:
            rep["skipped_hosts"][host] = rep["skipped_hosts"].get(host, 0) + 1
            continue
        try:
            f = fetch(r["url"])
            status = "ok" if f.ok and not f.partial else "partial" if f.ok else "fail"
            reason, title, body, published = f.reason, f.title, f.body, f.published_at
        except single.BadUrl as e:
            status, reason, title, body, published = "fail", str(e), "", "", None
        except Exception as e:  # noqa: BLE001 —— 一篇出錯不影響其他篇
            status, reason, title, body, published = "fail", f"讀取時發生錯誤：{e}", "", "", None
        with conn:
            conn.execute("UPDATE backfill_news SET status = ?, reason = ?, title = ?, body = ?, "
                         "published_at = COALESCE(?, listed_at), fetched_at = ? WHERE id = ?",
                         (status, reason, title, body, published, config.now_iso(), r["id"]))
        rep[status] += 1
        streak[host] = streak.get(host, 0) + 1 if status == "fail" else 0
        if streak[host] == HOST_FAIL_LIMIT:
            log(f"  {host} 連續 {HOST_FAIL_LIMIT} 篇讀不到（{reason}），這次先不讀這個網站")
        done += 1
        if done % 25 == 0:
            log(f"  已讀 {done} 篇：有內文 {rep['ok']}、只有標題或摘要 {rep['partial']}、讀不到 {rep['fail']}")
    rep["left"] = conn.execute("SELECT COUNT(*) FROM backfill_news WHERE status = 'pending'").fetchone()[0]
    return rep


# ---------- 進度 ----------

def status(conn: sqlite3.Connection) -> dict:
    by = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM backfill_news GROUP BY status")}
    hosts: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT url, status, LENGTH(body) AS n FROM backfill_news WHERE status != 'skip'"):
        h = hosts.setdefault(urlsplit(r["url"]).hostname or "?", {"ok": 0, "partial": 0, "fail": 0, "pending": 0, "chars": 0})
        h[r["status"]] += 1
        h["chars"] += r["n"] if r["status"] == "ok" else 0
    skips = {r["reason"]: r["n"] for r in conn.execute(
        "SELECT reason, COUNT(*) AS n FROM backfill_news WHERE status = 'skip' GROUP BY reason")}
    fails = {r["reason"]: r["n"] for r in conn.execute(
        "SELECT reason, COUNT(*) AS n FROM backfill_news WHERE status = 'fail' GROUP BY reason ORDER BY n DESC LIMIT 5")}
    span = conn.execute("SELECT MIN(date), MAX(date), COUNT(*), COUNT(DISTINCT code) FROM backfill_days").fetchone()
    return {"by_status": by, "hosts": hosts, "skips": skips, "fails": fails,
            "days": {"first": span[0], "last": span[1], "queried": span[2], "codes": span[3]}}


def print_status(conn: sqlite3.Connection, log=print) -> None:
    s = status(conn)
    by, d = s["by_status"], s["days"]
    if not d["queried"]:
        log("還沒有回補資料。")
        return
    log(f"已查 {d['codes']} 檔個股、{d['queried']} 個（個股, 日期），範圍 {d['first']} ～ {d['last']}")
    log(f"新聞網址：有內文 {by.get('ok', 0)}、只有標題或摘要 {by.get('partial', 0)}、讀不到 {by.get('fail', 0)}、"
        f"還沒讀 {by.get('pending', 0)}、略過 {by.get('skip', 0)}")
    for reason, n in s["skips"].items():
        log(f"  略過 {n} 則：{reason}")
    if s["hosts"]:
        log("各網站（有內文／只有標題或摘要／讀不到／還沒讀，平均內文字數）：")
        for host, h in sorted(s["hosts"].items(), key=lambda kv: -sum(v for k, v in kv[1].items() if k != "chars")):
            avg = h["chars"] // h["ok"] if h["ok"] else 0
            log(f"  {host:<28} {h['ok']:>5}／{h['partial']:>4}／{h['fail']:>4}／{h['pending']:>5}　{avg:>5} 字")
    for reason, n in s["fails"].items():
        log(f"  讀不到 {n} 則：{reason}")


# ---------- 指令 ----------

def top_codes(conn: sqlite3.Connection, n: int) -> list[str]:
    return [r["code"] for r in conn.execute(
        "SELECT code FROM stocks ORDER BY market_cap IS NULL, market_cap DESC, code LIMIT ?", (n,))]


def run(conn: sqlite3.Connection, codes: list[str], start: date, end: date, token: str | None = None,
        links: bool = True, bodies: bool = True, limit: int | None = None, log=print, sleep=time.sleep) -> None:
    """查網址與抓內文交替進行：FinMind 到上限時先抓內文，內文抓完還沒到下一個小時才等待。"""
    while True:
        rep = {"limited": False, "left": 0}
        if links:
            log(f"查網址：{len(codes)} 檔個股，{start} ～ {end}")
            rep = collect(conn, codes, start, end, token, log)
            log(f"  這一輪查了 {rep['queried']} 個（個股, 日期），新增 {rep['new']} 則網址"
                + ("；FinMind 這個小時的次數用完了" if rep["limited"] else ""))
        if bodies:
            log("抓內文")
            got = fetch_pending(conn, limit, log)
            log(f"  有內文 {got['ok']}、只有標題或摘要 {got['partial']}、讀不到 {got['fail']}，還沒讀 {got['left']}")
            for host, n in got["skipped_hosts"].items():
                log(f"  {host} 這次先不讀，留下 {n} 則")
            if limit is not None:
                break
        if not (links and rep["limited"]):
            break
        log(f"等 {LIMIT_WAIT // 60} 分鐘後繼續查網址（按 Ctrl+C 可以先停，下次執行會接著做）")
        sleep(LIMIT_WAIT)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回補歷史新聞（查網址＋抓內文），可中斷後接續")
    ap.add_argument("--top", type=int, default=20, help="市值前幾檔（預設 20）")
    ap.add_argument("--codes", help="改成指定個股代號，逗號分隔，例如 2330,2454")
    ap.add_argument("--days", type=int, default=90, help="往回幾天（預設 90）")
    ap.add_argument("--from", dest="start", type=date.fromisoformat, help="起始日 YYYY-MM-DD（取代 --days）")
    ap.add_argument("--to", dest="end", type=date.fromisoformat, help="結束日 YYYY-MM-DD（預設今天）")
    ap.add_argument("--links-only", action="store_true", help="只查網址，不抓內文")
    ap.add_argument("--fetch-only", action="store_true", help="只抓內文，不查新的網址")
    ap.add_argument("--limit", type=int, help="這次最多抓幾篇內文")
    ap.add_argument("--status", action="store_true", help="只顯示目前進度")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    if args.status:
        apply_rules(conn)
        print_status(conn)
        return 0
    end = args.end or config.now_taipei().date()
    start = args.start or end - timedelta(days=args.days)
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else top_codes(conn, args.top)
    if not codes:
        print("資料庫沒有公司資料，請先執行 python -m radar.stocks.load")
        return 1
    token = os.environ.get("FINMIND_TOKEN") or None
    try:
        run(conn, codes, start, end, token, links=not args.fetch_only, bodies=not args.links_only, limit=args.limit)
    except KeyboardInterrupt:
        print("\n已中斷。進度都存好了，下次執行會接著做。")
    print()
    print_status(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
