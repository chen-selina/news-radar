"""預測驗證：基準價（盤前／盤後、休市）、觀察期間、扣除大盤、門檻、命中、指標。

測試用的交易日曆刻意跳過 10/23、10/26（颱風休市）與週末。
"""
import pytest

from radar import db
from radar.validate import base_index, validate

DAYS = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-27", "2026-10-28", "2026-10-29"]
# 收盤價：TAIEX 每天 +1%；A 每天 +3%；B 每天 −2%；C 第一天之後停牌
TAIEX = [100 * 1.01 ** i for i in range(6)]
A = [100 * 1.03 ** i for i in range(6)]
B = [100 * 0.98 ** i for i in range(6)]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.executemany("INSERT INTO trading_days (date) VALUES (?)", [(d,) for d in DAYS])
    rows = [(d, "TAIEX", TAIEX[i]) for i, d in enumerate(DAYS)]
    rows += [(d, "1111", A[i]) for i, d in enumerate(DAYS)] + [(d, "2222", B[i]) for i, d in enumerate(DAYS)]
    rows += [(DAYS[0], "3333", 50.0)]
    c.executemany("INSERT INTO prices (date, code, close) VALUES (?, ?, ?)", rows)
    c.executemany("INSERT INTO stocks (code, name, market, updated_at) VALUES (?, ?, '上市', 'now')",
                  [("1111", "甲公司"), ("2222", "乙公司"), ("3333", "丙公司")])
    c.execute("INSERT INTO lexicon_versions (version, checksum, created_at) VALUES (1, 'x', 'now')")
    c.commit()
    return c


def add(conn, published, stocks, nid=[0]):
    """stocks: [(代號, 分數, 方向, 類型)]"""
    nid[0] += 1
    n = conn.execute("INSERT INTO news (url, source, title, published_at, fetched_at) VALUES (?, 'udn', ?, ?, 'now')",
                     (f"https://a.com/{nid[0]}", f"新聞{nid[0]}", published)).lastrowid
    p = conn.execute("INSERT INTO predictions (news_id, lexicon_version, total_score, label, created_at, stock_count) "
                     "VALUES (?, 1, 0, 'neu', 'now', ?)", (n, len(stocks))).lastrowid
    conn.executemany("INSERT INTO prediction_stocks (prediction_id, stock_code, score, label, type) VALUES (?, ?, ?, ?, ?)",
                     [(p, c, s, l, t) for c, s, l, t in stocks])
    conn.commit()


def test_base_index_rules():
    assert DAYS[base_index(DAYS, "2026-10-21", "10:00")] == "2026-10-20"  # 盤前 → 前一個交易日
    assert DAYS[base_index(DAYS, "2026-10-21", "13:30")] == "2026-10-21"  # 收盤後 → 當日
    assert DAYS[base_index(DAYS, "2026-10-21", "")] == "2026-10-20"       # 沒有時間視為盤前
    assert DAYS[base_index(DAYS, "2026-10-24", "15:00")] == "2026-10-22"  # 週末 → 之前最後一個交易日
    assert DAYS[base_index(DAYS, "2026-10-26", "09:00")] == "2026-10-22"  # 颱風休市
    assert DAYS[base_index(DAYS, "2026-10-27", "09:00")] == "2026-10-22"  # 休市後第一天盤前
    assert base_index(DAYS, "2026-10-30", "09:00") is None                # 還沒有當天之後的資料
    assert base_index(DAYS, "2026-10-01", "09:00") == -1                  # 早於價格資料


def test_hit_with_excess_return(conn):
    add(conn, "2026-10-21T10:00:00+08:00", [("1111", 3, "pos", "direct"), ("2222", -2, "neg", "direct")])
    res = validate(conn, horizon=3)
    rows = {r["code"]: r for r in res["rows"]}
    a = rows["1111"]
    assert (a["base_date"], a["end_date"]) == ("2026-10-20", "2026-10-27")  # 第 3 個交易日跳過休市
    assert a["ret"] == pytest.approx(1.03 ** 3 - 1)
    assert a["ex"] == pytest.approx(1.03 ** 3 - 1.01 ** 3)
    assert a["act"] == "up" and a["hit"] is True and rows["2222"]["hit"] is True
    s = res["summary"]
    assert (s["hits"], s["n"], s["hit_rate"]) == (2, 2, 1.0)
    assert s["spread"] == pytest.approx(rows["1111"]["ex"] - rows["2222"]["ex"])


