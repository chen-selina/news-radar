"""台灣財經情緒字典（黃裕烈，2025）：讀檔、計數、情緒指標。出處與授權見 config/remi/README.md。

論文的用法是「數詞」：字典只分正面、負面，沒有強度，每個詞算一次。這裡照做，並提供：
- count()：一段文字裡的正面、負面詞數（長詞優先，同論文所附程式）。
- tone()：論文式 (1) 的情緒指標 Tone ＝（正面句數 − 負面句數）÷（正面句數 ＋ 負面句數）。
- as_lexicon()：轉成本專案分析引擎能用的詞典（每個詞 ±1），用來做「只換字典、其他規則不變」的比較。

與論文所附程式的一處差異：論文把正面詞、負面詞分開各比對一次，「未影響」會同時算到正面的「未影響」
和負面的「影響」。這裡正負面一起做長詞優先比對，一段文字只會算給最長的那個詞。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .. import config
from .engine import sentence_spans
from .lexicon import Lexicon

DICT_PATH = config.CONFIG_DIR / "remi" / "dictionary.tsv"


@dataclass(frozen=True)
class RemiDict:
    polarity: dict[str, int]  # 詞 → +1／−1
    counts: dict[str, int]    # 詞 → 在財經新聞的出現次數（可當權重的依據）

    @property
    def max_len(self) -> int:
        return max(map(len, self.polarity), default=0)


@lru_cache(maxsize=4)
def load(path: Path = DICT_PATH) -> RemiDict:
    polarity: dict[str, int] = {}
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 2 or not cols[0]:
            continue
        polarity[cols[0]] = 1 if cols[1] == "pos" else -1
        counts[cols[0]] = int(cols[3]) if len(cols) > 3 and cols[3].isdigit() else 0
    return RemiDict(polarity, counts)


def matches(text: str, d: RemiDict | None = None) -> list[tuple[int, int, str, int]]:
    """文字裡的情緒詞：[(開始, 結束, 詞, +1／−1)]，依位置排序。長詞優先，同長取前面的，不重疊。"""
    d = d or load()
    found = []
    for i in range(len(text)):
        for n in range(min(d.max_len, len(text) - i), 0, -1):
            sign = d.polarity.get(text[i:i + n])
            if sign:
                found.append((i, i + n, text[i:i + n], sign))
    found.sort(key=lambda m: (m[0] - m[1], m[0]))
    used = [False] * len(text)
    keep = []
    for m in found:
        if not any(used[m[0]:m[1]]):
            used[m[0]:m[1]] = [True] * (m[1] - m[0])
            keep.append(m)
    return sorted(keep)


def count(text: str, d: RemiDict | None = None) -> tuple[int, int]:
    """（正面詞數, 負面詞數）。"""
    ms = matches(text, d)
    pos = sum(m[3] > 0 for m in ms)
    return pos, len(ms) - pos


def tone(text: str, d: RemiDict | None = None) -> dict:
    """論文式 (1)。句子的正面詞多於負面詞算正面句，反之負面句，一樣多或沒有情緒詞不計。

    回傳 pos_words／neg_words（詞數）、pos_sentences／neg_sentences（句數）、tone（−1～+1；沒有正負面句時為 0）。
    """
    ms = matches(text, d)
    pos_s = neg_s = 0
    for a, b in sentence_spans(text):
        net = sum(m[3] for m in ms if a <= m[0] < b)
        pos_s += net > 0
        neg_s += net < 0
    pos_w = sum(m[3] > 0 for m in ms)
    return {"pos_words": pos_w, "neg_words": len(ms) - pos_w, "pos_sentences": pos_s, "neg_sentences": neg_s,
            "tone": (pos_s - neg_s) / (pos_s + neg_s) if pos_s + neg_s else 0.0}


def as_lexicon(base: Lexicon, extend: bool = False, d: RemiDict | None = None) -> Lexicon:
    """把論文字典轉成分析引擎用的詞典：每個詞 ±1，產業關鍵字沿用 base。

    extend=True 時再加上 base 的情緒詞（個股事件擴充），同樣只取方向 ±1；
    兩邊方向不同的詞（例如「吃緊」）以 base 為準，base 裡分數為 0 的詞視為停用，從結果拿掉。
    """
    d = d or load()
    sentiment = {w: float(s) for w, s in d.polarity.items()}
    if extend:
        for term, w in base.sentiment.items():
            if w == 0:
                sentiment.pop(term, None)
            else:
                sentiment[term] = 1.0 if w > 0 else -1.0
    return Lexicon(sentiment, dict(base.sectors))
