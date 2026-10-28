"""爬取流程：列出新聞 → 去重 → 讀文章頁擷取內文 → 存檔。

執行：python -m radar.crawl                 （所有已開啟的來源）
      python -m radar.crawl --source cw --limit 5
      python -m radar.crawl --dry-run       （只列出會收錄的新聞，不存檔）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field

from .. import config, db, http, predict
from . import store, urls
from .extract import extract
from .models import SOURCES, Article, ListItem
from .sources import FETCHERS

CRAWL_CONFIG = config.CONFIG_DIR / "crawl.json"


@dataclass
class SourceReport:
    listed: int = 0
    saved: int = 0
    duplicate: int = 0
    no_url: int = 0
    partial: int = 0          # 讀不到全文，只用摘要或標題
    retry_later: int = 0      # 文章頁暫時讀不到，留到下次
    error: str | None = None
    saved_ids: list[int] = field(default_factory=list)


def load_config() -> dict:
    return json.loads(CRAWL_CONFIG.read_text(encoding="utf-8"))


def build_article(item: ListItem, html: str | None) -> Article:
    """把列表項目和文章頁內容合成一則新聞。html 為 None 代表文章頁讀不到。"""
    page = extract(html, item.source) if html else None
    title = (page.title if page and page.title else "") or item.title
    if page and page.body and not page.partial:
        body, body_from = page.body, "page"
    elif item.summary or (page and page.body):
        body, body_from = (item.summary if len(item.summary) >= len(page.body if page else "") else page.body), "summary"
    else:
        body, body_from = "", "title"
    # 「轉載自」只對彙整型來源有意義：Yahoo 讀文章頁，Google 取 RSS 的 <source>
    via = item.via_outlet or (page.via_outlet if page and item.source == "yahoo" else None)
    return Article(
        url=item.url, source=item.source, title=title, body=body,
        published_at=item.published_at or (page.published_at if page else None),
        via_outlet=via, google_url=item.google_url,
        paywalled=bool(page and page.paywalled), body_from=body_from)


def crawl_source(conn: sqlite3.Connection, name: str, limit: int, dedupe_days: int,
                 dry_run: bool = False, log=print, analyzer=None) -> SourceReport:
    """analyzer 有給時，每則新聞存檔後立刻建立並鎖定預測（brief 6.1：抓到當下就存預測）。"""
    rep = SourceReport()
    try:
        items = FETCHERS[name].list_items(limit)
    except (http.Blocked, RuntimeError) as e:
        rep.error = str(e)
        return rep
    rep.listed = len(items)
    seen_titles = store.recent_title_keys(conn, dedupe_days)
    for item in items:
        if not urls.is_http(item.url):
            rep.no_url += 1  # brief：沒有原文網址的不收錄
            continue
        item.url = urls.canonical(item.url)
        key = store.title_key(item.title)
        if store.url_exists(conn, item.url) or (key and key in seen_titles):
            rep.duplicate += 1
            continue
        try:
            html = http.get(item.url).text
        except http.Blocked as e:
            log(f"    {e}，改用摘要")
            html = None
        except RuntimeError as e:
            if not item.summary:  # 暫時性的網路錯誤又沒有摘要：先不存，下次爬取再試
                log(f"    文章頁讀不到，下次再試：{e}")
                rep.retry_later += 1
                continue
            log(f"    文章頁讀不到，改用摘要：{e}")
            html = None
        art = build_article(item, html)
        if not art.title:
            rep.no_url += 1
            continue
        if art.body_from != "page":
            rep.partial += 1
        seen_titles.add(store.title_key(art.title))
        if dry_run:
            log(f"  [{art.body_from}] {art.title}  {art.url}")
            rep.saved += 1
            continue
        nid = store.save(conn, art)
        if nid is None:
            rep.duplicate += 1
        else:
            rep.saved += 1
            rep.saved_ids.append(nid)
            if analyzer is not None:
                predict.create_prediction(conn, nid, analyzer)
    return rep


def run_all(conn: sqlite3.Connection, names: list[str] | None = None, limit: int | None = None,
            dry_run: bool = False, log=print) -> dict:
    """爬取多個來源（預設為 crawl.json 開啟的來源），存檔並鎖定預測。命令列與網站 API 共用。"""
    cfg = load_config()
    names = names or [n for n, on in cfg["enabled"].items() if on]
    limit = limit or cfg.get("per_source_limit", 30)
    out = {"sources": {}, "saved": 0, "backlog": 0, "lexicon_version": None, "notes": []}
    analyzer = None
    if not dry_run:
        try:
            analyzer = predict.current_analyzer(conn)
            out["lexicon_version"] = analyzer.lexicon.version
            # 先補上之前沒建立預測的新聞（例如上次爬取時還沒載入公司資料）
            out["backlog"] = len(predict.predict_pending(conn, analyzer))
        except RuntimeError as e:
            out["notes"].append(f"{e}；這次只存新聞，之後執行 python -m radar.predict 補建預測")
    for name in names:
        log(f"{SOURCES[name]}：")
        rep = crawl_source(conn, name, limit, cfg.get("title_dedupe_days", 3), dry_run, log=log, analyzer=analyzer)
        out["sources"][name] = asdict(rep)
        out["saved"] += rep.saved
        if rep.error:
            log(f"  失敗：{rep.error}")
        else:
            log(f"  列表 {rep.listed} 則 → 新收錄 {rep.saved}（其中只有摘要或標題 {rep.partial}）"
                f"、重複 {rep.duplicate}、缺網址 {rep.no_url}" + (f"、下次再試 {rep.retry_later}" if rep.retry_later else ""))
    return out


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="爬取新聞")
    ap.add_argument("--source", choices=list(FETCHERS), action="append",
                    help="只抓這個來源（可重複指定）；指定時會忽略 crawl.json 的開關")
    ap.add_argument("--limit", type=int, default=cfg.get("per_source_limit", 30))
    ap.add_argument("--dry-run", action="store_true", help="只列出會收錄的新聞，不存檔")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)

    out = run_all(db.connect(args.db), args.source, args.limit, args.dry_run)
    for note in out["notes"]:
        print("注意：" + note)
    if out["backlog"]:
        print(f"補建 {out['backlog']} 筆先前漏掉的預測")
    total, v = out["saved"], out["lexicon_version"]
    print(f"完成，共新收錄 {total} 則" + ("（試跑，未存檔）" if args.dry_run else "")
          + (f"，預測已鎖定（詞典第 {v} 版）" if v and total else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
