"""同事件新聞分群（radar/dedupe.py）與驗證時「一檔個股一天算一筆」（validate 的 dedupe）。"""
import pytest

from radar import db
from radar.dedupe import bigrams, event_groups, similarity
from radar.validate import Row, collapse, validate

T = "2026-11-10T20:00:00+08:00"


def groups(*news):
    return event_groups([(i + 1, title, t) for i, (title, t) in enumerate(news)])


def test_bigrams_ignore_punctuation_case_and_outlet_suffix():
    assert bigrams("台積電配息9元！ - 經濟日報") == bigrams("台積電，配息 9 元")
    assert bigrams("OpenAI 入股") == bigrams("openai入股")
    assert bigrams("漲") == frozenset()


def test_same_event_from_different_outlets_is_one_group():
    g = groups(("台積電投資Anthropic、OpenAI 各砸5億美元", T),
               ("台積電投資Anthropic和OpenAI 各5億美元", "2026-11-10T20:20:00+08:00"),
               ("重磅／台積電宣布認購Anthropic、OpenAI股票各5億美元", "2026-11-10T20:12:00+08:00"),
               ("聯發科10月營收550.26億元！AI、ASIC助力Q4挑戰新高", T))
    assert g[1] == g[2] == g[3] == 1 and g[4] == 4


def test_groups_chain_through_a_shared_article():
    # 甲只講配息、丙只講投資，乙兩件事都講：三則是同一場董事會
    g = groups(("台積電董事會決議每股配息9元創新高", T),
               ("台積電每股配息9元創新高，另投資OpenAI最多10億美元", T),
               ("台積電宣布投資OpenAI最多10億美元", T))
    assert len(set(g.values())) == 1


def test_different_events_about_same_company_stay_apart():
    g = groups(("台積電10月營收創新高", T), ("台積電遭外資連23賣", T), ("台積電核准1.34兆元資本預算", T))
    assert len(set(g.values())) == 3


def test_same_title_far_apart_in_time_is_not_merged():
    g = groups(("三大法人賣超台股148億元", "2026-11-10T15:00:00+08:00"),
               ("三大法人賣超台股148億元", "2026-11-13T15:00:00+08:00"))
    assert g[1] != g[2]


def test_short_titles_sharing_only_a_name_are_not_merged():
    assert similarity(bigrams("台積電大漲"), bigrams("台積電重挫")) == 0.0


def test_news_without_time_needs_identical_title():
    g = groups(("台積電配息9元創新高", None), ("台積電配息9元創新高", T), ("台積電配息9元創新高再投資OpenAI十億美元", None))
    assert g[1] == g[2] and g[3] != g[1]


# ---------- 驗證：一檔個股一天算一筆 ----------

DAYS = ["2026-11-09", "2026-11-10", "2026-11-11"]


def row(nid, title, score, time="20:00", code="1111", typ="direct", date="2026-11-10"):
    return Row(nid, date, time, title, f"u{nid}", "yahoo", None, code, "甲", typ, None, score,
               "up" if score >= 1 else "down" if score <= -1 else "flat")


def test_collapse_counts_one_event_once_and_averages_events():
    rows = [row(1, "甲公司董事會決議每股配息9元創新高", 3.0), row(2, "甲公司每股配息9元創新高股民嗨翻", 1.5),
            row(3, "甲公司遭外資大舉賣超連23賣", -2.0)]
    out = collapse(rows, DAYS)
    assert len(out) == 1
    assert out[0].score == pytest.approx(0.5)      # (3.0 + −2.0) ÷ 2：配息那件事只取 3.0
    assert out[0].pred == "flat" and out[0].merged == 3 and out[0].news_id == 1


def test_collapse_prefers_direct_over_sector_and_splits_by_base_day():
    rows = [row(1, "甲公司營收創新高", 2.0), row(2, "記憶體報價下跌", -3.0, typ="sector"),
            row(3, "甲公司法說會前夕", 2.0, time="08:00"),          # 開盤前：基準日是 11/09
            row(4, "乙公司接獲大單", 2.0, code="2222")]
    out = {(r.code, r.time): r for r in collapse(rows, DAYS)}
    assert len(out) == 3
    assert out[("1111", "20:00")].score == 2.0 and out[("1111", "20:00")].merged == 2
    assert out[("1111", "08:00")].merged == 1 and out[("2222", "20:00")].merged == 1


def test_validate_dedupe_reduces_rows(tmp_path):
    c = db.connect(":memory:")
    c.executemany("INSERT INTO trading_days (date) VALUES (?)", [(d,) for d in DAYS])
    c.executemany("INSERT INTO prices (date, code, close) VALUES (?, ?, ?)",
                  [(d, "TAIEX", 100.0) for d in DAYS] + [(d, "1111", p) for d, p in zip(DAYS, (100, 100, 105))])
    rows = [row(i, f"甲公司董事會決議每股配息9元創新高{'！' * i}", 2.0) for i in range(1, 6)]
    plain = validate(c, horizon=1, rows=[Row(**vars(r)) for r in rows])
    one = validate(c, horizon=1, rows=[Row(**vars(r)) for r in rows], dedupe=True)
    assert plain["summary"]["n"] == 5 and one["summary"]["n"] == 1
    assert one["summary"]["hit_rate"] == 1.0 and one["rows"][0]["merged"] == 5
    assert one["params"]["dedupe"] is True and plain["params"]["dedupe"] is False
