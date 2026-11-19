"""公司對應的修正（2026-11-19，P0-3 抽查發現）：異體字、括號代號、一般用語撞名、讀取時補別名。"""
import json

import pytest

from radar import db
from radar.analysis.engine import Analyzer, Stock, production_analyzer
from radar.analysis.lexicon import Lexicon
from radar.analysis.names import NameRules
from radar.stocks.query import all_stocks

LEX = Lexicon({"看好": 1}, {})
STOCKS = [Stock("2308", "台達電"), Stock("8044", "網家"), Stock("6274", "台燿"), Stock("2330", "台積電", ("台積",)),
          Stock("2365", "昆盈"), Stock("2834", "臺企銀"), Stock("7828", "創新服務"), Stock("2891", "中信金"),
          Stock("2027", "大成鋼"), Stock("3661", "世芯-KY", ("世芯",))]
FIXED = Analyzer(LEX, STOCKS, NameRules.load(), variants=True, code_mentions=True)
PROTO = Analyzer(LEX, STOCKS)


def codes(a, text):
    return sorted({m.stock.code for m in a.find_matches(text) if m.kind == "stock"})


def test_tai_variant_matches_both_ways():
    assert codes(FIXED, "臺達電(2308)盤中強彈") == ["2308"]
    assert codes(FIXED, "台企銀調升存款利率") == ["2834"]          # 官方名稱用「臺」，新聞寫「台」
    assert codes(PROTO, "臺達電(2308)盤中強彈") == []              # 原型行為不變（比對原型的測試要用）


def test_code_in_parentheses_when_name_is_not_recognised():
    assert codes(FIXED, "網路家庭（8044）10月合併營收31.6億元") == ["8044"]
    assert codes(FIXED, "看好台光電及台耀（6274）受惠") == ["6274"]      # 媒體寫錯字
    assert codes(FIXED, "IP 供應商 M31 (6643-TW) 宣布") == []           # 不在名單裡的代號不算


def test_code_does_not_override_a_recognised_name():
    assert codes(FIXED, "台積電（2365）10月營收創新高") == ["2330"]      # 代號誤植，不多抓昆盈
    assert codes(FIXED, "世芯 - KY(3661-TW) 與光程研創合資") == ["3661"]


def test_years_in_parentheses_are_not_codes():
    assert codes(FIXED, "法說會於115年（2027）舉行") == []               # 前面是「年」
    assert codes(FIXED, "預估明（2027）年獲利成長") == []                # 去／今／明（年份）年
    assert codes(FIXED, "電子材料營收占比從去(2025)年的46%") == []
    assert codes(FIXED, "財報（2027）") == ["2027"]                       # 已知限制：前面是中文就會當代號


def test_generic_phrase_company_needs_evidence():
    assert codes(FIXED, "中信金近年推動逾百項數位創新服務，廣泛應用") == ["2891"]
    assert codes(FIXED, "創新服務（7828）公布營收") == ["7828"]


def test_all_stocks_adds_suffix_and_manual_aliases_on_read():
    c = db.connect(":memory:")
    c.executemany("INSERT INTO stocks (code, name, market, aliases, updated_at) VALUES (?, ?, '上市', ?, 'now')",
                  [("6854", "錼創科技-KY創", "[]"), ("2330", "台積電", json.dumps(["台積"], ensure_ascii=False))])
    s = {x.code: x for x in all_stocks(c)}
    assert set(s["6854"].aliases) >= {"錼創科技", "錼創", "錼創-KY"}       # 後綴規則＋stock_overrides.json
    assert s["2330"].aliases.count("台積") == 1                          # 不重複
    an = production_analyzer(LEX, list(s.values()))
    assert [m.stock.code for m in an.find_matches("錼創-KY受激勵逾半根停板") if m.kind == "stock"] == ["6854"]
