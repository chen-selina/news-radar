"""從資料庫讀公司清單給分析引擎用。"""
from __future__ import annotations

import json
import sqlite3

from ..analysis.engine import Stock


def all_stocks(conn: sqlite3.Connection) -> list[Stock]:
    """依市值由大到小（沒有市值的排最後）：原型的產業連動挑每個族群的前 4 檔；正式版整個族群都連動，依這個順序列出。"""
    rows = conn.execute(
        "SELECT code, name, aliases, sectors, market_cap FROM stocks "
        "ORDER BY market_cap IS NULL, market_cap DESC, code").fetchall()
    return [Stock(r["code"], r["name"], tuple(json.loads(r["aliases"])), tuple(json.loads(r["sectors"])),
                  r["market_cap"]) for r in rows]
