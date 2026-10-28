"""共用設定：路徑、時區、爬蟲身分。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

# 可用環境變數 RADAR_DB 指到別的資料庫檔（測試或隊友各自的位置）
DB_PATH = Path(os.environ.get("RADAR_DB", DATA_DIR / "radar.db"))

# 台灣沒有日光節約時間，固定 UTC+8 即可，不必依賴 Windows 上常缺的 tzdata
TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")

USER_AGENT = "TWNewsRadar/0.1 (personal research project)"
REQUEST_INTERVAL = 1.5  # 同一網站兩次請求之間至少間隔的秒數
# 個別網站的間隔：證交所請求太密集會暫時封鎖來源 IP
HOST_INTERVALS = {"www.twse.com.tw": 3.5, "www.tpex.org.tw": 2.0}
REQUEST_TIMEOUT = 30.0


def now_taipei() -> datetime:
    return datetime.now(TAIPEI)


def now_iso() -> str:
    return now_taipei().isoformat(timespec="seconds")
