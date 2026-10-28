"""排程：爬新聞→抓收盤價、某一項失敗不影響下一項、執行紀錄、同時只跑一個。不連網（換成假的）。"""
import os
import time

import pytest

from radar import db, jobs
from radar.crawl import run as crawl_run
from radar.prices import update as prices


def fake_crawl(saved=3, error=None):
    def run_all(conn, *a, log=print, **kw):
        log("經濟日報：")
        return {"saved": saved, "notes": [], "sources": {"udn": {"error": error}}, "backlog": 0, "lexicon_version": 1}
    return run_all


def fake_prices(days=1, errors=()):
    return lambda conn, start, end, log=print: {"months": 1, "days": days, "rows": 10, "errors": list(errors)}


@pytest.fixture
def conn():
    return db.connect(":memory:")


def last_run(conn):
    return dict(conn.execute("SELECT * FROM job_runs ORDER BY id DESC").fetchone())


def test_ok_run_is_recorded(conn, monkeypatch):
    monkeypatch.setattr(crawl_run, "run_all", fake_crawl())
    monkeypatch.setattr(prices, "update", fake_prices(days=2))
    res = jobs.run(conn, log=lambda m: None)
    assert res["status"] == "ok"
    r = last_run(conn)
    assert r["status"] == "ok" and r["finished_at"] and r["summary"] == "新收錄 3 則；收盤價 2 天"


def test_crawl_crash_still_fetches_prices(conn, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("網路斷線")
    called = []
    monkeypatch.setattr(crawl_run, "run_all", boom)
    monkeypatch.setattr(prices, "update", lambda *a, **kw: called.append(1) or fake_prices()(None, None, None))
    res = jobs.run(conn, log=lambda m: None)
    assert called and res["status"] == "partial" and any("網路斷線" in e for e in res["errors"])


def test_everything_failed_is_error(conn, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("x")
    monkeypatch.setattr(crawl_run, "run_all", boom)
    monkeypatch.setattr(prices, "update", boom)
    assert jobs.run(conn, log=lambda m: None)["status"] == "error"


def test_source_error_and_missing_prices_are_partial(conn, monkeypatch):
    monkeypatch.setattr(crawl_run, "run_all", fake_crawl(error="讀取失敗：403"))
    monkeypatch.setattr(prices, "update", fake_prices(errors=["2026-10-28 twse：官方沒有資料（可能尚未公布），下次再試"]))
    res = jobs.run(conn, log=lambda m: None)
    assert res["status"] == "partial" and len(res["errors"]) == 2 and "注意 2 項" in res["summary"]


def test_only_prices(conn, monkeypatch):
    monkeypatch.setattr(crawl_run, "run_all", lambda *a, **kw: pytest.fail("不該爬新聞"))
    monkeypatch.setattr(prices, "update", fake_prices())
    assert jobs.run(conn, crawl=False, log=lambda m: None)["summary"] == "收盤價 1 天"


def test_lock_blocks_second_run_and_clears_stale(tmp_path):
    path = tmp_path / "jobs.lock"
    with jobs.Lock(path):
        with pytest.raises(jobs.Busy):
            with jobs.Lock(path):
                pass
    assert not path.exists()  # 結束後釋放
    path.write_text("123")
    old = time.time() - jobs.STALE_LOCK_SECONDS - 60
    os.utime(path, (old, old))
    with jobs.Lock(path):  # 異常中斷留下的舊鎖檔不會永遠卡住
        pass


def test_main_writes_log(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(jobs, "LOCK_PATH", tmp_path / "jobs.lock")
    monkeypatch.setattr(crawl_run, "run_all", fake_crawl())
    monkeypatch.setattr(prices, "update", fake_prices())
    assert jobs.main(["--db", str(tmp_path / "r.db")]) == 0
    text = next((tmp_path / "logs").glob("jobs-*.log")).read_text(encoding="utf-8")
    assert "爬取新聞" in text and "結束（成功）" in text
