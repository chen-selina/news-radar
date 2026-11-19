"""新聞品質抽查：自動欄位、規則分類、分層抽樣、填好標註後的計分。"""
import csv

import pytest

from radar import backfill, db, quality
from radar.analysis import remi

DAYS = ["2026-09-01", "2026-09-02", "2026-09-03"]


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(remi, "load", lambda path=None: remi.RemiDict({"成長": 1, "虧損": -1}, {}))
    c = db.connect(":memory:")
    c.executemany("INSERT INTO trading_days (date) VALUES (?)", [(d,) for d in DAYS])
    c.executemany("INSERT INTO stocks (code, name, market, market_cap, updated_at) VALUES (?, ?, '上市', ?, 'now')",
                  [("1111", "甲乙科技", 9e12), ("2222", "丙丁電子", 1e9)])
    c.commit()
    return c


def add(conn, n, title, body, when, code="1111"):
    backfill.save_links(conn, code, when[:10], [{"date": "2026-09-01 00:00:00", "title": title, "source": "測試日報",
                                                  "link": f"https://a.example.com/{n}"}])
    conn.execute("UPDATE backfill_news SET status = 'ok', title = ?, list_title = ?, body = ?, published_at = ? WHERE url = ?",
                 (title, title, body, when, f"https://a.example.com/{n}"))
    conn.commit()


def test_bucket_rules():
    tr = set(DAYS)
    assert quality._bucket("2026-09-01", "08:59", tr) == "盤前"
    assert quality._bucket("2026-09-01", "13:30", tr) == "盤中"
    assert quality._bucket("2026-09-01", "13:31", tr) == "盤後"
    assert quality._bucket("2026-09-05", "10:00", tr) == "休市日"
    assert quality._bucket("2026-09-01", "", tr) == "沒有時間" and quality._bucket("", "", tr) == "沒有時間"


def test_collect_fills_automatic_fields_and_rule_types(conn):
    add(conn, 1, "甲乙科技董事會決議配息創新高營收成長", "甲乙科技營收成長。", "2026-09-01T08:00:00+08:00")
    add(conn, 2, "甲乙科技董事會決議配息創新高營收成長！", "甲乙科技營收成長。", "2026-09-01T09:30:00+08:00")
    add(conn, 3, "甲乙科技股價收盤大漲", "甲乙科技今天大漲。", "2026-09-02T14:00:00+08:00")
    add(conn, 4, "央行理監事會議", "今天天氣晴。", "2026-09-02T10:00:00+08:00", code="2222")
    items = {it["id"]: it for it in quality.collect(conn, include_live=False)}
    a, b, c, d = (items[i] for i in sorted(items))
    assert a["auto_type"] == "?" and a["first_in_event"] and a["event_size"] == 2
    assert b["auto_type"] == "6" and not b["first_in_event"]                 # 同事件的第二篇
    assert c["auto_type"] == "5" and c["recap"] and c["bucket"] == "盤後"     # 行情報導
    assert d["auto_type"] == "7" and d["direct"] == [] and d["tag_found"] is False   # FinMind 標了 2222，引擎沒找到
    assert a["tag_found"] is True and a["size"] == "前 50 大" and a["bucket"] == "盤前"
    assert a["ts_source"] == "published_at" and a["delay_h"] is None             # 回補的抓取延遲不算


def test_report_rows_cover_each_dimension(conn):
    add(conn, 1, "甲乙科技營收成長", "甲乙科技營收成長。", "2026-09-01T08:00:00+08:00")
    add(conn, 2, "甲乙科技股價大漲", "甲乙科技大漲。", "2026-09-02T14:00:00+08:00")
    rows = quality.report_rows(quality.collect(conn, include_live=False), [("ok", None, 2), ("skip", "不是新聞", 3)])
    allrow = next(r for r in rows if r["維度"] == "全部")
    assert allrow["則數"] == 2 and allrow["行情報導（規則）"] == 0.5 and allrow["有 published_at"] == 1.0
    assert {r["維度"] for r in rows} >= {"資料來源", "媒體", "發布時段", "規模", "月份", "規則分類", "回補抓取狀態"}


def fake_items(n_big=200, n_small=3):
    out = [{"data": "回補", "id": i, "bucket": "盤後", "recap": False, "size": "其他"} for i in range(n_big)]
    out += [{"data": "回補", "id": 1000 + i, "bucket": "盤前", "recap": True, "size": "前 50 大"} for i in range(n_small)]
    out += [{"data": "網站", "id": 2000, "bucket": "盤中", "recap": False, "size": "其他"}]
    return out


def test_stratified_sample_reaches_small_strata_and_is_reproducible():
    items = fake_items()
    s1, s2 = quality.stratified_sample(items, 20), quality.stratified_sample(items, 20)
    assert [x["id"] for x in s1] == [x["id"] for x in s2]                    # 固定種子
    assert len(s1) == 20
    by = {}
    for x in s1:
        by.setdefault(x["stratum"], []).append(x)
    assert len(by) == 3 and len(by["回補｜盤前｜行情｜前 50 大"]) >= 2 and len(by["網站｜盤中｜非行情｜其他"]) == 1
    # 權重推回全體：每層 weight × 抽樣數 ＝ 層的總數
    assert sum(x["weight"] for x in s1) == pytest.approx(len(items))


