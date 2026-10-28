"""商業周刊：財經頻道文章列表頁。

只收 /business/blog/ 的一般文章；/indep/ 是廣編稿，不收。
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ... import http
from ..models import ListItem

LIST_URL = "https://www.businessweekly.com.tw/Channel/business/0000000319"
ARTICLE = re.compile(r"^/business/blog/\d+$")


def parse_list(html: str, limit: int) -> list[ListItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: dict[str, ListItem] = {}
    for a in soup.find_all("a", href=ARTICLE):
        url = urljoin(LIST_URL, a["href"])
        title = a.get_text(" ", strip=True)
        if url not in items:
            items[url] = ListItem(url=url, source="bw", title=title)
        elif title and not items[url].title:
            items[url].title = title
    return list(items.values())[:limit]


def list_items(limit: int) -> list[ListItem]:
    return parse_list(http.get(LIST_URL).text, limit)
