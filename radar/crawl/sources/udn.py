"""經濟日報：證券新聞 RSS（money.udn.com/rssfeed）。

RSS 比列表頁輕量，也提供摘要；要多抓其他分類，在 FEEDS 加網址即可。
"""
from __future__ import annotations

from ... import http
from ..models import ListItem
from . import rss

FEEDS = ["https://money.udn.com/rssfeed/news/1001/5590/5607?ch=money"]


def parse_feed(xml_text: str, limit: int) -> list[ListItem]:
    return [ListItem(url=i.link, source="udn", title=i.title, summary=i.summary, published_at=i.published_at)
            for i in rss.parse(xml_text) if i.link][:limit]


def list_items(limit: int) -> list[ListItem]:
    out: list[ListItem] = []
    for feed in FEEDS:
        out += parse_feed(http.get(feed).text, limit)
    return out[:limit]
