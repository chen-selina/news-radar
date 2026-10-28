"""天下雜誌：「今日最新」文章列表頁。

網址帶 ?rec= 的是頁面上「推薦閱讀」區塊的舊文章，不收。

⚠ 預設關閉（config/crawl.json）。robots.txt 允許爬取，但網站對本爬蟲的請求回應 403 拒絕存取
（2026-10-28 確認，兩台電腦結果相同）。專案決定不改 User-Agent 繞過阻擋；若天下同意或網站
調整後，把 crawl.json 的 cw 改成 true 即可。
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ... import http
from ..models import ListItem

LIST_URL = "https://www.cw.com.tw/today"
ARTICLE = re.compile(r"^https://www\.cw\.com\.tw/article/\d+$")


def parse_list(html: str, limit: int) -> list[ListItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: dict[str, ListItem] = {}
    for a in soup.find_all("a", href=ARTICLE):
        url, title = a["href"], a.get_text(" ", strip=True)
        if url not in items:
            items[url] = ListItem(url=url, source="cw", title=title)
        elif title and not items[url].title:
            items[url].title = title
    return list(items.values())[:limit]


def list_items(limit: int) -> list[ListItem]:
    return parse_list(http.get(LIST_URL).text, limit)
