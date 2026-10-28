"""新聞存檔與去重。

去重兩層：
1. 原文網址（整理掉追蹤參數後）相同 → 同一則，資料庫的 UNIQUE 也會擋。
2. 標題相同（去掉空白與標點後比對）且在最近幾天內 → 視為同一則新聞被不同來源轉載，只收第一則。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

from .. import config
from .models import Article

_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def title_key(title: str) -> str:
    return _PUNCT.sub("", title).lower()


def url_exists(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute("SELECT 1 FROM news WHERE url = ? OR google_url = ?", (url, url)).fetchone() is not None


def recent_title_keys(conn: sqlite3.Connection, days: int) -> set[str]:
    since = (config.now_taipei() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute("SELECT title FROM news WHERE fetched_at >= ? OR published_at >= ?", (since, since))
    return {title_key(r["title"]) for r in rows}


def save(conn: sqlite3.Connection, a: Article) -> int | None:
    """存一則新聞，回傳 id；網址已存在就回傳 None。"""
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO news (url, source, via_outlet, google_url, title, body, published_at, fetched_at, "
                "paywalled, body_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (a.url, a.source, a.via_outlet, a.google_url, a.title, a.body, a.published_at, config.now_iso(),
                 int(a.paywalled), a.body_from))
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
