"""Python 分析引擎與原型 analyze() 逐項比對。

標準答案由 node tools/make_golden.js 從 news-radar.html 產生（tests/fixtures/analysis/golden.json），
包含原型的 15 則範例新聞與每條規則的邊界案例。
"""
import html
import json
from pathlib import Path

import pytest

from radar.analysis.engine import Analyzer, Stock
from radar.analysis.lexicon import Lexicon, load_file

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "analysis" / "golden.json").read_text(encoding="utf-8"))
STOCKS = [Stock(s["code"], s["name"], tuple(s["aliases"]), tuple(s["sectors"])) for s in GOLDEN["stocks"]]
BASE = Lexicon({t: float(w) for t, w in GOLDEN["sentiment"]}, dict(GOLDEN["sectors"]))
approx = lambda x: pytest.approx(x, abs=1e-9)


def analyzer_for(custom: dict) -> Analyzer:
    return Analyzer(Lexicon({**BASE.sentiment, **{k: float(v) for k, v in custom.items()}}, BASE.sectors), STOCKS)


@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: c["id"][-28:])
def test_matches_prototype(case):
    a = analyzer_for(case["custom"]).analyze(case["title"], case["body"])
    exp = case["result"]

    assert [(m.start, m.end, m.term, m.kind) for m in a.matches] == \
           [(m["start"], m["end"], m["term"], m["kind"]) for m in exp["matches"]]
    assert [list(s) for s in a.sentences] == exp["sentences"]
    assert a.sentence_scores == [approx(x) for x in exp["sentence_scores"]]
    assert [(h.term, h.note) for h in a.hits] == [(h["term"], h["note"]) for h in exp["hits"]]
    assert [h.w for h in a.hits] == [approx(h["w"]) for h in exp["hits"]]
    assert [{"start": s, "end": e} for s, e in a.negators] == exp["negators"]
    assert a.total == approx(exp["total"])
    assert a.label == exp["label"]

    got = [(v.stock.code, v.type, v.sector, v.key_sentence) for v in a.impacts]
    want = [(v["code"], v["type"], v["sector"], v["key_sentence"]) for v in exp["impacts"]]
    assert got == want
    assert [v.score for v in a.impacts] == [approx(v["score"]) for v in exp["impacts"]]
    # 原型的說明文字經過 HTML 跳脫；後端存純文字，由前端負責跳脫
    assert [v.reason for v in a.impacts] == [html.unescape(v["reason"]) for v in exp["impacts"]]


def test_config_lexicon_equals_prototype():
    """config/lexicon.json 的初版必須和原型的 BASE_LEX、SECTOR_KW 完全一樣（含順序）。"""
    lex = load_file()
    assert list(lex.sentiment.items()) == list(BASE.sentiment.items())
    assert list(lex.sectors.items()) == list(BASE.sectors.items())


def test_golden_covers_every_rule():
    notes = {h["note"] for c in GOLDEN["cases"] for h in c["result"]["hits"]}
    assert {"否定", "加強", "否定、加強"} <= notes
    types = {v["type"] for c in GOLDEN["cases"] for v in c["result"]["impacts"]}
    assert types == {"direct", "sector"}
    assert any(c["custom"] for c in GOLDEN["cases"])
