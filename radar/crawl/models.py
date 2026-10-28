"""爬蟲共用的資料格式。"""
from __future__ import annotations

from dataclasses import dataclass

SOURCES = {"yahoo": "Yahoo奇摩股市", "google": "Google新聞", "udn": "經濟日報", "bw": "商業周刊", "cw": "天下雜誌"}

OTHER = "其他網站"  # 不在五個指定來源內（貼連結分析、手動加入）


@dataclass
class ListItem:
    """列表頁或 RSS 上的一則新聞（還沒讀內文）。"""
    url: str                         # 原文網址；Google新聞為解析後的原始媒體網址
    source: str                      # yahoo／google／udn／bw／cw
    title: str = ""
    summary: str = ""                # RSS 或列表頁上的摘要，讀不到內文時拿來分析
    published_at: str | None = None  # ISO 8601，台北時間
    via_outlet: str | None = None    # 轉載的原始媒體
    google_url: str | None = None


@dataclass
class Article:
    """準備存進 news 資料表的一則新聞。"""
    url: str
    source: str
    title: str
    body: str
    published_at: str | None
    via_outlet: str | None = None
    google_url: str | None = None
    paywalled: bool = False
    body_from: str = "page"          # page＝讀到文章內文；summary＝只有摘要；title＝只有標題
