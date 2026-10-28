"""分析引擎：原型 news-radar.html 的 analyze() 逐行移植。

規則（PROJECT_BRIEF.md 第 5 節）：
1. 最長優先比對：情緒詞、公司名、產業詞一起比，重疊時取最長的。
2. 否定反轉：詞前 5 字內（遇標點截斷）有否定詞就反轉分數。
3. 加強：詞前 4 字內有加強詞，分數 ×1.5。
4. 標題加權：出現在標題的詞 ×1.5。
5. 直接提及的公司取提到它的句子分數加總；產業連動取一半，每族群最多 4 檔。

與原型一致性由 tests/test_engine_parity.py 逐項比對 tools/make_golden.js 產生的標準答案。
唯一已知差異：JavaScript 字串以 UTF-16 計數，罕見的擴充區字元（例如表情符號）在原型
算 2 個字、這裡算 1 個字，只會影響「前 5 字／前 4 字」的範圍。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .lexicon import Lexicon
from .names import NameRules

NEGATORS = ("沒有", "並非", "降低", "未", "不", "無")  # 順序與原型相同：先比對長的
NOT_NEGATORS = ("不斷", "不少", "無論", "不僅")
BOOSTERS = ("大幅", "強勁", "明顯", "顯著", "持續")
NEG_WINDOW, BOOST_WINDOW = 5, 4
NEG_STOP = ("，", "。", "、", "；", "\n")  # 否定詞的範圍遇到這些標點就截斷
SENTENCE_END = "。！？；\n"
TITLE_WEIGHT = BOOST_WEIGHT = 1.5
SECTOR_SHARE = 0.5   # 產業連動取一半分數
SECTOR_MIN = 0.5     # 產業連動分數絕對值低於此值就不列出
SECTOR_MAX_STOCKS = 4
LABELS = {"pos": "利多", "neg": "利空", "neu": "中性"}


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    aliases: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    market_cap: float | None = None


@dataclass
class Match:
    start: int
    end: int
    term: str
    kind: str                    # pos／neg／stock／sector
    weight: float = 0.0          # 情緒詞的原始分數
    stock: Stock | None = None
    sector: str | None = None
    effective: float | None = None  # 套用否定、加強、標題加權後的分數


@dataclass
class Hit:
    term: str
    w: float
    note: str  # 「否定」「加強」「否定、加強」或空字串


@dataclass
class Impact:
    stock: Stock
    type: str                    # direct／sector
    score: float = 0.0
    sector: str | None = None
    key_sentence: int | None = None
    reason: str = ""
    sentences: list[int] = field(default_factory=list, repr=False)

    @property
    def direction(self) -> str:
        return "偏多" if self.score > 0 else "偏空" if self.score < 0 else "中性"


@dataclass
class Analysis:
    title: str
    body: str
    text: str
    total: float
    label: str
    hits: list[Hit]
    impacts: list[Impact]
    matches: list[Match]
    negators: list[tuple[int, int]]
    sentences: list[tuple[int, int]]
    sentence_scores: list[float]

    @property
    def strength(self) -> str:
        return strength(self.total)

    def to_dict(self) -> dict:
        return {
            "total": self.total, "label": self.label, "label_text": LABELS[self.label], "strength": self.strength,
            "hits": [vars(h) for h in self.hits],
            "impacts": [{"code": v.stock.code, "name": v.stock.name, "type": v.type, "sector": v.sector,
                         "score": v.score, "strength": strength(v.score), "reason": v.reason} for v in self.impacts],
            "matches": [{"start": m.start, "end": m.end, "term": m.term, "kind": m.kind} for m in self.matches],
            "negators": [{"start": a, "end": b} for a, b in self.negators],
        }


def strength(score: float) -> str:
    a = abs(score)
    return "高" if a >= 4 else "中" if a >= 2 else "低"


def label_of(score: float) -> str:
    return "pos" if score >= 1 else "neg" if score <= -1 else "neu"


def sentence_spans(text: str) -> list[tuple[int, int]]:
    out, s = [], 0
    for i, ch in enumerate(text):
        if ch in SENTENCE_END:
            out.append((s, i + 1))
            s = i + 1
    if s < len(text):
        out.append((s, len(text)))
    return out


class Analyzer:
    """建立一次（詞典＋公司清單），重複分析多則新聞。

    stocks 的順序就是產業連動挑股票的順序：正式版傳入依市值由大到小排好的清單。
    下面兩個參數是原型沒有的修正，預設值的行為與原型完全相同：
    - name_rules（radar/analysis/names.py）：公司短名稱的排除規則。
    - sector_fallback：產業關鍵字所在句子沒有情緒時怎麼辦。"total"＝原型做法，改用整則新聞總分；
      "title"＝只有關鍵字出現在標題時才用整則總分，內文隨口提到的（「拿起手機拍下來」）不連動。
    正式流程用 production_analyzer() 建立，兩項修正都會開啟。
    """

    def __init__(self, lexicon: Lexicon, stocks: list[Stock], name_rules: NameRules | None = None,
                 sector_fallback: str = "total"):
        if sector_fallback not in ("total", "title"):
            raise ValueError("sector_fallback 只能是 total 或 title")
        self.lexicon = lexicon
        self.stocks = list(stocks)
        self.name_rules = name_rules
        self.sector_fallback = sector_fallback
        # 詞條順序與原型 buildEntries() 相同：情緒詞 → 公司名與別名 → 產業詞
        self.entries: list[Match] = []
        for t, w in lexicon.sentiment.items():
            if w != 0:
                self.entries.append(Match(0, 0, t, "pos" if w > 0 else "neg", weight=w))
        for s in self.stocks:
            for t in (s.name, *s.aliases):
                self.entries.append(Match(0, 0, t, "stock", stock=s))
        for t, sec in lexicon.sectors.items():
            self.entries.append(Match(0, 0, t, "sector", sector=sec))

    def find_matches(self, text: str) -> list[Match]:
        found: list[Match] = []
        for en in self.entries:
            if not en.term:
                continue
            i = text.find(en.term)
            while i != -1:
                found.append(Match(i, i + len(en.term), en.term, en.kind, en.weight, en.stock, en.sector))
                i = text.find(en.term, i + 1)
        if self.name_rules is not None:  # 先拿掉沒有佐證的公司短名稱，讓出位置給其他詞
            found = self.name_rules.filter(text, found)
        # 長的優先，同長取前面的；sorted 是穩定排序，同長同位置時保留詞條順序（同原型）
        found.sort(key=lambda m: (-(m.end - m.start), m.start))
        keep: list[Match] = []
        for f in found:
            if not any(f.start < k.end and k.start < f.end for k in keep):
                keep.append(f)
        return sorted(keep, key=lambda m: m.start)

    def analyze(self, title: str, body: str) -> Analysis:
        text = title + "\n" + body
        ms = self.find_matches(text)
        sents = sentence_spans(text)

        def s_idx(pos: int) -> int:
            return next((i for i, (a, b) in enumerate(sents) if a <= pos < b), -1)

        s_score = [0.0] * len(sents)
        hits: list[Hit] = []
        negators: list[tuple[int, int]] = []
        total = 0.0

        for m in ms:
            if m.kind not in ("pos", "neg"):
                continue
            w = m.weight
            before = text[max(0, m.start - NEG_WINDOW):m.start]
            before = before[max(before.rfind(c) for c in NEG_STOP) + 1:]
            neg_w = next((n for n in NEGATORS if n in before and not any(x in before for x in NOT_NEGATORS)), None)
            note = ""
            if neg_w:
                w = -w
                note = "否定"
                p = m.start - len(before) + before.rfind(neg_w)
                negators.append((p, p + len(neg_w)))
            before4 = text[max(0, m.start - BOOST_WINDOW):m.start]
            if any(b in before4 for b in BOOSTERS):
                w *= BOOST_WEIGHT
                note += ("、" if note else "") + "加強"
            if m.start < len(title):
                w *= TITLE_WEIGHT
            m.effective = w
            total += w
            si = s_idx(m.start)
            if si >= 0:
                s_score[si] += w
            hits.append(Hit(m.term, w, note))

        # 直接提及：依第一次出現的順序
        imp: dict[str, Impact] = {}
        for m in ms:
            if m.kind != "stock":
                continue
            cur = imp.setdefault(m.stock.code, Impact(m.stock, "direct"))
            si = s_idx(m.start)
            if si not in cur.sentences:
                cur.sentences.append(si)
        for v in imp.values():
            sc = 0.0
            for si in v.sentences:
                sc += s_score[si]
            if abs(sc) < 0.01:
                sc = total * 0.5
            v.score = sc
            v.key_sentence = sorted(v.sentences, key=lambda si: -abs(s_score[si]))[0]

        # 產業連動
        sec_hits: dict[str, list[int]] = {}
        sec_in_title: set[str] = set()
        for m in ms:
            if m.kind == "sector":
                lst = sec_hits.setdefault(m.sector, [])
                si = s_idx(m.start)
                if si not in lst:
                    lst.append(si)
                if m.start < len(title):
                    sec_in_title.add(m.sector)
        for sec, lst in sec_hits.items():
            sc = 0.0
            for si in lst:
                sc += s_score[si]
            if abs(sc) < 0.01:
                # 關鍵字所在句子沒有情緒：原型改用整則總分；sector_fallback="title" 時只有標題提到才這樣做
                sc = total if self.sector_fallback == "total" or sec in sec_in_title else 0.0
            sc *= SECTOR_SHARE
            if abs(sc) < SECTOR_MIN:
                continue
            members = [s for s in self.stocks if sec in s.sectors and s.code not in imp][:SECTOR_MAX_STOCKS]
            for s in members:
                imp[s.code] = Impact(s, "sector", sc, sector=sec)

        impacts = sorted(imp.values(), key=lambda v: -abs(v.score))
        for v in impacts:
            v.reason = _reason(v, text, sents)
        return Analysis(title, body, text, total, label_of(total), hits, impacts, ms, negators, sents, s_score)


def _reason(v: Impact, text: str, sents: list[tuple[int, int]]) -> str:
    if v.type == "direct":
        s, e = sents[v.key_sentence] if 0 <= v.key_sentence < len(sents) else (0, 0)
        sent = text[s:e].replace("\n", "").strip()
        if len(sent) > 60:
            sent = sent[:60] + "…"
        return f"新聞直接提及，{v.direction}。關鍵句：「{sent}」"
    return f"未直接提及，屬「{v.sector}」族群，產業連動{v.direction}（取一半分數）"


def production_analyzer(lexicon: Lexicon, stocks: list[Stock]) -> Analyzer:
    """正式流程（爬蟲建立預測、命令列分析）用的分析器：開啟所有原型之後加入的修正。"""
    return Analyzer(lexicon, stocks, NameRules.load(), sector_fallback="title")
