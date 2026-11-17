"""同事件新聞分群：同一件事被多家媒體報導時，找出它們是同一群。

做法：標題去掉標點與媒體名稱後切成相鄰兩字的組合，兩則標題共有的組合數 ÷ 較短那則的組合數
就是相似度；發布時間相差 36 小時內、相似度達門檻的兩則視為同一事件，再把互相連得到的併成一群
（甲像乙、乙像丙，三則就是同一群）。

只比標題、不比內文：轉載的新聞內文常被改寫或截斷，標題反而穩定。這是簡單的規則，不是語意理解：
用字完全不同的同一件事會漏掉，寧可少併也不要把不同的事併在一起。

驗證時的用法見 radar/validate.py 的 dedupe 參數。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

WINDOW_HOURS = 36
THRESHOLD = 0.5
MIN_SHARED = 4      # 至少共有幾個兩字組合：避免兩則很短的標題只因為都有公司名就被併在一起
_SUFFIX = re.compile(r"\s[-|｜]\s[^-|｜]{1,20}$")   # 標題結尾的「 - 媒體名稱」
_KEEP = re.compile(r"[0-9a-z一-鿿]")


def bigrams(title: str) -> frozenset[str]:
    """標題的相鄰兩字組合（去掉標點、空白、結尾的媒體名稱，英文不分大小寫）。"""
    t = "".join(_KEEP.findall(_SUFFIX.sub("", title).lower()))
    return frozenset(t[i:i + 2] for i in range(len(t) - 1))


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    shared = len(a & b)
    return shared / min(len(a), len(b)) if shared >= MIN_SHARED else 0.0


def _hours(a: str, b: str) -> float | None:
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()) / 3600
    except (TypeError, ValueError):
        return None


def event_groups(news: list[tuple[int, str, str | None]], threshold: float = THRESHOLD,
                 window_hours: float = WINDOW_HOURS) -> dict[int, int]:
    """news: [(新聞 id, 標題, 發布時間 ISO 字串)]。回傳 新聞 id → 事件編號（同群裡最小的新聞 id）。

    其中一則沒有時間時，標題（去掉標點後）完全相同才併：沒有時間可以把關，條件要更嚴。
    """
    parent = {nid: nid for nid, _, _ in news}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    timed: list[tuple[float, int, frozenset[str]]] = []
    untimed: list[tuple[int, frozenset[str]]] = []
    same_title: dict[frozenset[str], list[int]] = {}
    for nid, title, t in news:
        g = bigrams(title)
        ts = _timestamp(t)
        if ts is None:
            untimed.append((nid, g))
        else:
            timed.append((ts, nid, g))
        if len(g) >= MIN_SHARED:
            same_title.setdefault(g, []).append(nid)
    # 有時間的：依時間排序，只和時間窗內的比（新聞多的時候不必兩兩全比）
    timed.sort()
    for i, (ta, a, ga) in enumerate(timed):
        for tb, b, gb in timed[i + 1:]:
            if tb - ta > window_hours * 3600:
                break
            if similarity(ga, gb) >= threshold:
                union(a, b)
    # 沒有時間的：只和標題完全相同的併
    for a, g in untimed:
        for b in same_title.get(g, ()):
            union(a, b)
    return {nid: find(nid) for nid in parent}


def _timestamp(t: str | None) -> float | None:
    """ISO 時間 → 秒數；沒有時區的當成台北時間。讀不出來回傳 None。"""
    try:
        d = datetime.fromisoformat(t)
    except (TypeError, ValueError):
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone(timedelta(hours=8)))).timestamp()
