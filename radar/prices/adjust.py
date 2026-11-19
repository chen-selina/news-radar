"""匯入還原股價的調整係數（除權息、減資），讓報酬不再把除息當成下跌。

原始收盤價在除權息那天會機械性下跌（例如每股配 3 元，參考價就少 3 元），用原始收盤算報酬會把它算成「跌」。
這裡匯入外部資料的調整係數 adj_factor（還原收盤 ＝ 原始收盤 × adj_factor），存進 price_adj 資料表；
radar/validate.py 的 _prices() 預設會乘上係數，所以驗證、訓練、消融、面板迴歸的報酬都會是還原後的。

CSV 欄位：date、code、close、adj_factor（其他欄位不用）。例如 Apex 匯出的 daily_prices.csv。
匯入前會比對 CSV 的 close 和資料庫的收盤價；不一致的筆數太多（> 1%）就不匯入，避免對錯價格。

執行：python -m radar.prices.adjust 路徑\\daily_prices.csv
      python -m radar.prices.adjust --status
限制：
- 只有 CSV 裡的個股有係數，其他個股（以及加權指數 TAIEX，它本來就是價格指數）維持原始收盤。
- 係數以 CSV 最後一天為 1。之後再發生的除權息不會自動調整，要重新匯出、重新匯入。
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

from .. import db

MAX_MISMATCH = 0.01


def read_csv(path: Path) -> list[tuple[str, str, float | None, float]]:
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        need = {"date", "code", "adj_factor"}
        if not need <= set(r.fieldnames or []):
            raise ValueError(f"CSV 少了欄位：{', '.join(sorted(need - set(r.fieldnames or [])))}")
        for row in r:
            try:
                factor = float(row["adj_factor"])
            except (TypeError, ValueError):
                continue
            if not factor > 0:
                continue
            close = float(row["close"]) if row.get("close") not in (None, "") else None
            out.append((row["date"], row["code"].strip(), close, factor))
    return out


def import_factors(conn: sqlite3.Connection, rows: list[tuple[str, str, float | None, float]], source: str,
                   max_mismatch: float = MAX_MISMATCH) -> dict:
    """比對收盤價後寫入 price_adj。同一檔個股原有的係數整個換掉（係數的基準日會變，不能新舊混用）。"""
    have = {(r["date"], r["code"]): r["close"] for r in conn.execute("SELECT date, code, close FROM prices")}
    compared = [(d, c, close) for d, c, close, _ in rows if close is not None and (d, c) in have]
    bad = [(d, c, close, have[(d, c)]) for d, c, close in compared if abs(close - have[(d, c)]) > 1e-6]
    rep = {"rows": len(rows), "codes": len({c for _, c, _, _ in rows}), "compared": len(compared),
           "mismatch": len(bad), "examples": bad[:5], "imported": False}
    if compared and len(bad) / len(compared) > max_mismatch:
        return rep
    codes = sorted({c for _, c, _, _ in rows})
    with conn:
        conn.executemany("DELETE FROM price_adj WHERE code = ?", [(c,) for c in codes])
        conn.executemany("INSERT INTO price_adj (date, code, factor, source) VALUES (?, ?, ?, ?)",
                         [(d, c, f, source) for d, c, _, f in rows])
    rep["imported"] = True
    return rep


def status(conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT COUNT(DISTINCT code) n, MIN(date) a, MAX(date) b, COUNT(*) rows FROM price_adj").fetchone()
    events = conn.execute(
        "SELECT COUNT(*) FROM (SELECT code, factor, LAG(factor) OVER (PARTITION BY code ORDER BY date) prev "
        "FROM price_adj) WHERE prev IS NOT NULL AND ABS(factor - prev) > 1e-12").fetchone()[0]
    return {"codes": r["n"], "first": r["a"], "last": r["b"], "rows": r["rows"], "changes": events}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="匯入還原股價的調整係數（date, code, close, adj_factor）")
    ap.add_argument("csv", nargs="?", type=Path, help="CSV 檔路徑，例如 daily_prices.csv")
    ap.add_argument("--status", action="store_true", help="只顯示目前匯入的範圍")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    if args.csv:
        rows = read_csv(args.csv)
        rep = import_factors(conn, rows, args.csv.name)
        print(f"讀到 {rep['rows']:,} 筆、{rep['codes']} 檔；和資料庫收盤價比對 {rep['compared']:,} 筆，不一致 {rep['mismatch']} 筆")
        for d, c, a, b in rep["examples"]:
            print(f"  {d} {c}：CSV {a}／資料庫 {b}")
        if not rep["imported"]:
            print(f"不一致超過 {MAX_MISMATCH:.0%}，沒有匯入。請確認 CSV 的 close 是原始（未還原）收盤價。")
            return 1
        print("已匯入。")
    elif not args.status:
        ap.print_help()
        return 1
    s = status(conn)
    print(f"目前有調整係數的個股 {s['codes']} 檔，{s['first']} ～ {s['last']}，共 {s['rows']:,} 筆；係數變動（除權息等事件）{s['changes']} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
