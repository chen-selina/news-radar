"""Yahoo奇摩股市：台股新聞 RSS。

轉載自其他媒體的新聞，原始媒體名稱在文章頁讀取（見 extract.py 的 via_outlet）。
"""
from __future__ import annotations

from ... import http
from ..models import ListItem
from . import rss

FEEDS = ["https://tw.stock.yahoo.com/rss?category=tw-market"]


def parse_feed(xml_text: str, limit: int) -> list[ListItem]:
    return [ListItem(url=i.link, source="yahoo", title=i.title, summary=i.summary, published_at=i.published_at)
            for i in rss.parse(xml_text) if i.link][:limit]


def list_items(limit: int) -> list[ListItem]:
    out: list[ListItem] = []
    for feed in FEEDS:
        out += parse_feed(http.get(feed).text, limit)
    return out[:limit]
