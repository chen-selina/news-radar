"""SQLite 資料庫：連線與資料表。

只用標準 SQL 與 sqlite3，之後要換 PostgreSQL 時改這一支即可。
預測（predictions、prediction_stocks）與詞典版本（lexicon_terms）用觸發器擋住
UPDATE／DELETE：建立後就不能改，避免事後回頭修改預測灌水命中率。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,              -- 官方簡稱，例如「台積電」
    full_name   TEXT,                       -- 官方全名
    aliases     TEXT NOT NULL DEFAULT '[]', -- JSON 陣列，例如 ["台積"]
    sectors     TEXT NOT NULL DEFAULT '[]', -- JSON 陣列：官方產業別＋手動補充的細分族群
    industry    TEXT,                       -- 官方產業別名稱
    market      TEXT NOT NULL CHECK (market IN ('上市', '上櫃')),
    shares      INTEGER,                    -- 已發行普通股數
    close       REAL,                       -- 計算市值用的收盤價
    price_date  TEXT,                       -- 上面收盤價的日期（西元 YYYY-MM-DD）
    market_cap  REAL,                       -- shares × close，產業連動依此排序
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,      -- 原文網址，唯一鍵（去重用）
    source       TEXT NOT NULL CHECK (source IN ('yahoo', 'google', 'udn', 'bw', 'cw', 'other')),
    via_outlet   TEXT,                      -- 轉載的原始媒體，例如「自由財經」
    google_url   TEXT,                      -- Google新聞的轉址連結
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    published_at TEXT,                      -- ISO 8601，台北時間
    fetched_at   TEXT NOT NULL,
    paywalled    INTEGER NOT NULL DEFAULT 0 CHECK (paywalled IN (0, 1)),
    body_from    TEXT NOT NULL DEFAULT 'page'  -- page＝文章內文；summary＝只有摘要；title＝只有標題；manual＝使用者貼上或修改
);

CREATE TABLE IF NOT EXISTS lexicon_versions (
    version    INTEGER PRIMARY KEY,
    checksum   TEXT NOT NULL,               -- 詞典內容的雜湊，用來判斷 config/lexicon.json 有沒有改
    created_at TEXT NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS lexicon_terms (
    version  INTEGER NOT NULL REFERENCES lexicon_versions(version),
    position INTEGER NOT NULL,              -- 在詞典裡的順序（比對同長同位置的詞時，順序會影響結果）
    term     TEXT NOT NULL,
    kind     TEXT NOT NULL CHECK (kind IN ('sentiment', 'sector')),
    weight   REAL,                          -- 情緒詞的分數
    sector   TEXT,                          -- 產業關鍵字對應的族群
    PRIMARY KEY (version, term, kind)
);

CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY,
    news_id         INTEGER NOT NULL UNIQUE REFERENCES news(id),  -- 每則新聞只有一筆原始預測
    lexicon_version INTEGER NOT NULL REFERENCES lexicon_versions(version),
    total_score     REAL NOT NULL,
    label           TEXT NOT NULL CHECK (label IN ('pos', 'neg', 'neu')),
    created_at      TEXT NOT NULL,
    stock_count     INTEGER NOT NULL DEFAULT 0  -- 個股筆數；寫滿後不能再加（觸發器檢查）
);

CREATE TABLE IF NOT EXISTS prediction_stocks (
    prediction_id INTEGER NOT NULL REFERENCES predictions(id),
    stock_code    TEXT NOT NULL,
    score         REAL NOT NULL,
    label         TEXT NOT NULL DEFAULT 'neu' CHECK (label IN ('pos', 'neg', 'neu')),  -- 個股方向
    type          TEXT NOT NULL CHECK (type IN ('direct', 'sector')),
    sector        TEXT,
    reason        TEXT,
    PRIMARY KEY (prediction_id, stock_code)
);

-- 重算結果：用別的詞典版本重新分析舊新聞，與原始預測分開存，可以覆寫
CREATE TABLE IF NOT EXISTS recalcs (
    news_id         INTEGER NOT NULL REFERENCES news(id),
    lexicon_version INTEGER NOT NULL REFERENCES lexicon_versions(version),
    total_score     REAL NOT NULL,
    label           TEXT NOT NULL CHECK (label IN ('pos', 'neg', 'neu')),
    created_at      TEXT NOT NULL,
    PRIMARY KEY (news_id, lexicon_version)
);

CREATE TABLE IF NOT EXISTS recalc_stocks (
    news_id         INTEGER NOT NULL,
    lexicon_version INTEGER NOT NULL,
    stock_code      TEXT NOT NULL,
    score           REAL NOT NULL,
    label           TEXT NOT NULL CHECK (label IN ('pos', 'neg', 'neu')),
    type            TEXT NOT NULL CHECK (type IN ('direct', 'sector')),
    sector          TEXT,
    reason          TEXT,
    PRIMARY KEY (news_id, lexicon_version, stock_code),
    FOREIGN KEY (news_id, lexicon_version) REFERENCES recalcs(news_id, lexicon_version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS prices (
    date  TEXT NOT NULL,                    -- 西元 YYYY-MM-DD
    code  TEXT NOT NULL,                    -- 個股代號，大盤為 TAIEX
    close REAL NOT NULL,
    PRIMARY KEY (date, code)
);

-- 證交所實際交易日（國定假日、颱風休市都不在裡面），驗證時「第 N 個交易日」以此為準
CREATE TABLE IF NOT EXISTS trading_days (
    date TEXT PRIMARY KEY                   -- 西元 YYYY-MM-DD
);

-- 哪一天、哪個市場的收盤價已經抓過（避免重抓，也用來判斷資料是否完整）
CREATE TABLE IF NOT EXISTS price_loads (
    date       TEXT NOT NULL,
    market     TEXT NOT NULL CHECK (market IN ('twse', 'tpex', 'csv')),
    rows       INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, market)
);

-- 排程（python -m radar.jobs）每次執行的紀錄，網站顯示「上次排程」
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'error')),
    summary     TEXT NOT NULL DEFAULT ''   -- 白話摘要，例如「新收錄 12 則；收盤價 2 天」
);

CREATE INDEX IF NOT EXISTS idx_news_published ON news(published_at);
CREATE INDEX IF NOT EXISTS idx_pstocks_code ON prediction_stocks(stock_code);
CREATE INDEX IF NOT EXISTS idx_prices_code ON prices(code, date);

CREATE TRIGGER IF NOT EXISTS predictions_no_update BEFORE UPDATE ON predictions
BEGIN SELECT RAISE(ABORT, '預測建立後不可修改'); END;
CREATE TRIGGER IF NOT EXISTS predictions_no_delete BEFORE DELETE ON predictions
BEGIN SELECT RAISE(ABORT, '預測建立後不可刪除'); END;
CREATE TRIGGER IF NOT EXISTS pstocks_no_update BEFORE UPDATE ON prediction_stocks
BEGIN SELECT RAISE(ABORT, '預測建立後不可修改'); END;
CREATE TRIGGER IF NOT EXISTS pstocks_no_delete BEFORE DELETE ON prediction_stocks
BEGIN SELECT RAISE(ABORT, '預測建立後不可刪除'); END;
CREATE TRIGGER IF NOT EXISTS pstocks_no_extra BEFORE INSERT ON prediction_stocks
WHEN (SELECT COUNT(*) FROM prediction_stocks WHERE prediction_id = NEW.prediction_id)
     >= (SELECT stock_count FROM predictions WHERE id = NEW.prediction_id)
BEGIN SELECT RAISE(ABORT, '預測建立後不可再加入個股'); END;
CREATE TRIGGER IF NOT EXISTS lexterms_no_update BEFORE UPDATE ON lexicon_terms
BEGIN SELECT RAISE(ABORT, '詞典版本建立後不可修改，請新增版本'); END;
CREATE TRIGGER IF NOT EXISTS lexterms_no_delete BEFORE DELETE ON lexicon_terms
BEGIN SELECT RAISE(ABORT, '詞典版本建立後不可修改，請新增版本'); END;
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # 網站伺服器的同一個請求可能在不同執行緒建立與使用連線；每個請求各自一條連線，不會同時共用
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


# 舊版資料庫升級：key 是「從第幾版升上來」
MIGRATIONS = {
    2: ["ALTER TABLE news ADD COLUMN body_from TEXT NOT NULL DEFAULT 'page'"],
    3: ["ALTER TABLE predictions ADD COLUMN stock_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE prediction_stocks ADD COLUMN label TEXT NOT NULL DEFAULT 'neu' "
        "CHECK (label IN ('pos', 'neg', 'neu'))"],
}


def init_db(conn: sqlite3.Connection) -> None:
    old = conn.execute("PRAGMA user_version").fetchone()[0]
    if 0 < old < min(MIGRATIONS, default=SCHEMA_VERSION):
        raise RuntimeError(
            f"資料庫是舊格式（第 {old} 版，程式需要第 {SCHEMA_VERSION} 版）。"
            "請刪除 data/radar.db 後重跑 python -m radar.stocks.load。")
    if old:
        for v in range(old, SCHEMA_VERSION):
            for sql in MIGRATIONS.get(v, []):
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):  # 欄位已存在＝這步已經做過
                        raise
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
