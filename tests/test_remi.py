"""論文字典（台灣財經情緒字典）：讀檔、長詞優先計數、Tone 指標、轉成引擎詞典、三版本比較。"""
import pytest

from radar import compare, db
from radar.analysis import remi
from radar.analysis.lexicon import Lexicon
from radar.validate import Row, validate

SMALL = remi.RemiDict({"成長": 1, "未影響": 1, "影響": -1, "不確定性升高": -1, "升高": 1, "虧損": -1, "吃緊": -1},
                      {"成長": 100})


def test_real_dictionary_loads():
    d = remi.load()
    assert len(d.polarity) > 7000
    assert d.polarity["獲利"] == 1 and d.polarity["虧損"] == -1
    assert d.polarity["不確定性升高"] == -1          # 否定詞組也收進來了
    assert d.counts["獲利"] > 1000
    assert not {"壓倒性", "扳倒", "獨霸"} & set(d.polarity)  # 原始檔正負面都列的詞不收
    assert all(w and w == w.strip() for w in d.polarity)


def test_longest_term_wins_across_polarities():
    # 「未影響」是正面，不能再把裡面的「影響」算成負面；「不確定性升高」同理
    assert remi.count("疫情未影響營運", SMALL) == (1, 0)
    assert remi.count("不確定性升高", SMALL) == (0, 1)
    assert [m[2] for m in remi.matches("營收成長但虧損，成長", SMALL)] == ["成長", "虧損", "成長"]


def test_count_counts_every_occurrence():
    assert remi.count("成長、成長、虧損", SMALL) == (2, 1)
    assert remi.count("今天天氣晴", SMALL) == (0, 0)
    assert remi.count("", SMALL) == (0, 0)


def test_tone_follows_paper_formula():
    # 三句：正面、負面、正面；第四句正負各一不計 → (2 − 1) ÷ (2 + 1)
    t = remi.tone("營收成長。出現虧損。持續成長。成長但虧損。", SMALL)
    assert (t["pos_sentences"], t["neg_sentences"]) == (2, 1)
    assert t["tone"] == pytest.approx(1 / 3)
    assert (t["pos_words"], t["neg_words"]) == (3, 2)
    assert remi.tone("沒有情緒詞。", SMALL)["tone"] == 0.0


def test_as_lexicon_is_unit_weight_and_keeps_sectors():
    base = Lexicon({"漲停": 2.0, "吃緊": 1.0, "砍單": -2.0, "成長": 0.0}, {"記憶體": "記憶體"})
    plain = remi.as_lexicon(base, d=SMALL)
    assert plain.sentiment["吃緊"] == -1 and "漲停" not in plain.sentiment and plain.sentiment["成長"] == 1
    assert plain.sectors == {"記憶體": "記憶體"}
    ext = remi.as_lexicon(base, extend=True, d=SMALL)
    assert ext.sentiment["漲停"] == 1 and ext.sentiment["砍單"] == -1   # 只取方向，不帶強度
    assert ext.sentiment["吃緊"] == 1                                    # 方向不同時以個股詞典為準
    assert "成長" not in ext.sentiment                                   # 個股詞典設為 0 的詞視為停用
    assert ext.sentiment["虧損"] == -1


# ---------- 三版本比較 ----------

DAYS = ["2026-10-20", "2026-10-21", "2026-10-22"]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.executemany("INSERT INTO trading_days (date) VALUES (?)", [(d,) for d in DAYS])
    px = [(d, "TAIEX", 100.0) for d in DAYS]
    px += [(d, "1111", p) for d, p in zip(DAYS, (100, 105, 110))] + [(d, "2222", p) for d, p in zip(DAYS, (100, 95, 90))]
    c.executemany("INSERT INTO prices (date, code, close) VALUES (?, ?, ?)", px)
    c.executemany("INSERT INTO stocks (code, name, market, updated_at) VALUES (?, ?, '上市', 'now')",
                  [("1111", "甲乙科技"), ("2222", "丙丁電子")])
    c.executemany("INSERT INTO news (url, source, title, body, published_at, fetched_at) VALUES (?, 'udn', ?, ?, ?, 'now')",
                  [("https://a.com/1", "甲乙科技漲停", "甲乙科技今天漲停。", "2026-10-20T15:00:00+08:00"),
                   ("https://a.com/2", "丙丁電子出現虧損", "丙丁電子上季虧損。", "2026-10-20T15:00:00+08:00")])
    c.commit()
    return c


def test_validate_accepts_rows_without_touching_db(conn):
    rows = [Row(1, "2026-10-20", "15:00", "t", "u", "udn", None, "1111", "甲乙科技", "direct", None, 2.0, "up")]
    res = validate(conn, horizon=1, rows=rows)
    assert res["summary"]["n"] == 1 and res["summary"]["hit_rate"] == 1.0
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0


def test_compare_runs_three_variants_and_writes_nothing(conn, monkeypatch):
    monkeypatch.setattr(remi, "load", lambda path=None: SMALL)
    base = Lexicon({"漲停": 2.0, "虧損": -1.5}, {})
    before = conn.total_changes
    res = compare.compare(conn, base)
    assert conn.total_changes == before
    assert {r["variant"] for r in res} == set(compare.VARIANTS)
    one = {r["variant"]: r for r in res if r["horizon"] == 1}
    assert one["手工詞典"]["hit_rate"] == 1.0 and one["手工詞典"]["n"] == 2
    # 論文字典沒有「漲停」：甲乙科技變中性，只剩丙丁電子一筆；加上個股擴充後補回來
    assert one["論文字典"]["n"] == 1 and one["論文字典"]["neutral"] == 1
    assert one["論文＋個股擴充"]["n"] == 2 and one["論文＋個股擴充"]["hit_rate"] == 1.0
    assert one["手工詞典"]["spread"] == pytest.approx(0.10)
