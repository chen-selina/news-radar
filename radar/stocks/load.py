"""載入全部上市櫃公司到 stocks 資料表。

執行：python -m radar.stocks.load
      python -m radar.stocks.load --save-raw data/raw   （順便把官方原始 JSON 存檔）
      python -m radar.stocks.load --from-raw data/raw   （不連網，用存檔的 JSON 載入）

公司名稱、產業別、股數、收盤價來自官方；別名與細分族群（記憶體、AI伺服器、CPO…）
來自 config/stock_overrides.json，由人工維護。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

from .. import config, db
from . import sources

OVERRIDES_PATH = config.CONFIG_DIR / "stock_overrides.json"
MIN_COMPANIES = {"上市": 500, "上櫃": 400}  # 少於這個數字代表官方資料不完整，不覆蓋資料庫
_SUFFIX = re.compile(r"(-KY|-DR|\*)$")


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def auto_aliases(name: str) -> list[str]:
    """「臻鼎-KY」→「臻鼎」、「華義*」→「華義」：新聞通常不會寫後綴。"""
    short = _SUFFIX.sub("", name)
    return [short] if short != name and len(short) >= 2 else []


def build_rows(raw: dict[str, list[dict]], overrides: dict[str, dict],
               min_companies: dict[str, int] = MIN_COMPANIES) -> tuple[list[dict], list[str]]:
    """把官方原始資料與人工補充合併成 stocks 資料列；回傳（資料列, 警告）。"""
    warnings: list[str] = []
    unknown: set[str] = set()
    companies = sources.parse_twse_basic(raw["twse_basic"], unknown) + sources.parse_tpex_basic(raw["tpex_basic"], unknown)
    quotes = {**sources.parse_tpex_day(raw["tpex_day"]), **sources.parse_twse_day(raw["twse_day"])}
    if unknown:
        warnings.append(f"沒有對照的產業別代碼：{', '.join(sorted(unknown))}（請補到 radar/stocks/industries.py）")

    for market, n in min_companies.items():
        got = sum(c.market == market for c in companies)
        if got < n:
            raise RuntimeError(f"{market}公司只讀到 {got} 家（預期至少 {n} 家），官方資料可能不完整，停止載入。")

    rows: dict[str, dict] = {}
    now = config.now_iso()
    for c in companies:
        if c.code in rows:  # 同一代號同時出現在上市與上櫃（轉市場期間），以先讀到的上市為準
            warnings.append(f"{c.code} {c.name} 同時出現在上市與上櫃資料，採用{rows[c.code]['market']}")
            continue
        ov = overrides.get(c.code, {})
        aliases = list(dict.fromkeys(auto_aliases(c.name) + ov.get("aliases", [])))
        sectors = list(dict.fromkeys(([c.industry] if c.industry else []) + ov.get("sectors", [])))
        q = quotes.get(c.code)
        rows[c.code] = {
            "code": c.code, "name": c.name, "full_name": c.full_name,
            "aliases": json.dumps(aliases, ensure_ascii=False),
            "sectors": json.dumps(sectors, ensure_ascii=False),
            "industry": c.industry, "market": c.market, "shares": c.shares,
            "close": q.close if q else None, "price_date": q.date if q else None,
            "market_cap": round(c.shares * q.close) if q and c.shares else None,
            "updated_at": now,
        }

    for code, ov in overrides.items():
        if code not in rows:
            warnings.append(f"stock_overrides.json 的 {code}（{ov.get('name', '')}）不在官方名單，可能已下市或改代號")
        elif ov.get("name") and ov["name"] != rows[code]["name"]:
            warnings.append(f"stock_overrides.json 的 {code} 寫「{ov['name']}」，官方簡稱是「{rows[code]['name']}」")
    return list(rows.values()), warnings


def save_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """整批取代 stocks：已下市的公司會被移除（舊預測仍以代號保留，不受影響）。"""
    cols = list(rows[0].keys())
    with conn:
        conn.execute("DELETE FROM stocks")
        conn.executemany(
            f"INSERT INTO stocks ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})", rows)


def read_raw(folder: Path) -> dict[str, list[dict]]:
    return {k: json.loads((folder / f"{k}.json").read_text(encoding="utf-8"))
            for k in ("twse_basic", "tpex_basic", "twse_day", "tpex_day")}


def write_raw(folder: Path, raw: dict[str, list[dict]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for k, v in raw.items():
        (folder / f"{k}.json").write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="載入全部上市櫃公司")
    ap.add_argument("--from-raw", type=Path, help="從存檔的官方 JSON 載入，不連網")
    ap.add_argument("--save-raw", type=Path, help="把抓到的官方 JSON 存到這個資料夾")
    ap.add_argument("--db", type=Path, help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)

    if args.from_raw:
        raw = read_raw(args.from_raw)
    else:
        print("下載證交所、櫃買中心公開資料…")
        try:
            raw = sources.fetch_all()
        except RuntimeError as e:
            print(f"{e}\n請確認網路連線後再試一次；資料庫沒有被修改。")
            return 1
        if args.save_raw:
            write_raw(args.save_raw, raw)

    rows, warnings = build_rows(raw, load_overrides())
    conn = db.connect(args.db)
    save_rows(conn, rows)

    listed = sum(r["market"] == "上市" for r in rows)
    with_cap = sum(r["market_cap"] is not None for r in rows)
    dates = sorted({r["price_date"] for r in rows if r["price_date"]})
    print(f"完成：上市 {listed} 家、上櫃 {len(rows) - listed} 家，共 {len(rows)} 家")
    print(f"有市值的 {with_cap} 家（收盤價日期 {', '.join(dates) or '無'}）")
    for w in warnings:
        print("注意：" + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
