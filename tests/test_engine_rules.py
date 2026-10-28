"""分析規則的白話測試：PROJECT_BRIEF.md 第 5 節的例子逐條寫成測試。"""
import pytest

from radar.analysis.engine import Analyzer, Stock, label_of, strength
from radar.analysis.lexicon import Lexicon

LEX = Lexicon(
    {"砍單": -2.5, "上漲": 1, "油價上漲": -1.5, "產能過剩": -2, "營收成長": 2, "跌停": -1.5, "看好": 1},
    {"DRAM": "記憶體", "HBM": "記憶體"},
)
STOCKS = [
    Stock("1303", "南亞", (), ("塑化",)),
    Stock("2408", "南亞科", (), ("記憶體",)),
    Stock("2330", "台積電", ("台積",), ("晶圓代工",)),
]
A = Analyzer(LEX, STOCKS)


def terms(a):
    return [m.term for m in a.matches]


def test_negation_flips_score():
    assert A.analyze("", "未見砍單。").total == 2.5
    assert A.analyze("", "降低產能過剩風險。").total == 2


def test_not_negation_words():
    assert A.analyze("", "不斷營收成長。").total == 2


def test_longest_match_wins():
    assert terms(A.analyze("", "南亞科跌停。")) == ["南亞科", "跌停"]
    a = A.analyze("", "油價上漲。")
    assert terms(a) == ["油價上漲"] and a.total == -1.5


def test_boost_and_title_weight():
    assert A.analyze("", "大幅營收成長。").total == 3
    assert A.analyze("營收成長", "").total == 3
    assert A.analyze("大幅營收成長", "").total == 4.5


def test_direct_mention_uses_its_sentences():
    a = A.analyze("", "台積營收成長。南亞科跌停。")
    s = {v.stock.code: v.score for v in a.impacts}
    assert s == {"2330": 2, "2408": -1.5}
    assert all(v.type == "direct" for v in a.impacts)
    assert "台積營收成長" in a.impacts[0].reason


def test_direct_mention_without_sentiment_takes_half_total():
    a = A.analyze("營收成長", "台積電今天開董事會。")
    assert a.impacts[0].score == 1.5  # 總分 3 的一半


def test_sector_linkage_takes_half_and_is_labelled():
    a = A.analyze("", "DRAM 營收成長。")
    assert [(v.stock.code, v.type, v.sector, v.score) for v in a.impacts] == [("2408", "sector", "記憶體", 1)]
    assert "記憶體" in a.impacts[0].reason and "產業連動" in a.impacts[0].reason


def test_sector_picks_first_four_in_given_order():
    many = [Stock(str(1000 + i), f"記憶{i}號", (), ("記憶體",), market_cap=100 - i) for i in range(6)]
    a = Analyzer(LEX, many).analyze("", "HBM 營收成長。")
    assert [v.stock.code for v in a.impacts] == ["1000", "1001", "1002", "1003"]


SECTOR_LEX = Lexicon({"爆賣": 2, "上漲": 1}, {"手機": "手機", "DRAM": "記憶體"})
PHONE = [Stock("2454", "聯發科", (), ("手機",)), Stock("2408", "南亞科", (), ("記憶體",))]


def test_prototype_sector_fallback_uses_total():
    # 原型：關鍵字所在句子沒有情緒，就拿整則總分的一半
    a = Analyzer(SECTOR_LEX, PHONE).analyze("甜甜圈爆賣", "有人拿起手機拍下來。")
    assert [(v.stock.code, v.score) for v in a.impacts] == [("2454", 1.5)]


def test_title_mode_ignores_passing_mentions_in_body():
    a = Analyzer(SECTOR_LEX, PHONE, sector_fallback="title").analyze("甜甜圈爆賣", "有人拿起手機拍下來。")
    assert a.impacts == []


def test_title_mode_keeps_keyword_in_title():
    a = Analyzer(SECTOR_LEX, PHONE, sector_fallback="title").analyze("DRAM題材熱夯", "大盤上漲。")
    assert [(v.stock.code, v.score) for v in a.impacts] == [("2408", 0.5)]


def test_title_mode_still_uses_sentence_score():
    a = Analyzer(SECTOR_LEX, PHONE, sector_fallback="title").analyze("", "DRAM 報價上漲。")
    assert [(v.stock.code, v.score) for v in a.impacts] == [("2408", 0.5)]


def test_bad_sector_fallback_rejected():
    with pytest.raises(ValueError):
        Analyzer(SECTOR_LEX, PHONE, sector_fallback="all")


def test_production_analyzer_turns_on_both_fixes():
    from radar.analysis.engine import production_analyzer
    an = production_analyzer(SECTOR_LEX, PHONE)
    assert an.sector_fallback == "title" and an.name_rules is not None


def test_label_and_strength_thresholds():
    assert [label_of(x) for x in (1, 0.99, -0.99, -1)] == ["pos", "neu", "neu", "neg"]
    assert [strength(x) for x in (4, -3.9, 2, 1.99)] == ["高", "中", "中", "低"]


def test_zero_weight_term_is_ignored():
    a = Analyzer(Lexicon({"擴大": 0}, {}), []).analyze("", "擴大。")
    assert a.matches == [] and a.total == 0


@pytest.mark.parametrize("title,body", [("", ""), ("只有標題", ""), ("", "只有內文")])
def test_handles_empty_parts(title, body):
    assert A.analyze(title, body).label == "neu"


def test_to_dict_is_json_ready():
    import json
    d = A.analyze("台積電營收成長", "DRAM 上漲。").to_dict()
    json.dumps(d, ensure_ascii=False)
    assert d["label_text"] == "利多" and d["impacts"][0]["name"] == "台積電"
