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


def test_production_analyzer_turns_on_all_fixes():
    from radar.analysis.engine import production_analyzer
    an = production_analyzer(SECTOR_LEX, PHONE)
    assert an.sector_fallback == "title" and an.name_rules is not None
    assert (an.repeat_decay, an.direct_scope, an.direct_fallback) == (0.5, "segment", "title")


# ---- 2026-11-10 加入的三項修正（預設關閉，行為與原型相同） ----
FLOW_LEX = Lexicon({"買超": 1, "賣超": -1, "跌停": -1.5, "下跌": -1, "漲停": 1.5, "年增": 1, "新高": 1.5}, {})
FLOW = [Stock("3481", "群創"), Stock("2303", "聯電"), Stock("1802", "台玻"), Stock("2610", "華航"),
        Stock("5269", "祥碩"), Stock("6446", "藥華藥"), Stock("2383", "台光電"), Stock("2345", "智邦")]
SEG = Analyzer(FLOW_LEX, FLOW, direct_scope="segment", direct_fallback="title")


def scores(a):
    return {v.stock.name: v.score for v in a.impacts}


def test_repeat_decay_halves_each_repeat():
    body = "買超。買超。買超。"
    assert Analyzer(FLOW_LEX, []).analyze("", body).total == 3           # 原型：出現幾次加幾次
    a = Analyzer(FLOW_LEX, [], repeat_decay=0.5).analyze("", body)
    assert a.total == 1.75 and [h.w for h in a.hits] == [1, 0.5, 0.25]   # 1 + 0.5 + 0.25


def test_repeat_decay_counts_biggest_first_and_per_term():
    a = Analyzer(FLOW_LEX, [], repeat_decay=0.5).analyze("買超", "買超。賣超。")
    assert [h.w for h in a.hits] == [1.5, 0.5, -1]   # 標題的 1.5 先算；賣超是另一個詞，不受影響


def test_prototype_gives_whole_sentence_to_every_company():
    a = Analyzer(FLOW_LEX, FLOW).analyze("", "群創遭賣超1.9萬張，台玻獲買超8.3萬張。")
    assert scores(a) == {"群創": 0, "台玻": 0}   # 買超與賣超互相抵銷，再退回總分的一半（也是 0）


def test_segment_gives_each_company_its_own_clause():
    a = SEG.analyze("", "群創遭賣超1.9萬張，聯電遭賣超1.4萬張，台玻、華航分別獲買超8.3萬張及5.8萬張。")
    assert scores(a) == {"群創": -1, "聯電": -1, "台玻": 1, "華航": 1}
    assert "台玻、華航分別獲買超" in a.impacts[2].reason and "群創" not in a.impacts[2].reason


def test_segment_clause_without_company_belongs_to_previous_company():
    a = SEG.analyze("", "台光電營收244億元，年增突破200%，智邦營收也同創新高。")
    assert scores(a) == {"台光電": 1, "智邦": 1.5}


def test_segment_keeps_whole_sentence_for_single_company():
    a = SEG.analyze("", "祥碩開盤走弱，午盤後跌停，終場下跌一成。")
    assert scores(a) == {"祥碩": -2.5}


def test_direct_fallback_title_only():
    body = "漲幅前5名個股為祥碩、藥華藥。大盤下跌。"
    assert scores(Analyzer(FLOW_LEX, FLOW).analyze("", body)) == {"祥碩": -0.5, "藥華藥": -0.5}   # 原型：總分的一半
    assert scores(SEG.analyze("", body)) == {"祥碩": 0, "藥華藥": 0}                                # 只是被提到，不給方向
    assert scores(SEG.analyze("祥碩董事會", "大盤下跌。")) == {"祥碩": -0.5}                        # 標題主角仍取一半


@pytest.mark.parametrize("kw", [{"repeat_decay": 0}, {"repeat_decay": 1.5}, {"direct_scope": "x"}, {"direct_fallback": "x"}])
def test_bad_options_rejected(kw):
    with pytest.raises(ValueError):
        Analyzer(FLOW_LEX, FLOW, **kw)


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
