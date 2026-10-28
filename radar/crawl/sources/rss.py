"""RSS 2.0 共用解析（Yahoo、經濟日報、Google新聞都用）。"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from ..extract import squash, to_taipei_iso


@dataclass
class RssItem:
    title: str
    link: str
    published_at: str | None
    summary: str
    source_name: str | None  # <source> 標籤（Google新聞用來標示原始媒體）
    source_url: str | None


def strip_html(s: str) -> str:
    return squash(html.unescape(re.sub(r"<[^>]+>", " ", s or "")))


def parse(xml_text: str) -> list[RssItem]:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    out = []
    for it in root.iter("item"):
        get = lambda tag: (it.findtext(tag) or "").strip()
        src = it.find("source")
        out.append(RssItem(
            title=strip_html(get("title")),
            link=get("link"),
            published_at=to_taipei_iso(get("pubDate")),
            summary=strip_html(get("description")),
            source_name=(src.text or "").strip() if src is not None and src.text else None,
            source_url=src.get("url") if src is not None else None,
        ))
    return out
