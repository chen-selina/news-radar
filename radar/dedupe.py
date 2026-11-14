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
from datetime import datetime

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

    grams = [(nid, bigrams(title), t) for nid, title, t in news]
    for i, (a, ga, ta) in enumerate(grams):
        for b, gb, tb in grams[i + 1:]:
            gap = _hours(ta, tb)
            if gap is None:
                same = ga == gb and len(ga) >= MIN_SHARED
            else:
                same = gap <= window_hours and similarity(ga, gb) >= threshold
            if same:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    return {nid: find(nid) for nid in parent}
