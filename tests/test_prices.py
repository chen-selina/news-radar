"""收盤價：官方資料解析（tests/fixtures/prices 是官方回應的片段）、抓取流程、CSV 匯入。"""
import json
from datetime import date
from pathlib import Path

import pytest

from radar import db
from radar.prices import sources, update

FIX = Path(__file__).parent / "fixtures" / "prices"
load = lambda name: json.loads((FIX / name).read_text(encoding="utf-8"))


def test_parse_fmtqik_calendar_and_taiex():
    cal = sources.parse_fmtqik(load("fmtqik.json"))
    assert list(cal) == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-27"]
    assert "2026-10-23" not in cal and "2026-10-26" not in cal  # 颱風休市
    assert cal["2026-10-27"] == 46096.23


def test_parse_mi_index_skips_no_trade():
    closes = sources.parse_mi_index(load("mi_index.json"))
    assert closes["2330"] > 0 and "9999" not in closes  # 收盤價「--」的略過


def test_parse_tpex():
    closes = sources.parse_tpex(load("tpex_otc.json"))
    assert closes["5347"] == 161.5 and "9998" not in closes


def test_parse_reports_changed_fields():
    with pytest.raises(KeyError, match="找不到欄位"):
        sources.parse_fmtqik({"stat": "OK", "fields": ["日期", "收盤"], "data": []})


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.executemany("INSERT INTO stocks (code, name, market, updated_at) VALUES (?, ?, ?, 'now')",
                  [("2330", "台積電", "上市"), ("5347", "世界", "上櫃")])
    c.commit()
    return c


def test_update_fetches_only_missing_days(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "fetch_month", lambda d: {"2026-10-21": 46887.77, "2026-10-22": 46041.43})
    monkeypatch.setattr(sources, "fetch_twse_day", lambda d: calls.append(("twse", d)) or {"2330": 2400.0, "0050": 60.0})
    monkeypatch.setattr(sources, "fetch_tpex_day", lambda d: calls.append(("tpex", d)) or {"5347": 160.0})
    rep = update.update(conn, date(2026, 10, 20), date(2026, 10, 28), log=lambda *_: None)
    assert rep["errors"] == [] and len(calls) == 4
    assert conn.execute("SELECT COUNT(*) FROM prices WHERE code = '0050'").fetchone()[0] == 0  # 只存公司
    assert conn.execute("SELECT close FROM prices WHERE code = 'TAIEX' AND date = '2026-10-22'").fetchone()[0] == 46041.43
    calls.clear()
    update.update(conn, date(2026, 10, 20), date(2026, 10, 28), log=lambda *_: None)
    assert calls == []  # 已抓過的日子不重抓


def test_update_keeps_going_when_a_day_is_missing(conn, monkeypatch):
    monkeypatch.setattr(sources, "fetch_month", lambda d: {"2026-10-27": 46096.23})
    monkeypatch.setattr(sources, "fetch_twse_day", lambda d: {})  # 官方還沒公布
    monkeypatch.setattr(sources, "fetch_tpex_day", lambda d: {"5347": 161.5})
    rep = update.update(conn, date(2026, 10, 27), date(2026, 10, 27), log=lambda *_: None)
    assert any("尚未公布" in e for e in rep["errors"])
    assert conn.execute("SELECT market FROM price_loads").fetchall()[0][0] == "tpex"  # 上市那天下次會重抓


def test_import_csv_roc_dates_and_taiex(conn):
    text = "日期,代號,收盤價\n2026/09/01,2330,1985\n115/09/01,2408,\"1,182.5\"\n2026-09-01,加權指數,32150.6\n亂寫,1,2\n"
    rep = update.import_csv(conn, text)
    assert (rep["rows"], rep["taiex_days"], len(rep["errors"])) == (3, 1, 1)
    assert conn.execute("SELECT close FROM prices WHERE code = '2408'").fetchone()[0] == 1182.5
    assert conn.execute("SELECT date FROM trading_days").fetchall()[0][0] == "2026-09-01"


def test_normalize_date():
    assert update.normalize_date("115/10/27") == "2026-10-27"
    assert update.normalize_date("2026-9-1") == "2026-09-01"
    assert update.normalize_date("x") is None
