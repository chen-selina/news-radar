"""網址整理：去掉追蹤參數，讓同一篇文章只有一個網址（去重用）。"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 這些查詢參數只是追蹤或來源標記，去掉後仍是同一篇文章
_TRACKING = re.compile(r"^(utm_.*|rec|ref|fbclid|gclid|oc|ocid|guccounter|guce_.*|soc_.*|_gl)$", re.I)

HOSTS = [
    (re.compile(r"(^|\.)money\.udn\.com$"), "udn"),
    (re.compile(r"(^|\.)yahoo\.com$"), "yahoo"),
    (re.compile(r"(^|\.)news\.google\."), "google"),
    (re.compile(r"(^|\.)businessweekly\.com\.tw$"), "bw"),
    (re.compile(r"(^|\.)cw\.com\.tw$"), "cw"),
]


def canonical(url: str) -> str:
    u = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not _TRACKING.match(k)])
    path = u.path or "/"
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), path, query, ""))


def is_http(url: str | None) -> bool:
    return bool(url) and re.match(r"^https?://\S+$", url) is not None


def source_of(url: str) -> str | None:
    """網址屬於五個指定來源的哪一個；都不是回傳 None（貼連結分析標為「其他網站」）。"""
    host = urlsplit(url).hostname or ""
    return next((src for rx, src in HOSTS if rx.search(host)), None)
