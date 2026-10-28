"""公司詞典載入：用官方資料的真實片段（tests/fixtures/stocks）測試，不連網。"""
import json
from pathlib import Path

import pytest

from radar import db
from radar.stocks import load, sources

FIX = Path(__file__).parent / "fixtures" / "stocks"
NO_MIN = {}  # 測試資料只有幾家公司，關掉「家數過少」的保護


@pytest.fixture
def raw():
    return load.read_raw(FIX)


def by_code(rows):
    return {r["code"]: r for r in rows}


def test_roc_date():
    assert sources.roc_to_iso("1151022") == "2026-10-22"
    assert sources.roc_to_iso("115/10/27") == "2026-10-27"
    assert sources.roc_to_iso("99-1-5") == "2010-01-05"
    assert sources.roc_to_iso("") is None


def test_both_markets_and_official_industry(raw):
    rows = by_code(load.build_rows(raw, {}, NO_MIN)[0])
    assert rows["2330"]["market"] == "上市" and rows["2330"]["industry"] == "半導體業"
    assert rows["5347"]["market"] == "上櫃" and rows["5347"]["name"] == "世界"
    assert rows["2610"]["industry"] == "航運業"
    assert json.loads(rows["2330"]["sectors"]) == ["半導體業"]


def test_market_cap_is_shares_times_close(raw):
    r = by_code(load.build_rows(raw, {}, NO_MIN)[0])["2330"]
    assert r["market_cap"] == round(r["shares"] * r["close"])
    assert r["price_date"] == "2026-10-22"


def test_missing_price_leaves_market_cap_empty(raw):
    r = by_code(load.build_rows(raw, {}, NO_MIN)[0])["1303"]  # 測試資料刻意不放南亞的收盤價
    assert r["close"] is None and r["market_cap"] is None


def test_suffix_alias():
    assert load.auto_aliases("臻鼎-KY") == ["臻鼎"]
    assert load.auto_aliases("華義*") == ["華義"]
    assert load.auto_aliases("台積電") == []


def test_overrides_add_aliases_and_sectors(raw):
    ov = {"2330": {"name": "台積電", "aliases": ["台積"], "sectors": ["晶圓代工", "先進封裝"]},
          "4958": {"name": "臻鼎-KY", "aliases": ["臻鼎"], "sectors": ["PCB"]}}
    rows, warnings = load.build_rows(raw, ov, NO_MIN)
    rows = by_code(rows)
    assert json.loads(rows["2330"]["aliases"]) == ["台積"]
    assert json.loads(rows["2330"]["sectors"]) == ["半導體業", "晶圓代工", "先進封裝"]
    assert json.loads(rows["4958"]["aliases"]) == ["臻鼎"]  # 自動別名與人工別名重複時只留一個
    assert warnings == []


def test_override_warnings(raw):
    ov = {"9999": {"name": "不存在"}, "2330": {"name": "台積"}}
    _, warnings = load.build_rows(raw, ov, NO_MIN)
    assert any("9999" in w for w in warnings)
    assert any("官方簡稱是「台積電」" in w for w in warnings)


def test_refuses_incomplete_official_data(raw):
    with pytest.raises(RuntimeError, match="不完整"):
        load.build_rows(raw, {})  # 使用預設家數門檻


def test_save_rows_replaces_table(raw):
    conn = db.connect(":memory:")
    rows = load.build_rows(raw, {}, NO_MIN)[0]
    load.save_rows(conn, rows)
    load.save_rows(conn, rows[:2])
    assert conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0] == 2


def test_project_overrides_file_is_valid():
    ov = load.load_overrides()
    assert len(ov) >= 59
    for code, v in ov.items():
        assert code.isdigit(), code
        assert isinstance(v.get("aliases", []), list) and isinstance(v.get("sectors", []), list)
