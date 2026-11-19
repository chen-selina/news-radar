"""預測驗證：基準價（可成交的收盤、舊規則、休市、資訊可取得時間）、觀察期間、扣除大盤、門檻、命中、指標。

測試用的交易日曆刻意跳過 10/23、10/26（颱風休市）與週末。
"""
import pytest

from radar import db
from radar.targets import available_at, entry_index, local_time
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


def add(conn, published, stocks, nid=[0], fetched="now"):
    """stocks: [(代號, 分數, 方向, 類型)]；fetched：系統抓到的時間（"now" 不能解析，會被略過）"""
    nid[0] += 1
    n = conn.execute("INSERT INTO news (url, source, title, published_at, fetched_at) VALUES (?, 'udn', ?, ?, ?)",
                     (f"https://a.com/{nid[0]}", f"新聞{nid[0]}", published, fetched)).lastrowid
    p = conn.execute("INSERT INTO predictions (news_id, lexicon_version, total_score, label, created_at, stock_count) "
                     "VALUES (?, 1, 0, 'neu', 'now', ?)", (n, len(stocks))).lastrowid
    conn.executemany("INSERT INTO prediction_stocks (prediction_id, stock_code, score, label, type) VALUES (?, ?, ?, ?, ?)",
                     [(p, c, s, l, t) for c, s, l, t in stocks])
    conn.commit()


def test_base_index_rules():
    assert DAYS[base_index(DAYS, "2026-10-21", "08:30")] == "2026-10-20"  # 開盤前 → 前一個交易日
    assert DAYS[base_index(DAYS, "2026-10-21", "09:00")] == "2026-10-21"  # 開盤後（盤中）→ 當日收盤
    assert DAYS[base_index(DAYS, "2026-10-21", "10:00")] == "2026-10-21"  # 盤中報導當天漲跌的新聞，不能拿當天收盤來驗證
    assert DAYS[base_index(DAYS, "2026-10-21", "13:30")] == "2026-10-21"  # 收盤後 → 當日
    assert DAYS[base_index(DAYS, "2026-10-21", "")] == "2026-10-20"       # 沒有時間視為盤前
    assert DAYS[base_index(DAYS, "2026-10-24", "15:00")] == "2026-10-22"  # 週末 → 之前最後一個交易日
    assert DAYS[base_index(DAYS, "2026-10-26", "10:00")] == "2026-10-22"  # 颱風休市
    assert DAYS[base_index(DAYS, "2026-10-27", "08:00")] == "2026-10-22"  # 休市後第一天盤前
    assert base_index(DAYS, "2026-10-30", "08:00") is None                # 還沒有當天之後的資料
    assert base_index(DAYS, "2026-10-29", "10:00") == len(DAYS) - 1       # 最後一個交易日的盤中新聞：基準是當日收盤
    assert base_index(DAYS, "2026-10-01", "08:00") == -1                  # 早於價格資料


def test_entry_index_close_rule():
    """可成交的收盤：資訊在交易日 13:00（含）以前可取得 → 當日收盤；其餘 → 下一個交易日收盤。"""
    e = lambda d, t: DAYS[entry_index(DAYS, d, t)]
    assert e("2026-10-21", "08:59") == "2026-10-21"    # 開盤前：當天收盤買得到（不再用前一天收盤）
    assert e("2026-10-21", "09:00") == "2026-10-21"
    assert e("2026-10-21", "13:00") == "2026-10-21"    # 截止時間含
    assert e("2026-10-21", "13:01") == "2026-10-22"    # 來不及收盤集合競價
    assert e("2026-10-21", "13:30") == "2026-10-22"    # 收盤
    assert e("2026-10-21", "20:00") == "2026-10-22"    # 盤後：不能用已經過去的 13:30 收盤
    assert e("2026-10-21", "") == "2026-10-22"         # 沒有時間：保守，當成收盤後
    assert e("2026-10-24", "10:00") == "2026-10-27"    # 週末 → 下一個交易日
    assert e("2026-10-26", "10:00") == "2026-10-27"    # 颱風休市 → 下一個交易日
    assert entry_index(DAYS, "2026-10-29", "10:00") == len(DAYS) - 1
    assert entry_index(DAYS, "2026-10-29", "15:00") is None      # 下一個交易日的收盤還沒出來
    assert entry_index(DAYS, "2026-10-30", "08:00") is None
    assert entry_index(DAYS, "2026-10-01", "08:00") == -1        # 早於價格資料
    assert entry_index(DAYS, "2026-10-21", "08:00", "legacy") == 1 - 1   # 舊規則還在，只供比較
    assert entry_index(DAYS, "2026-10-21", "", "given") == 1 and entry_index(DAYS, "2026-10-24", "", "given") == -1
    with pytest.raises(ValueError):
        entry_index(DAYS, "2026-10-21", "08:00", "open")


