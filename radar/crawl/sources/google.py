"""Google新聞：搜尋「台股」的 RSS（zh-TW）。

⚠ 預設關閉（config/crawl.json）。news.google.com 的 robots.txt 不允許一般程式讀取 /rss/，
而爬蟲會遵守 robots.txt，所以即使打開，目前也會被擋下。要不要例外處理由專案負責人決定。

原文網址：Google 的連結是轉址（news.google.com/rss/articles/CBMi…）。舊格式的編碼裡直接含有
原始網址，可以不連網解出；2024 年後的新格式（CBMi 後接 AU_yqL…）必須再向 Google 查詢，
同樣受 robots.txt 限制，所以這裡解不出來的項目會略過不收（brief 規定沒有原文網址不收錄）。
原始媒體名稱取自 RSS 的 <source> 標籤，例如「自由財經」。
"""
from __future__ import annotations

import base64
import re
from urllib.parse import quote

from ... import http
from ..models import ListItem
from . import rss

QUERY = "台股"
FEED = f"https://news.google.com/rss/search?q={quote(QUERY)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
_URL_IN_BYTES = re.compile(rb"https?://[\x21-\x7e]+")


def decode_google_url(google_url: str) -> str | None:
    """不連網解出原始網址（只適用舊格式）；解不出來回傳 None。"""
    m = re.search(r"/articles/([A-Za-z0-9_-]+)", google_url)
    if not m:
        return None
    token = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        return None
    if b"AU_yqL" in raw[:12]:
        return None  # 新格式：網址不在編碼裡
    found = _URL_IN_BYTES.search(raw)
    if not found:
        return None
    url = found.group(0).decode("ascii", "ignore")
    # 編碼裡網址後面可能接著長度位元組等雜訊，只取到合法網址字元為止
    return re.match(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", url).group(0)


def split_title(title: str, outlet: str | None) -> str:
    """Google 標題是「標題 - 媒體名」，去掉後面的媒體名。"""
    if outlet and title.endswith(" - " + outlet):
        return title[: -len(outlet) - 3].strip()
    return title


def parse_feed(xml_text: str, limit: int) -> tuple[list[ListItem], int]:
    """回傳（可收錄的項目, 因解不出原文網址而略過的數量）。"""
    out, skipped = [], 0
    for i in rss.parse(xml_text):
        original = decode_google_url(i.link)
        if not original:
            skipped += 1
            continue
        out.append(ListItem(url=original, source="google", title=split_title(i.title, i.source_name),
                            summary="", published_at=i.published_at, via_outlet=i.source_name,
                            google_url=i.link))
    return out[:limit], skipped


def list_items(limit: int) -> list[ListItem]:
    items, skipped = parse_feed(http.get(FEED).text, limit)
    if skipped:
        print(f"  Google新聞：{skipped} 則無法解析原始網址，略過")
    return items
