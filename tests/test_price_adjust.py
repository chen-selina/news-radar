"""還原股價：除息日不再被算成下跌；網站顯示的價格仍是原始收盤；收盤價對不上就不匯入。"""
import pytest

from radar import db, validate
from radar.prices import adjust
from radar.validate import Row

DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.executemany("INSERT INTO trading_days (date) VALUES (?)", [(d,) for d in DAYS])
    # 9/03 除息 10 元：原始收盤 100 → 90，其實沒有漲跌
    raw = {"2026-09-01": 100.0, "2026-09-02": 100.0, "2026-09-03": 90.0, "2026-09-04": 90.0}
    c.executemany("INSERT INTO prices (date, code, close) VALUES (?, ?, ?)",
                  [(d, "1111", p) for d, p in raw.items()] + [(d, "TAIEX", 1000.0) for d in DAYS])
    c.commit()
    return c


FACTORS = [("2026-09-01", "1111", 100.0, 0.9), ("2026-09-02", "1111", 100.0, 0.9),
           ("2026-09-03", "1111", 90.0, 1.0), ("2026-09-04", "1111", 90.0, 1.0)]


def test_import_checks_closes_then_adjusts_returns(conn):
    rep = adjust.import_factors(conn, FACTORS, "t.csv")
    assert rep["imported"] and rep["mismatch"] == 0
    px = validate._prices(conn, {"1111"})["1111"]
    assert px["2026-09-04"] / px["2026-09-02"] - 1 == pytest.approx(0.0)
    raw = validate._prices(conn, {"1111"}, adjusted=False)["1111"]
    assert raw["2026-09-04"] / raw["2026-09-02"] - 1 == pytest.approx(-0.1)


def test_dates_after_last_factor_carry_it_forward(conn):
    adjust.import_factors(conn, FACTORS[:3], "t.csv")          # 係數只到 9/03
    px = validate._prices(conn, {"1111"})["1111"]
    assert px["2026-09-04"] == pytest.approx(90.0)


def test_mismatched_closes_are_not_imported(conn):
    bad = [(d, c, close * 1.5, f) for d, c, close, f in FACTORS]
    rep = adjust.import_factors(conn, bad, "t.csv")
    assert not rep["imported"] and rep["mismatch"] == 4
    assert conn.execute("SELECT COUNT(*) FROM price_adj").fetchone()[0] == 0


def test_reimport_replaces_old_factors_for_that_stock(conn):
    adjust.import_factors(conn, FACTORS, "a.csv")
    adjust.import_factors(conn, [(d, c, close, 1.0) for d, c, close, _ in FACTORS], "b.csv")
    assert {r[0] for r in conn.execute("SELECT DISTINCT source FROM price_adj")} == {"b.csv"}


def test_validate_returns_use_adjusted_but_show_raw_prices(conn):
    adjust.import_factors(conn, FACTORS, "t.csv")
    row = Row(0, "2026-09-02", "", "", "", "", None, "1111", "1111", "direct", None, 1.0, "up")
    res = validate.validate(conn, horizon=2, rows=[row], entry="given")
    r = res["rows"][0]
    assert r["status"] == "done"
    assert r["ret"] == pytest.approx(0.0)        # 除息不算跌
    assert r["act"] == "flat"
    assert (r["p0"], r["p1"]) == (100.0, 90.0)   # 顯示原始收盤