def test_score_rows_precision_recall_and_weights():
    def r(rule, human, comp="Y", new="N", w=1.0, src="回補"):
        return {"規則分類": rule, "人工_新聞類型": human, "人工_公司對應": comp, "人工_漏掉的公司": "", "人工_時間可信": "Y",
                "人工_含新資訊": new, "weight": w, "資料": src}
    rows = [r("5", "5"), r("5", "1", new="Y"), r("?", "5"), r("6", "6", comp="部分"), r("?", "1", new="Y", w=3.0),
            {**r("?", ""), "人工_新聞類型": ""}]                                       # 沒填的不算
    res = {x["指標"]: x for x in quality.score_rows(rows)}
    assert res["已標註則數"]["值"] == 5
    assert res["規則「行情報導」precision"]["值"] == 0.5 and res["規則「行情報導」precision"]["分母"] == 2
    assert res["規則「行情報導」recall"]["值"] == 0.5
    assert res["規則「重複」precision"]["值"] == 1.0
    assert res["公司對應完全正確"]["值"] == 0.8 and res["公司對應至少部分正確"]["值"] == 1.0
    assert res["含新資訊"]["值"] == 0.4 and res["含新資訊"]["加權值（推回全體）"] == pytest.approx(4 / 7, abs=1e-4)
    assert res["含新資訊（類型 1 公司公告／財報／營收／法說）"]["值"] == 1.0


def test_sample_command_does_not_overwrite_annotations(tmp_path, monkeypatch, conn):
    monkeypatch.setattr(quality, "OUT_DIR", tmp_path)
    monkeypatch.setattr(quality.db, "connect", lambda path=None: conn)
    add(conn, 1, "甲乙科技營收成長", "甲乙科技營收成長。", "2026-09-01T08:00:00+08:00")
    assert quality.main(["sample", "--n", "5"]) == 0
    with (tmp_path / "audit_sample.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1 and rows[0]["人工_新聞類型"] == "" and rows[0]["規則分類"] == "?"
    assert quality.main(["sample", "--n", "5"]) == 1                                  # 已經有檔案就不覆蓋
    rows[0]["人工_新聞類型"] = "1"
    rows[0]["人工_含新資訊"] = "Y"
    with (tmp_path / "audit_sample.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert quality.main(["score"]) == 0
    assert (tmp_path / "audit_results.csv").exists()


def test_import_ai_fills_only_ai_columns_and_validates():
    rows = [{"編號": "1", "人工_新聞類型": "", "AI_新聞類型": ""}, {"編號": "2", "人工_新聞類型": "5", "AI_新聞類型": ""}]
    n = quality.import_ai(rows, [["1", "4", "Y", "", "N", "法人看好"], ["2", "5", "部分", "2308", "N", "行情"]], "test")
    assert n == 2 and rows[0]["AI_新聞類型"] == "4" and rows[1]["AI_漏掉的公司"] == "2308"
    assert rows[1]["人工_新聞類型"] == "5" and rows[0]["人工_新聞類型"] == ""          # 人工欄位沒有動
    assert rows[0]["AI_版本"] == "test"
    with pytest.raises(ValueError, match="編號 9"):
        quality.import_ai(rows, [["9", "4", "Y", "", "N", ""]], "test")
    with pytest.raises(ValueError, match="不合規定"):
        quality.import_ai(rows, [["1", "8", "Y", "", "N", ""]], "test")


def test_score_reports_ai_agreement_against_humans():
    def r(ai, human, ai_new="N", h_new="N"):
        return {"規則分類": "?", "人工_新聞類型": human, "人工_公司對應": "Y", "人工_漏掉的公司": "", "人工_時間可信": "Y",
                "人工_含新資訊": h_new, "weight": 1, "資料": "回補", "AI_新聞類型": ai, "AI_公司對應": "Y",
                "AI_含新資訊": ai_new}
    res = {x["指標"]: x for x in quality.score_rows([r("5", "5"), r("4", "1", "N", "Y"), r("6", "6"), r("1", "1", "Y", "Y")])}
    assert res["AI 新聞類型＝人工"]["值"] == 0.75 and res["人工改了 AI 建議的類型"]["值"] == 0.25
    assert res["AI 含新資訊＝人工"]["值"] == 0.75 and res["AI 公司對應＝人工"]["值"] == 1.0


def test_ai_import_command_keeps_extra_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "OUT_DIR", tmp_path)
    cols = ["編號", "標題", "我的欄位", "人工_新聞類型"]
    with (tmp_path / "audit_sample.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerow({"編號": "1", "標題": "甲", "我的欄位": "保留", "人工_新聞類型": "2"})
    (tmp_path / "ai.tsv").write_text("1\t2\tY\t\tY\t產品\n", encoding="utf-8")
    assert quality.main(["ai-import", "--labels", str(tmp_path / "ai.tsv"), "--ai-version", "v"]) == 0
    with (tmp_path / "audit_sample.csv").open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert reader.fieldnames == ["編號", "標題", "我的欄位", *quality.AI, "人工_新聞類型"]
    assert rows[0]["我的欄位"] == "保留" and rows[0]["人工_新聞類型"] == "2" and rows[0]["AI_新聞類型"] == "2"
