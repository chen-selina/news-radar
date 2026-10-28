"""公司短名稱的排除規則（config/name_rules.json＋radar/analysis/names.py）。

句子是依真實新聞常見寫法自行編寫的例子。
"""
import json

import pytest

from radar.analysis.engine import Analyzer, Stock
from radar.analysis.lexicon import Lexicon
from radar.analysis.names import NAME_RULES_PATH, NameRules

LEX = Lexicon({"跌停": -1.5, "法說": 0.5, "營收成長": 2, "看好": 1}, {"記憶體": "記憶體"})
STOCKS = [
    Stock("2337", "旺宏", (), ("記憶體",)), Stock("6239", "力成", (), ("記憶體",)),
    Stock("3167", "大量"), Stock("1109", "信大"), Stock("8923", "時報"), Stock("1216", "統一"),
    Stock("1303", "南亞"), Stock("2408", "南亞科", (), ("記憶體",)), Stock("1802", "台玻"),
    Stock("5392", "能率"), Stock("2374", "佳能"), Stock("8021", "尖點"), Stock("6510", "精測"),
    Stock("3045", "台灣大", ("台灣大哥大",)), Stock("2330", "台積電", ("台積",)), Stock("8112", "至上"),
]
RULES = NameRules.load()
ON = Analyzer(LEX, STOCKS, RULES)
OFF = Analyzer(LEX, STOCKS)


def codes(a, text):
    return [m.stock.code for m in a.find_matches(text) if m.kind == "stock"]


@pytest.mark.parametrize("text", [
    "收盤仍有大量賣單掛在跌停價。",
    "投信大買236億元撐盤。",
    "AI算力成本回收壓力大。",
    "截至上週止的統計。",
    "工商時報報導指出。",
    "企業競相採用統一化的系統。",
    "台灣大型企業的成長。",
])
def test_common_words_are_not_companies(text):
    assert codes(OFF, text), "沒有規則時應該會誤判（確認測試句有意義）"
    assert codes(ON, text) == []


@pytest.mark.parametrize("text,code,why", [
    ("能率（5392）是豐田機器人夥伴。", "5392", "代號"),
    ("2468 華經與能率(5392)公告。", "5392", "代號"),
    ("法人鎖定【精測】。", "6510", "括號"),
    ("旺宏、力成下午舉行法說會。", "6239", "列舉"),
    ("能率、佳能搶機器人商機。", "2374", "列舉"),
    ("台玻逆勢獲外資買超。", "1802", "語境"),
    ("鑽針廠尖點 9 月營收年增 98%。", "8021", "語境"),
    ("法人調節大量持股。", "3167", "語境"),
])
def test_confirmed_mentions_count(text, code, why):
    assert code in codes(ON, text), why


def test_trusted_names_and_manual_aliases_need_no_evidence():
    assert codes(ON, "台積尾盤拉一把。") == ["2330"]  # 人工別名
    assert "2337" in codes(ON, "旺宏今天開盤。")      # 免佐證名單
    assert codes(ON, "台灣大哥大推出新方案。") == ["3045"]


def test_confirmed_once_counts_everywhere_in_same_news():
    text = "能率（5392）宣布擴產。分析師認為能率明年可期。"
    assert codes(ON, text) == ["5392", "5392"]
    assert codes(ON, "分析師認為能率明年可期。") == []


def test_exclude_phrase_wins_even_if_company_confirmed():
    text = "南亞衝高後拉回。南亞計畫在東南亞擴廠。"
    starts = [m.start for m in ON.find_matches(text) if m.kind == "stock"]
    assert len(starts) == 2 and text.index("東南亞") + 1 not in starts


def test_longer_name_is_not_evidence_for_shorter():
    # 「南亞科」裡的「南亞」不能讓後面單獨出現的「南亞」被當成已確認
    assert codes(ON, "南亞科跌停。東南亞市場") == ["2408"]


def test_rejected_name_frees_text_for_other_terms():
    # 沒有規則時，「大量」不影響情緒分數；有規則時分數相同、但不會多出個股
    a_on, a_off = ON.analyze("", "收盤仍有大量賣單掛在跌停價。"), OFF.analyze("", "收盤仍有大量賣單掛在跌停價。")
    assert a_on.total == a_off.total == -1.5
    assert [v.stock.code for v in a_off.impacts] == ["3167"] and a_on.impacts == []


def test_without_rules_engine_matches_prototype():
    assert Analyzer(LEX, STOCKS).name_rules is None


def test_config_file_is_valid():
    d = json.loads(NAME_RULES_PATH.read_text(encoding="utf-8"))
    assert len(d["trusted"]) == len(set(d["trusted"]))
    assert all(len(n) == 2 for n in d["trusted"])
    assert "台積" in RULES.trusted  # 從 stock_overrides.json 帶入的人工別名
    for name in ("南亞", "統一", "大量", "創意", "力成"):
        assert name not in d["trusted"], f"{name} 容易混淆，不該免佐證"