def test_raw_vs_excess_and_threshold(conn):
    add(conn, "2026-10-21T10:00:00+08:00", [("2222", 2, "pos", "direct")])
    # 乙公司 3 日跌約 5.9%：個股漲跌與扣除大盤都是跌 → 利多未命中
    assert validate(conn, horizon=3, benchmark="raw")["rows"][0]["hit"] is False
    # 門檻 10%：跌幅不到門檻 → 持平，仍是未命中
    r = validate(conn, horizon=3, threshold=0.10)["rows"][0]
    assert r["act"] == "flat" and r["hit"] is False


def test_neutral_not_counted(conn):
    add(conn, "2026-10-21T10:00:00+08:00", [("1111", 0.5, "neu", "direct")])
    res = validate(conn, horizon=1)
    assert res["rows"][0]["hit"] is None and res["summary"]["n"] == 0 and res["summary"]["hit_rate"] is None


def test_wait_and_missing_price(conn):
    add(conn, "2026-10-28T09:00:00+08:00", [("1111", 2, "pos", "direct")])   # 基準 10/27，第 5 日還沒到
    add(conn, "2026-10-30T09:00:00+08:00", [("1111", 2, "pos", "direct")])   # 基準日資料還沒出來
    add(conn, "2026-10-21T15:00:00+08:00", [("3333", 2, "pos", "sector")])   # 丙公司停牌
    res = {r["date"]: r for r in validate(conn, horizon=5)["rows"]}
    assert res["2026-10-28"]["status"] == "wait" and res["2026-10-28"]["left"] == 3
    assert res["2026-10-30"]["status"] == "wait_base"
    assert res["2026-10-21"]["status"] == "nodata"


def test_scope_direct_only(conn):
    add(conn, "2026-10-21T10:00:00+08:00", [("1111", 2, "pos", "direct"), ("2222", 1.5, "pos", "sector")])
    assert [r["code"] for r in validate(conn, scope="direct")["rows"]] == ["1111"]
    assert len(validate(conn, scope="all")["rows"]) == 2


def test_strength_buckets_and_event_curve(conn):
    add(conn, "2026-10-22T10:00:00+08:00", [("1111", 4.5, "pos", "direct"), ("2222", -1.2, "neg", "direct")])
    res = validate(conn, horizon=1)
    by = {b["strength"]: b for b in res["by_strength"]}
    assert by["高"]["n"] == 1 and by["低"]["n"] == 1 and by["中"]["n"] == 0
    up = {p["k"]: p for p in res["event_curve"]["up"]}
    assert up[0]["y"] == pytest.approx(0)
    assert up[1]["y"] == pytest.approx(1.03 - 1.01)               # 基準 10/21 → 10/22
    assert up[-1]["y"] == pytest.approx(1 / 1.03 - 1 / 1.01)      # 新聞前一個交易日
    assert up[-3]["n"] == 0                                       # 早於價格資料


def test_recalc_source(conn):
    add(conn, "2026-10-21T10:00:00+08:00", [("1111", 3, "pos", "direct")])
    conn.execute("INSERT INTO recalcs (news_id, lexicon_version, total_score, label, created_at) VALUES (1, 1, -3, 'neg', 'now')")
    conn.execute("INSERT INTO recalc_stocks (news_id, lexicon_version, stock_code, score, label, type) "
                 "VALUES (1, 1, '1111', -3, 'neg', 'direct')")
    assert validate(conn, horizon=1)["rows"][0]["pred"] == "up"
    assert validate(conn, horizon=1, lexicon_version=1)["rows"][0]["pred"] == "down"


def test_without_market_falls_back_to_raw(conn):
    conn.execute("DELETE FROM prices WHERE code = 'TAIEX'")
    add(conn, "2026-10-21T10:00:00+08:00", [("1111", 3, "pos", "direct")])
    res = validate(conn, horizon=1)
    assert res["params"]["benchmark"] == "raw" and res["notes"]
    assert res["rows"][0]["ex"] == pytest.approx(0.03)


def test_bad_params(conn):
    with pytest.raises(ValueError):
        validate(conn, horizon=0)
    with pytest.raises(ValueError):
        validate(conn, benchmark="x")
