"""排程要做的事：爬新聞（並鎖定預測）→ 抓收盤價。每天 08:00、14:30 各跑一次（設定方式見 README「排程」）。

執行：python -m radar.jobs              （爬新聞＋抓收盤價）
      python -m radar.jobs --no-crawl   （只抓收盤價）
      python -m radar.jobs --no-prices  （只爬新聞）

- 收盤價只抓還沒有的交易日：14:30 那次如果官方當天資料還沒公布，下一次（隔天 08:00）會補上。
- 預測驗證是網站打開時即時計算，收盤價一進資料庫，驗證結果就跟著更新，不必另外跑。
- 輸出同時寫到 data/logs/jobs-YYYY-MM.log；每次執行的結果記在資料表 job_runs，網站上方會顯示。
- 同時只能跑一個（data/jobs.lock）：上一次還沒跑完時，這次直接略過。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
import traceback
from pathlib import Path

from . import config, db
from .crawl import run as crawl_run
from .prices import update as prices

LOG_DIR = config.DATA_DIR / "logs"
LOCK_PATH = config.DATA_DIR / "jobs.lock"
STALE_LOCK_SECONDS = 2 * 60 * 60  # 鎖檔超過 2 小時視為上次異常中斷留下的
STATUS_TEXT = {"running": "執行中", "ok": "成功", "partial": "部分完成", "error": "失敗"}


class Busy(Exception):
    """上一次排程還在執行。"""


class Lock:
    def __init__(self, path: Path = LOCK_PATH):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > STALE_LOCK_SECONDS:
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Busy(f"上一次排程還在執行（{self.path} 存在），這次略過") from None
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def _logger(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"jobs-{config.now_taipei():%Y-%m}.log"

    def log(msg: str) -> None:
        line = f"[{config.now_taipei():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    return log


def run(conn: sqlite3.Connection, crawl: bool = True, update_prices: bool = True, log=print) -> dict:
    """依序執行各項工作；某一項失敗不影響下一項。回傳 {status, summary, errors}。"""
    with conn:
        run_id = conn.execute("INSERT INTO job_runs (started_at, status) VALUES (?, 'running')",
                              (config.now_iso(),)).lastrowid
    parts: list[str] = []
    errors: list[str] = []
    failed = 0  # 整項工作失敗（不是個別來源或個別日子）

    if crawl:
        log("爬取新聞")
        try:
            out = crawl_run.run_all(conn, log=lambda m: log("  " + m.strip()))
            parts.append(f"新收錄 {out['saved']} 則")
            errors += out["notes"]
            errors += [f"{name}：{r['error']}" for name, r in out["sources"].items() if r["error"]]
        except Exception as e:  # noqa: BLE001 —— 記下來，繼續做收盤價
            errors.append(f"爬取新聞失敗：{e}")
            failed += 1
            log(traceback.format_exc())

    if update_prices:
        log("抓收盤價")
        try:
            start = prices.default_start(conn)
            rep = prices.update(conn, start, config.now_taipei().date(), log=lambda m: log("  " + m.strip()))
            parts.append(f"收盤價 {rep['days']} 天")
            errors += rep["errors"]
        except Exception as e:  # noqa: BLE001
            errors.append(f"抓收盤價失敗：{e}")
            failed += 1
            log(traceback.format_exc())

    status = "error" if failed and failed == crawl + update_prices else "partial" if errors else "ok"
    summary = "；".join(parts) + (f"；注意 {len(errors)} 項" if errors else "")
    with conn:
        conn.execute("UPDATE job_runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
                     (config.now_iso(), status, summary, run_id))
    for e in errors:
        log("注意：" + e)
    log(f"結束（{STATUS_TEXT[status]}）：{summary}")
    return {"status": status, "summary": summary, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="排程工作：爬新聞＋抓收盤價")
    ap.add_argument("--no-crawl", action="store_true", help="不爬新聞")
    ap.add_argument("--no-prices", action="store_true", help="不抓收盤價")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    log = _logger(LOG_DIR)
    try:
        with Lock(LOCK_PATH):
            res = run(db.connect(args.db), not args.no_crawl, not args.no_prices, log=log)
    except Busy as e:
        log(str(e))
        return 0
    return 1 if res["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
