"""資料庫：去重與預測鎖定。"""
import sqlite3

import pytest

from radar import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.execute("INSERT INTO lexicon_versions (version, checksum, created_at) VALUES (1, 'x', '2026-10-27T08:00:00+08:00')")
    c.execute("INSERT INTO lexicon_terms (version, position, term, kind, weight) VALUES (1, 0, '創新高', 'sentiment', 2.5)")
    c.execute("INSERT INTO news (url, source, title, fetched_at) VALUES ('https://example.com/a', 'udn', '標題', 'now')")
    c.execute("INSERT INTO predictions (news_id, lexicon_version, total_score, label, created_at, stock_count) "
              "VALUES (1, 1, 3.0, 'pos', 'now', 1)")
    c.execute("INSERT INTO prediction_stocks (prediction_id, stock_code, score, type) VALUES (1, '2330', 3.0, 'direct')")
    c.commit()
    return c


def test_url_is_unique(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO news (url, source, title, fetched_at) VALUES ('https://example.com/a', 'yahoo', '別的標題', 'now')")


def test_source_must_be_known(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO news (url, source, title, fetched_at) VALUES ('https://example.com/b', 'ptt', 't', 'now')")


@pytest.mark.parametrize("sql", [
    "UPDATE predictions SET total_score = 9",
    "DELETE FROM predictions",
    "UPDATE prediction_stocks SET score = 9",
    "DELETE FROM prediction_stocks",
    "UPDATE lexicon_terms SET weight = 9",
    "DELETE FROM lexicon_terms",
])
def test_locked_tables_reject_changes(conn, sql):
    with pytest.raises(sqlite3.IntegrityError, match="不可"):
        conn.execute(sql)


def test_cannot_add_stocks_after_prediction_is_complete(conn):
    with pytest.raises(sqlite3.IntegrityError, match="不可再加入"):
        conn.execute("INSERT INTO prediction_stocks (prediction_id, stock_code, score, type) VALUES (1, '2303', 1, 'direct')")


def test_one_original_prediction_per_news(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO predictions (news_id, lexicon_version, total_score, label, created_at) VALUES (1, 1, 0, 'neu', 'now')")


def test_news_with_prediction_cannot_be_deleted(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM news WHERE id = 1")
