"""公司短名稱的排除規則：避免「大量賣單」被當成提到 3167 大量。

兩個字的公司名（以及 ambiguous_long 裡的長名稱）要有下列任一佐證才算：
1. 旁邊有股票代號：「能率（5392）」「2468 華經」
2. 用括號標出：【精測】「欣銓」
3. 用頓號與其他公司並列：「旺宏、力成」
4. 後面緊接股市用語（中間可隔數字、月份）：「台玻逆勢」「尖點 9 月營收」
5. 前面緊接股市用語：「調節友達」「鑽針廠尖點」
6. 同一則新聞已確認過這家公司（任一名稱、任一處）
免佐證：trusted 名單、stock_overrides.json 的人工別名、三個字以上的名稱。
exclude_phrases 裡的片語一律不算，例如「東南亞」的「南亞」。

設定在 config/name_rules.json。這套規則是原型沒有的，所以與原型比對的測試不使用它。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .. import config

NAME_RULES_PATH = config.CONFIG_DIR / "name_rules.json"
OVERRIDES_PATH = config.CONFIG_DIR / "stock_overrides.json"
BRACKETS = {"【": "】", "「": "」", "『": "』", "〈": "〉", "《": "》"}
WINDOW = 12


@dataclass
class NameRules:
    trusted: set[str]
    ambiguous_long: set[str]
    exclude_phrases: dict[str, list[str]]
    context_after: re.Pattern
    context_before: re.Pattern

    @classmethod
    def load(cls, path: Path = NAME_RULES_PATH, overrides_path: Path = OVERRIDES_PATH) -> "NameRules":
        d = json.loads(path.read_text(encoding="utf-8"))
        trusted = set(d["trusted"])
        if overrides_path.exists():  # 人工別名是人挑過的，不需佐證
            ov = json.loads(overrides_path.read_text(encoding="utf-8"))
            trusted |= {a for k, v in ov.items() if not k.startswith("_") for a in v.get("aliases", [])}
        alt = lambda words: "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        return cls(
            trusted=trusted,
            ambiguous_long=set(d.get("ambiguous_long", [])),
            exclude_phrases=d.get("exclude_phrases", {}),
            # 後面：中間可以隔空白、數字、月份等，例如「尖點 9 月營收」
            context_after=re.compile(rf"^[\s\d月日年%.]*(?:{alt(d['context_after'])})"),
            context_before=re.compile(rf"(?:{alt(d['context_before'])})[\s／/：:]*$"),
        )

    def needs_evidence(self, term: str) -> bool:
        if term in self.trusted:
            return False
        return len(term) <= 2 or term in self.ambiguous_long

    def excluded(self, text: str, start: int, end: int, term: str) -> bool:
        """這個位置是否落在固定排除的片語裡（例如「東南亞」）。"""
        for phrase in self.exclude_phrases.get(term, []):
            i = text.find(phrase, max(0, start - len(phrase)))
            while i != -1 and i <= start:
                if i <= start and end <= i + len(phrase):
                    return True
                i = text.find(phrase, i + 1)
        return False

    def evidence(self, text: str, m, stock_cands: list) -> str | None:
        """回傳佐證種類；沒有佐證回傳 None。m 是公司名稱的比對結果。"""
        a, b, code = m.start, m.end, m.stock.code
        after, before = text[b:b + WINDOW], text[max(0, a - WINDOW):a]
        if re.match(rf"^\s*[（(]?\s*{code}(?!\d)", after) or re.search(rf"(?<!\d){code}\s*[）)]?\s*$", before):
            return "代號"
        if a > 0 and text[a - 1] in BRACKETS and text[b:b + 1] == BRACKETS[text[a - 1]]:
            return "括號"
        if text[b:b + 1] == "、" and any(c.start == b + 1 for c in stock_cands):
            return "列舉"
        if a > 0 and text[a - 1] == "、" and any(c.end == a - 1 for c in stock_cands):
            return "列舉"
        if self.context_after.match(after):
            return "語境"
        if self.context_before.search(before):
            return "語境"
        return None

    def filter(self, text: str, found: list) -> list:
        """從所有候選比對中，拿掉沒有佐證的公司名稱。found 的順序保持不變。"""
        stock_cands = [f for f in found if f.kind == "stock"]

        def shadowed(m) -> bool:  # 整個被更長的詞蓋住（例如「南亞科」裡的「南亞」），不能拿來佐證
            return any(o is not m and o.start <= m.start and m.end <= o.end and (o.end - o.start) > (m.end - m.start)
                       for o in found)

        drop, pending, confirmed = set(), [], set()
        for m in stock_cands:
            if self.excluded(text, m.start, m.end, m.term):
                drop.add(id(m))
            elif not self.needs_evidence(m.term) or self.evidence(text, m, stock_cands):
                if not shadowed(m):
                    confirmed.add(m.stock.code)
            else:
                pending.append(m)
        for m in pending:  # 規則 6：同一則新聞已確認過這家公司
            if m.stock.code not in confirmed:
                drop.add(id(m))
        return [f for f in found if id(f) not in drop]
