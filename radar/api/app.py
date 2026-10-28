"""網站後端 API（FastAPI）。

啟動：python -m radar.api            （http://127.0.0.1:8000，API 說明在 /docs）

每個請求開一條自己的資料庫連線；詞典與分析引擎有快取，詞典或公司資料變了才重建。
錯誤一律回傳 {"detail": "白話說明"}。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import config, db, jobs, predict
from ..analysis import lexicon
from ..analysis.engine import LABELS, Analyzer, production_analyzer, strength
from ..crawl import run as crawl_run
from ..crawl import single, store, urls
from ..crawl.models import OTHER, SOURCES, Article
from ..prices import update as prices
from ..validate import HORIZONS, validate

SOURCE_NAMES = {**SOURCES, "other": OTHER}
WEB_DIR = config.ROOT / "web"  # 網頁（index.html、app.js、style.css）


# ---------- 請求內容 ----------

class TextIn(BaseModel):
    title: str = ""
    body: str = ""


class UrlIn(BaseModel):
    url: str


class NewsIn(BaseModel):
    url: str
    title: str
    body: str = ""
    published_at: str | None = None   # ISO 8601；沒給就用加入當下的時間


class TermIn(BaseModel):
    term: str
    weight: float                     # −5～5，不能是 0（lexicon.set_term 檢查）


class CrawlIn(BaseModel):
    sources: list[str] | None = None  # 沒給就用 config/crawl.json 開啟的來源
    limit: int | None = Field(default=None, ge=1, le=100)
    dry_run: bool = False


class PricesIn(BaseModel):
    start: str | None = None          # YYYY-MM-DD
    end: str | None = None


class CsvIn(BaseModel):
    csv: str


# ---------- 共用 ----------

def _bad(msg: str, code: int = 400) -> HTTPException:
    return HTTPException(status_code=code, detail=msg)


def _stock_dict(r: sqlite3.Row) -> dict:
    return {"code": r["stock_code"], "name": r["name"], "score": r["score"], "label": r["label"],
            "strength": strength(r["score"]), "type": r["type"], "sector": r["sector"], "reason": r["reason"]}


def _news_items(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    """新聞列加上鎖定的原始預測（含個股）。"""
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    preds = {p["news_id"]: p for p in conn.execute(f"SELECT * FROM predictions WHERE news_id IN ({marks})", ids)}
    stocks: dict[int, list[dict]] = {}
    for s in conn.execute(
            f"SELECT ps.*, COALESCE(st.name, ps.stock_code) AS name FROM prediction_stocks ps "
            f"LEFT JOIN stocks st ON st.code = ps.stock_code "
            f"WHERE ps.prediction_id IN (SELECT id FROM predictions WHERE news_id IN ({marks})) "
            f"ORDER BY ps.prediction_id, ps.type, abs(ps.score) DESC", ids):
        stocks.setdefault(s["prediction_id"], []).append(_stock_dict(s))
    out = []
    for r in rows:
        p = preds.get(r["id"])
        out.append({
            "id": r["id"], "url": r["url"], "source": r["source"], "source_name": SOURCE_NAMES[r["source"]],
            "via_outlet": r["via_outlet"], "title": r["title"], "body": r["body"],
            "published_at": r["published_at"], "fetched_at": r["fetched_at"],
            "paywalled": bool(r["paywalled"]), "body_from": r["body_from"],
            "prediction": None if p is None else {
                "id": p["id"], "lexicon_version": p["lexicon_version"], "created_at": p["created_at"],
                "total": p["total_score"], "label": p["label"], "label_text": LABELS[p["label"]],
                "strength": strength(p["total_score"]), "stocks": stocks.get(p["id"], [])},
        })
    return out


def _lexicon_dict(conn: sqlite3.Connection, lex: lexicon.Lexicon) -> dict:
    versions = conn.execute("SELECT version, created_at, note FROM lexicon_versions ORDER BY version DESC").fetchall()
    return {
        "version": lex.version,
        "sentiment": [{"term": t, "weight": w} for t, w in lex.sentiment.items()],
        "sectors": [{"keyword": k, "sector": s} for k, s in lex.sectors.items()],
        "versions": [dict(v) for v in versions],
    }


# ---------- 應用程式 ----------

def create_app(db_path: Path | str | None = None, lexicon_path: Path = lexicon.LEXICON_PATH,
               lock_path: Path = jobs.LOCK_PATH) -> FastAPI:
    app = FastAPI(title="台股新聞雷達 API", version="0.1",
                  description="新聞列表、文字與網址分析、預測驗證、詞典管理。錯誤回傳 {detail: 說明}。")
    cache: dict = {"key": None, "an": None}
    by_version: dict = {}
    cache_lock = threading.Lock()

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def analyzer(conn: sqlite3.Connection) -> Analyzer:
        """目前的詞典＋公司資料；兩者都沒變就沿用上次建好的分析引擎。"""
        lex = lexicon.current(conn, lexicon_path)
        stamp = tuple(conn.execute("SELECT COUNT(*), MAX(updated_at) FROM stocks").fetchone())
        if stamp[0] == 0:
            raise _bad("資料庫沒有公司資料，請先執行 python -m radar.stocks.load", 503)
        key = (lex.version, stamp)
        with cache_lock:
            if cache["key"] != key:
                cache["an"] = predict.current_analyzer(conn, lexicon_path)
                cache["key"] = key
            return cache["an"]

    Conn = Depends(get_conn)

    @app.exception_handler(RequestValidationError)
    def bad_request(_: Request, e: RequestValidationError):
        parts = [f"{'.'.join(str(x) for x in err['loc'] if x != 'body')}：{err['msg']}" for err in e.errors()]
        return JSONResponse(status_code=422, content={"detail": "請求格式不對（" + "；".join(parts) + "）"})

    @app.get("/api/meta", summary="來源、詞典版本、資料筆數與價格資料範圍")
    def meta(conn: sqlite3.Connection = Conn):
        one = lambda sql: conn.execute(sql).fetchone()[0]
        lv = conn.execute("SELECT MAX(version) FROM lexicon_versions").fetchone()[0]
        return {
            "sources": SOURCE_NAMES,
            "enabled": crawl_run.load_config()["enabled"],
            "lexicon_version": lv,
            "counts": {"news": one("SELECT COUNT(*) FROM news"), "predictions": one("SELECT COUNT(*) FROM predictions"),
                       "stocks": one("SELECT COUNT(*) FROM stocks")},
            "prices": {"first": one("SELECT MIN(date) FROM trading_days"), "last": one("SELECT MAX(date) FROM trading_days")},
            "horizons": list(HORIZONS),
            "last_fetched": one("SELECT MAX(fetched_at) FROM news"),
            "last_job": (dict(r) if (r := conn.execute(
                "SELECT started_at, finished_at, status, summary FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()) else None),
            "recalc_versions": [r[0] for r in conn.execute("SELECT DISTINCT lexicon_version FROM recalcs ORDER BY 1 DESC")],
        }

    # ----- 新聞 -----

    def versioned(conn: sqlite3.Connection, version: int) -> Analyzer:
        """指定版本的詞典＋目前的公司資料，用來在原文上標出關鍵詞。"""
        cur = analyzer(conn)
        with cache_lock:
            key = (version, id(cur))
            if key not in by_version:
                if len(by_version) > 20:
                    by_version.clear()
                by_version[key] = production_analyzer(lexicon.get(conn, version), cur.stocks)
            return by_version[key]

    def with_highlight(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
        # 只用來在原文上標出關鍵詞；分數與影響個股以鎖定的預測為準
        for it in items:
            p = it["prediction"]
            it["highlight"] = None if p is None else                 versioned(conn, p["lexicon_version"]).analyze(it["title"], it["body"]).to_dict()
        return items

    def news_filter(label=None, source=None, q=None, stock=None, since=None) -> tuple[str, list]:
        if source and source not in SOURCE_NAMES:
            raise _bad(f"沒有「{source}」這個來源")
        where, args = [], []
        if label:
            where.append("n.id IN (SELECT news_id FROM predictions WHERE label = ?)")
            args.append(label)
        if source:
            where.append("n.source = ?")
            args.append(source)
        if q and q.strip():
            # 標題、內文，或受影響個股的代號、名稱
            like = f"%{q.strip()}%"
            where.append("(n.title LIKE ? OR n.body LIKE ? OR n.id IN (SELECT p.news_id FROM predictions p "
                         "JOIN prediction_stocks ps ON ps.prediction_id = p.id LEFT JOIN stocks st ON st.code = ps.stock_code "
                         "WHERE ps.stock_code LIKE ? OR st.name LIKE ?))")
            args += [like] * 4
        if stock:
            where.append("n.id IN (SELECT p.news_id FROM predictions p JOIN prediction_stocks ps "
                         "ON ps.prediction_id = p.id WHERE ps.stock_code = ?)")
            args.append(stock)
        if since:
            where.append("substr(COALESCE(n.published_at, n.fetched_at), 1, 10) >= ?")
            args.append(since)
        return (" WHERE " + " AND ".join(where)) if where else "", args

    Since = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="只看這天（含）之後發布的新聞")

    @app.get("/api/news", summary="新聞列表（含鎖定的原始預測）")
    def list_news(label: str | None = Query(None, pattern="^(pos|neg|neu)$"),
                  source: str | None = Query(None), q: str | None = None, stock: str | None = None,
                  since: str | None = Since, highlight: bool = Query(False, description="附上原文要標色的關鍵詞位置"),
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                  conn: sqlite3.Connection = Conn):
        cond, args = news_filter(label, source, q, stock, since)
        total = conn.execute(f"SELECT COUNT(*) FROM news n{cond}", args).fetchone()[0]
        rows = conn.execute(f"SELECT n.* FROM news n{cond} "
                            f"ORDER BY COALESCE(n.published_at, n.fetched_at) DESC, n.id DESC LIMIT ? OFFSET ?",
                            [*args, limit, offset]).fetchall()
        items = _news_items(conn, rows)
        return {"total": total, "items": with_highlight(conn, items) if highlight else items}

    @app.get("/api/news/summary", summary="情緒分布與個股影響排行（側欄用）")
    def news_summary(source: str | None = Query(None), since: str | None = Since, top: int = Query(7, ge=1, le=50),
                     conn: sqlite3.Connection = Conn):
        cond, args = news_filter(source=source, since=since)
        counts = {"pos": 0, "neg": 0, "neu": 0, "none": 0}
        for r in conn.execute(f"SELECT COALESCE(p.label, 'none') AS label, COUNT(*) AS n FROM news n "
                              f"LEFT JOIN predictions p ON p.news_id = n.id{cond} GROUP BY 1", args):
            counts[r["label"]] = r["n"]
        rank = [dict(r) for r in conn.execute(
            f"SELECT ps.stock_code AS code, COALESCE(st.name, ps.stock_code) AS name, SUM(ps.score) AS score, "
            f"COUNT(DISTINCT p.news_id) AS n FROM news n JOIN predictions p ON p.news_id = n.id "
            f"JOIN prediction_stocks ps ON ps.prediction_id = p.id LEFT JOIN stocks st ON st.code = ps.stock_code"
            f"{cond} GROUP BY ps.stock_code", args)]
        up = sorted((r for r in rank if r["score"] > 0), key=lambda r: -r["score"])[:top]
        down = sorted((r for r in rank if r["score"] < 0), key=lambda r: r["score"])[:top]
        return {"total": sum(counts.values()), "counts": counts, "up": up, "down": down}

    @app.get("/api/news/{news_id}", summary="單則新聞；附上用鎖定時的詞典重新標示的關鍵詞位置")
    def get_news(news_id: int, conn: sqlite3.Connection = Conn):
        row = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
        if row is None:
            raise _bad(f"沒有 id={news_id} 的新聞", 404)
        return with_highlight(conn, _news_items(conn, [row]))[0]

    @app.post("/api/news", status_code=201, summary="把貼連結或手動輸入的新聞加入列表，並鎖定預測")
    def add_news(data: NewsIn, conn: sqlite3.Connection = Conn):
        if not urls.is_http(data.url.strip()):
            raise _bad("要加入列表的新聞必須有原文網址（http:// 或 https:// 開頭）")
        if not data.title.strip():
            raise _bad("標題不能是空白")
        url = urls.canonical(data.url)
        old = conn.execute("SELECT id FROM news WHERE url = ? OR google_url = ?", (url, url)).fetchone()
        if old:
            raise _bad(f"這則新聞已經在列表裡（id={old['id']}）", 409)
        an = analyzer(conn)
        published = data.published_at or config.now_iso()
        art = Article(url=url, source=urls.source_of(url) or "other", title=data.title.strip(),
                      body=data.body.strip(), published_at=published, body_from="manual")
        nid = store.save(conn, art)
        if nid is None:
            raise _bad("這則新聞已經在列表裡", 409)
        predict.create_prediction(conn, nid, an)
        row = conn.execute("SELECT * FROM news WHERE id = ?", (nid,)).fetchone()
        return _news_items(conn, [row])[0]

    # ----- 分析 -----

    @app.post("/api/analyze-text", summary="分析標題與內文（不存檔）")
    def analyze_text(data: TextIn, conn: sqlite3.Connection = Conn):
        if not data.title.strip() and not data.body.strip():
            raise _bad("請輸入標題或內文")
        an = analyzer(conn)
        return {"lexicon_version": an.lexicon.version, **an.analyze(data.title.strip(), data.body.strip()).to_dict()}

    @app.post("/api/analyze-url", summary="讀取網址、擷取標題與內文後分析（不存檔）")
    def analyze_url(data: UrlIn, conn: sqlite3.Connection = Conn):
        try:
            f = single.fetch(data.url)
        except single.BadUrl as e:
            raise _bad(str(e)) from None
        saved = conn.execute("SELECT id FROM news WHERE url = ? OR google_url = ?", (f.url, f.url)).fetchone()
        out = {**vars(f), "saved_id": saved["id"] if saved else None, "analysis": None, "lexicon_version": None}
        if f.ok:
            an = analyzer(conn)
            out["lexicon_version"] = an.lexicon.version
            out["analysis"] = an.analyze(f.title, f.body).to_dict()
        return out

    # ----- 驗證 -----

    @app.get("/api/validation", summary="預測驗證（命中率、事件研究、逐筆結果）")
    def validation(horizon: int = Query(3, ge=1, le=60), benchmark: str = Query("excess", pattern="^(excess|raw)$"),
                   threshold: float = Query(0.01, ge=0, le=0.5), scope: str = Query("all", pattern="^(all|direct)$"),
                   lexicon_version: int | None = Query(None, description="給版本號就驗證該版的重算結果"),
                   conn: sqlite3.Connection = Conn):
        if lexicon_version is not None and not conn.execute(
                "SELECT 1 FROM recalcs WHERE lexicon_version = ? LIMIT 1", (lexicon_version,)).fetchone():
            raise _bad(f"第 {lexicon_version} 版詞典還沒有重算結果，請先執行重算", 404)
        try:
            return validate(conn, horizon, benchmark, threshold, scope, lexicon_version)
        except ValueError as e:
            raise _bad(str(e)) from None

    @app.post("/api/recalc", summary="用目前的詞典重算所有新聞（另存，不改原始預測）")
    def recalc(conn: sqlite3.Connection = Conn):
        an = analyzer(conn)
        return {"lexicon_version": an.lexicon.version, "count": predict.recalculate(conn, an)}

    # ----- 詞典 -----

    @app.get("/api/lexicon", summary="目前的詞典與版本紀錄")
    def get_lexicon(conn: sqlite3.Connection = Conn):
        return _lexicon_dict(conn, lexicon.current(conn, lexicon_path))

    @app.post("/api/lexicon", summary="新增情緒詞或修改分數（建立新版本）")
    def set_term(data: TermIn, conn: sqlite3.Connection = Conn):
        try:
            lex = lexicon.set_term(conn, data.term, data.weight, lexicon_path)
        except ValueError as e:
            raise _bad(str(e)) from None
        return _lexicon_dict(conn, lex)

    @app.delete("/api/lexicon", summary="刪除情緒詞（建立新版本）")
    def remove_term(term: str, conn: sqlite3.Connection = Conn):
        try:
            lex = lexicon.remove_term(conn, term, lexicon_path)
        except KeyError as e:
            raise _bad(e.args[0], 404) from None
        return _lexicon_dict(conn, lex)

    # ----- 資料更新（開發用） -----

    @app.post("/api/crawl", summary="手動觸發爬蟲（開發用，會花幾分鐘）")
    def crawl(data: CrawlIn, conn: sqlite3.Connection = Conn):
        bad = [s for s in data.sources or [] if s not in SOURCES]
        if bad:
            raise _bad(f"沒有這些來源：{'、'.join(bad)}")
        # 和排程共用同一個鎖：排程正在爬的時候，按鈕不會再爬一次
        try:
            with jobs.Lock(lock_path):
                log: list[str] = []
                out = crawl_run.run_all(conn, data.sources, data.limit, data.dry_run, log=log.append)
                return {**out, "log": log}
        except jobs.Busy:
            raise _bad("排程或另一次爬取正在執行，請等它完成", 409) from None

    @app.post("/api/prices/update", summary="抓證交所、櫃買中心收盤價（開發用）")
    def prices_update(data: PricesIn, conn: sqlite3.Connection = Conn):
        try:
            start = date.fromisoformat(data.start) if data.start else prices.default_start(conn)
            end = date.fromisoformat(data.end) if data.end else config.now_taipei().date()
        except ValueError:
            raise _bad("日期格式請用 YYYY-MM-DD") from None
        log: list[str] = []
        return {**prices.update(conn, start, end, log=log.append), "log": log}

    @app.post("/api/prices/import", summary="匯入收盤價 CSV（日期, 代號, 收盤價；大盤代號 TAIEX）")
    def prices_import(data: CsvIn, conn: sqlite3.Connection = Conn):
        if not data.csv.strip():
            raise _bad("CSV 內容是空的")
        return prices.import_csv(conn, data.csv)

    # ----- 公司 -----

    @app.get("/api/stocks", summary="搜尋公司（代號、名稱或別名）")
    def stocks(q: str = "", limit: int = Query(20, ge=1, le=2000), conn: sqlite3.Connection = Conn):
        like = f"%{q.strip()}%"
        rows = conn.execute(
            "SELECT code, name, aliases, sectors, industry, market, close, price_date, market_cap FROM stocks "
            "WHERE ? = '%%' OR code LIKE ? OR name LIKE ? OR aliases LIKE ? "
            "ORDER BY code = ? DESC, market_cap IS NULL, market_cap DESC LIMIT ?",
            (like, like, like, like, q.strip(), limit)).fetchall()
        return [{**dict(r), "aliases": json.loads(r["aliases"]), "sectors": json.loads(r["sectors"])} for r in rows]

    # 網頁放最後：上面的 /api/… 沒有對到的網址才交給網頁
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app()
