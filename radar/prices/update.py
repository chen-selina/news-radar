"""抓取交易日曆與收盤價，或匯入 CSV。

執行：python -m radar.prices update                  （從最早一則新聞的前一個月抓到今天，已抓過的跳過）
      python -m radar.prices update --from 2026-09-01
      python -m radar.prices import 收盤價.csv          （欄位：日期, 代號, 收盤價；大盤代號 TAIEX）
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from .. import config, db, http
from . import sources

TAIEX = "TAIEX"
_TAIEX_NAMES = re.compile(r"^(TAIEX|加權指數|大盤|加權)$", re.I)
LOOKBACK_DAYS = 30  # 往前多抓一個月，事件研究圖需要新聞前 3 個交易日


def _months(start: date, end: date):
    d = date(start.year, start.month, 1)
    while d <= end:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def default_start(conn: sqlite3.Connection) -> date:
    row = conn.execute("SELECT MIN(substr(COALESCE(published_at, fetched_at), 1, 10)) FROM news").fetchone()
    first = date.fromisoformat(row[0]) if row and row[0] else config.now_taipei().date()
    return first - timedelta(days=LOOKBACK_DAYS)


def save_calendar(conn: sqlite3.Connection, days: dict[str, float]) -> None:
    with conn:
        conn.executemany("INSERT OR IGNORE INTO trading_days (date) VALUES (?)", [(d,) for d in days])
        conn.executemany("INSERT OR REPLACE INTO prices (date, code, close) VALUES (?, ?, ?)",
                         [(d, TAIEX, v) for d, v in days.items()])


def save_day(conn: sqlite3.Connection, day: str, market: str, closes: dict[str, float], codes: set[str]) -> int:
    rows = [(day, c, v) for c, v in closes.items() if not codes or c in codes]
    with conn:
        conn.executemany("INSERT OR REPLACE INTO prices (date, code, close) VALUES (?, ?, ?)", rows)
        conn.execute("INSERT OR REPLACE INTO price_loads (date, market, rows, fetched_at) VALUES (?, ?, ?, ?)",
                     (day, market, len(rows), config.now_iso()))
    return len(rows)


def update(conn: sqlite3.Connection, start: date, end: date, log=print) -> dict:
    """抓 start～end 的交易日曆、加權指數與上市櫃收盤價；已抓過的日子跳過。"""
    rep = {"months": 0, "days": 0, "rows": 0, "errors": []}
    this_month = date(end.year, end.month, 1)
    for m in _months(start, end):
        has = conn.execute("SELECT 1 FROM trading_days WHERE date LIKE ? LIMIT 1", (f"{m:%Y-%m}-%",)).fetchone()
        if has and m < this_month:
            continue  # 過去的月份抓過就不必重抓；本月每次都更新
        try:
            save_calendar(conn, sources.fetch_month(m))
            rep["months"] += 1
        except (http.Blocked, RuntimeError, ValueError) as e:
            rep["errors"].append(f"{m:%Y-%m} 交易日曆：{e}")

    codes = {r[0] for r in conn.execute("SELECT code FROM stocks")}
    days = [r[0] for r in conn.execute("SELECT date FROM trading_days WHERE date BETWEEN ? AND ? ORDER BY date",
                                        (start.isoformat(), end.isoformat()))]
    loaded = {(r[0], r[1]) for r in conn.execute("SELECT date, market FROM price_loads")}
    for d in days:
        if (d, "twse") in loaded and (d, "tpex") in loaded:
            continue
        for market, fetch in (("twse", sources.fetch_twse_day), ("tpex", sources.fetch_tpex_day)):
            if (d, market) in loaded:
                continue
            try:
                closes = fetch(date.fromisoformat(d))
            except (http.Blocked, RuntimeError, ValueError) as e:
                rep["errors"].append(f"{d} {market}：{e}")
                continue
            if not closes:
                rep["errors"].append(f"{d} {market}：官方沒有資料（可能尚未公布），下次再試")
                continue
            n = save_day(conn, d, market, closes, codes)
            rep["rows"] += n
            log(f"  {d} {'上市' if market == 'twse' else '上櫃'} {n} 檔")
        rep["days"] += 1
    return rep


def normalize_date(s: str) -> str | None:
    """西元或民國年（115/10/27）都轉成 YYYY-MM-DD。"""
    m = re.match(r"^\s*(\d{2,4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s or "")
    if not m:
        return None
    y = int(m[1])
    if y < 1000:
        y += 1911
    return f"{y:04d}-{int(m[2]):02d}-{int(m[3]):02d}"


def import_csv(conn: sqlite3.Connection, text: str) -> dict:
    """匯入收盤價 CSV（日期, 代號, 收盤價）。大盤代號寫 TAIEX、加權指數或大盤，其日期也會加進交易日曆。"""
    rows, errors, taiex_days = [], [], set()
    for i, r in enumerate(csv.reader(io.StringIO(text.lstrip("﻿"))), start=1):
        if not any(x.strip() for x in r):
            continue
        d = normalize_date(r[0]) if r else None
        price = sources.num(r[2]) if len(r) > 2 else None
        if not d or price is None:
            if i > 1:  # 第一列可能是標題
                errors.append(f"第 {i} 列無法讀取")
            continue
        code = re.sub(r"\s", "", r[1])
        if _TAIEX_NAMES.match(code):
            code = TAIEX
            taiex_days.add(d)
        rows.append((d, code, price))
    with conn:
        conn.executemany("INSERT OR REPLACE INTO prices (date, code, close) VALUES (?, ?, ?)", rows)
        conn.executemany("INSERT OR IGNORE INTO trading_days (date) VALUES (?)", [(d,) for d in taiex_days])
        conn.executemany("INSERT OR REPLACE INTO price_loads (date, market, rows, fetched_at) VALUES (?, 'csv', ?, ?)",
                         [(d, sum(1 for x in rows if x[0] == d), config.now_iso()) for d in {x[0] for x in rows}])
    return {"rows": len(rows), "days": len({x[0] for x in rows}), "taiex_days": len(taiex_days), "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="收盤價：抓取或匯入")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("update", help="從證交所、櫃買中心抓交易日曆與收盤價")
    up.add_argument("--from", dest="start", type=date.fromisoformat, help="起始日 YYYY-MM-DD")
    up.add_argument("--to", dest="end", type=date.fromisoformat, help="結束日 YYYY-MM-DD（預設今天）")
    im = sub.add_parser("import", help="匯入收盤價 CSV")
    im.add_argument("file", type=Path)
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)

    if args.cmd == "import":
        rep = import_csv(conn, args.file.read_text(encoding="utf-8-sig"))
        print(f"匯入 {rep['rows']} 筆、{rep['days']} 天（其中 {rep['taiex_days']} 天有大盤）")
        for e in rep["errors"][:10]:
            print("  略過：" + e)
        return 0

    start = args.start or default_start(conn)
    end = args.end or config.now_taipei().date()
    print(f"抓取 {start} ～ {end} 的交易日曆與收盤價（證交所請求間隔約 3.5 秒，第一次會比較久）…")
    rep = update(conn, start, end)
    print(f"完成：更新 {rep['months']} 個月的日曆、{rep['days']} 個交易日、{rep['rows']} 筆收盤價")
    for e in rep["errors"]:
        print("  注意：" + e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