def test_local_time_converts_time_zones():
    assert local_time("2026-10-21T08:30:00+08:00") == ("2026-10-21", "08:30", "ok")
    assert local_time("2026-10-21T01:30:00Z") == ("2026-10-21", "09:30", "ok")          # UTC → 台北
    assert local_time("2026-10-20T20:00:00-05:00") == ("2026-10-21", "09:00", "ok")     # 跨日
    assert local_time("2026-10-21T10:00:00") == ("2026-10-21", "10:00", "naive")       # 沒有時區：當台北
    assert local_time("2026-10-21") == ("2026-10-21", "", "date_only")
    assert local_time(None) == ("", "", "missing")


def test_available_at_takes_the_latest_parseable_time():
    assert available_at("2026-10-21T08:00:00+08:00", "2026-10-24T23:41:00+08:00", "now") == ("2026-10-24", "23:41")
    assert available_at("2026-10-21T08:00:00+08:00", None, "") == ("2026-10-21", "08:00")
    assert available_at("now", None) == ("", "")


def test_live_news_uses_the_time_the_system_first_saw_it(conn):
    # 08:00 發布，但系統 10/22 20:00 才抓到 → 最早只能 10/27 收盤進場（10/23、10/26 休市、中間是週末）
    add(conn, "2026-10-21T08:00:00+08:00", [("1111", 3, "pos", "direct")], fetched="2026-10-22T20:00:00+08:00")
    r = validate(conn, horizon=1)["rows"][0]
    assert (r["date"], r["time"]) == ("2026-10-21", "08:00")          # 顯示的仍是發布時間
    assert (r["avail_date"], r["avail_time"]) == ("2026-10-22", "20:00")
    assert (r["base_date"], r["end_date"]) == ("2026-10-27", "2026-10-28")


def test_legacy_entry_is_kept_for_comparison(conn):
    add(conn, "2026-10-21T08:00:00+08:00", [("1111", 3, "pos", "direct")])
    new, old = validate(conn, horizon=3), validate(conn, horizon=3, entry="legacy")
    assert new["rows"][0]["base_date"] == "2026-10-21" and old["rows"][0]["base_date"] == "2026-10-20"
    assert new["params"]["entry"] == "close" and old["params"]["entry"] == "legacy"
    with pytest.raises(ValueError):
        validate(conn, entry="open")


def test_hit_with_excess_return(conn):
    add(conn, "2026-10-21T08:00:00+08:00", [("1111", 3, "pos", "direct"), ("2222", -2, "neg", "direct")])
    res = validate(conn, horizon=3)
    rows = {r["code"]: r for r in res["rows"]}
    a = rows["1111"]
    assert (a["base_date"], a["end_date"]) == ("2026-10-21", "2026-10-28")  # 08:00 → 當日收盤；第 3 個交易日跳過休市
    assert a["ret"] == pytest.approx(1.03 ** 3 - 1)
    assert a["ex"] == pytest.approx(1.03 ** 3 - 1.01 ** 3)
    assert a["act"] == "up" and a["hit"] is True and rows["2222"]["hit"] is True
    s = res["summary"]
    assert (s["hits"], s["n"], s["hit_rate"]) == (2, 2, 1.0)
    assert s["spread"] == pytest.approx(rows["1111"]["ex"] - rows["2222"]["ex"])


