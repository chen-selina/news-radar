"""預測鎖定與重算。

- 原始預測（predictions、prediction_stocks）：新聞收錄當下用當時的詞典分析，連同詞典版本存檔。
  每則新聞只有一筆，建立後資料庫觸發器擋住修改、刪除與追加個股（radar/db.py）。
- 重算結果（recalcs、recalc_stocks）：之後想看新詞典對舊新聞的效果，另外存，不碰原始預測。

執行：python -m radar.predict            （替還沒有預測的新聞建立預測）
      python -m radar.predict --recalc   （用目前的詞典重算所有新聞，存成重算結果）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from . import config, db
from .analysis import lexicon
from .analysis.engine import Analysis, Analyzer, label_of, production_analyzer
from .stocks.query import all_stocks


def current_analyzer(conn: sqlite3.Connection, lexicon_path: Path = lexicon.LEXICON_PATH) -> Analyzer:
    """目前的詞典（必要時自動建立新版本）＋依市值排序的全部公司，開啟原型之後加入的修正。"""
    stocks = all_stocks(conn)
    if not stocks:
        raise RuntimeError("資料庫沒有公司資料，請先執行 python -m radar.stocks.load")
    return production_analyzer(lexicon.current(conn, lexicon_path), stocks)


def _analyze_news(an: Analyzer, row: sqlite3.Row) -> Analysis:
    return an.analyze(row["title"], row["body"] or "")


def _stock_rows(a: Analysis) -> list[tuple]:
    return [(v.stock.code, v.score, label_of(v.score), v.type, v.sector, v.reason) for v in a.impacts]


def create_prediction(conn: sqlite3.Connection, news_id: int, an: Analyzer) -> int:
    """建立並鎖定這則新聞的預測，回傳 prediction id；已經有預測就直接回傳原本的，不重算。"""
    old = conn.execute("SELECT id FROM predictions WHERE news_id = ?", (news_id,)).fetchone()
    if old:
        return old["id"]
    if an.lexicon.version is None:
        raise ValueError("詞典沒有版本號，請用 lexicon.current() 或 lexicon.get() 取得詞典")
    row = conn.execute("SELECT title, body FROM news WHERE id = ?", (news_id,)).fetchone()
    if row is None:
        raise KeyError(f"沒有 id={news_id} 的新聞")
    a = _analyze_news(an, row)
    stocks = _stock_rows(a)
    with conn:  # 預測與個股在同一個交易裡寫入，寫完 stock_count 筆就鎖住
        cur = conn.execute(
            "INSERT INTO predictions (news_id, lexicon_version, total_score, label, created_at, stock_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (news_id, an.lexicon.version, a.total, a.label, config.now_iso(), len(stocks)))
        pid = cur.lastrowid
        conn.executemany(
            "INSERT INTO prediction_stocks (prediction_id, stock_code, score, label, type, sector, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", [(pid, *s) for s in stocks])
    return pid


def predict_pending(conn: sqlite3.Connection, an: Analyzer | None = None) -> list[int]:
    """替還沒有預測的新聞建立預測，回傳新的 prediction id。"""
    ids = [r["id"] for r in conn.execute(
        "SELECT n.id FROM news n LEFT JOIN predictions p ON p.news_id = n.id WHERE p.id IS NULL ORDER BY n.id")]
    if not ids:
        return []
    an = an or current_analyzer(conn)
    return [create_prediction(conn, nid, an) for nid in ids]


def recalculate(conn: sqlite3.Connection, an: Analyzer | None = None) -> int:
    """用指定（預設為目前）的詞典重算所有新聞，存成重算結果；同一版本重算會覆寫上次的重算結果。"""
    an = an or current_analyzer(conn)
    v = an.lexicon.version
    rows = conn.execute("SELECT id, title, body FROM news ORDER BY id").fetchall()
    now = config.now_iso()
    with conn:
        conn.execute("DELETE FROM recalcs WHERE lexicon_version = ?", (v,))  # recalc_stocks 會跟著刪
        for r in rows:
            a = _analyze_news(an, r)
            conn.execute("INSERT INTO recalcs (news_id, lexicon_version, total_score, label, created_at) "
                         "VALUES (?, ?, ?, ?, ?)", (r["id"], v, a.total, a.label, now))
            conn.executemany(
                "INSERT INTO recalc_stocks (news_id, lexicon_version, stock_code, score, label, type, sector, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [(r["id"], v, *s) for s in _stock_rows(a)])
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="建立預測或重算")
    ap.add_argument("--recalc", action="store_true", help="用目前的詞典重算所有新聞（不影響原始預測）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    try:
        an = current_analyzer(conn)
    except RuntimeError as e:
        print(e)
        return 1
    if args.recalc:
        n = recalculate(conn, an)
        print(f"已用詞典第 {an.lexicon.version} 版重算 {n} 則新聞（存在 recalcs，原始預測不變）")
        return 0
    new = predict_pending(conn, an)
    print(f"新建立 {len(new)} 筆預測（詞典第 {an.lexicon.version} 版）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