def test_raw_vs_excess_and_threshold(conn):
    add(conn, "2026-10-21T08:00:00+08:00", [("2222", 2, "pos", "direct")])
    # 乙公司 3 日跌約 5.9%：個股漲跌與扣除大盤都是跌 → 利多未命中
    assert validate(conn, horizon=3, benchmark="raw")["rows"][0]["hit"] is False
    # 門檻 10%：跌幅不到門檻 → 持平，仍是未命中
    r = validate(conn, horizon=3, threshold=0.10)["rows"][0]
    assert r["act"] == "flat" and r["hit"] is False


def test_neutral_not_counted(conn):
    add(conn, "2026-10-21T08:00:00+08:00", [("1111", 0.5, "neu", "direct")])
    res = validate(conn, horizon=1)
    assert res["rows"][0]["hit"] is None and res["summary"]["n"] == 0 and res["summary"]["hit_rate"] is None


def test_wait_and_missing_price(conn):
    add(conn, "2026-10-28T08:00:00+08:00", [("1111", 2, "pos", "direct")])   # 基準 10/28，第 5 日還沒到
    add(conn, "2026-10-30T08:00:00+08:00", [("1111", 2, "pos", "direct")])   # 基準日資料還沒出來
    add(conn, "2026-10-21T15:00:00+08:00", [("3333", 2, "pos", "sector")])   # 丙公司停牌
    res = {r["date"]: r for r in validate(conn, horizon=5)["rows"]}
    assert res["2026-10-28"]["status"] == "wait" and res["2026-10-28"]["left"] == 4
    assert res["2026-10-30"]["status"] == "wait_base"
    assert res["2026-10-21"]["status"] == "nodata"


def test_scope_direct_only(conn):
    add(conn, "2026-10-21T08:00:00+08:00", [("1111", 2, "pos", "direct"), ("2222", 1.5, "pos", "sector")])
    assert [r["code"] for r in validate(conn, scope="direct")["rows"]] == ["1111"]
    assert len(validate(conn, scope="all")["rows"]) == 2


def test_strength_buckets_and_event_curve(conn):
    add(conn, "2026-10-22T08:00:00+08:00", [("1111", 4.5, "pos", "direct"), ("2222", -1.2, "neg", "direct")])
    res = validate(conn, horizon=1)
    by = {b["strength"]: b for b in res["by_strength"]}
    assert by["高"]["n"] == 1 and by["低"]["n"] == 1 and by["中"]["n"] == 0
    up = {p["k"]: p for p in res["event_curve"]["up"]}
    assert up[0]["y"] == pytest.approx(0)
    assert up[1]["y"] == pytest.approx(1.03 - 1.01)               # 基準 10/21 → 10/22
    assert up[-1]["y"] == pytest.approx(1 / 1.03 - 1 / 1.01)      # 新聞前一個交易日
    assert up[-3]["n"] == 0                                       # 早於價格資料


def test_recalc_source(conn):
    add(conn, "2026-10-21T08:00:00+08:00", [("1111", 3, "pos", "direct")])
    conn.execute("INSERT INTO recalcs (news_id, lexicon_version, total_score, label, created_at) VALUES (1, 1, -3, 'neg', 'now')")
    conn.execute("INSERT INTO recalc_stocks (news_id, lexicon_version, stock_code, score, label, type) "
                 "VALUES (1, 1, '1111', -3, 'neg', 'direct')")
    assert validate(conn, horizon=1)["rows"][0]["pred"] == "up"
    assert validate(conn, horizon=1, lexicon_version=1)["rows"][0]["pred"] == "down"


def test_without_market_falls_back_to_raw(conn):
    conn.execute("DELETE FROM prices WHERE code = 'TAIEX'")
    add(conn, "2026-10-21T08:00:00+08:00", [("1111", 3, "pos", "direct")])
    res = validate(conn, horizon=1)
    assert res["params"]["benchmark"] == "raw" and res["notes"]
    assert res["rows"][0]["ex"] == pytest.approx(0.03)


def test_bad_params(conn):
    with pytest.raises(ValueError):
        validate(conn, horizon=0)
    with pytest.raises(ValueError):
        validate(conn, benchmark="x")
