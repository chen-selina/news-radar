"""標籤與指標的唯一來源：五個手工案例（漲、跌、持平、缺資料、中性預測）在三個模組得到同樣的分數。"""
import pytest

from radar import db, metrics, scoring, targets, train, validate
from radar.train import Sample
from radar.validate import Row

DAYS = ["2026-09-01", "2026-09-02"]
# 代號 → (第二天收盤, 預測)；第一天都是 100，大盤持平
CASES = {
    "1111": (105.0, "up"),     # 實際漲、猜漲 → 命中
    "2222": (95.0, "up"),      # 實際跌、猜漲 → 沒命中
    "3333": (100.5, "down"),   # 實際持平（+0.5%）、猜跌 → 沒命中，但留在命中率的分母
    "4444": (None, "up"),      # 缺第二天的價格 → 不評分，原因 nodata
    "5555": (103.0, "flat"),   # 實際漲、中性預測 → 不發訊號，不進命中率，但進三分類
}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.executemany("INSERT INTO trading_days (date) VALUES (?)", [(d,) for d in DAYS])
    px = [(d, "TAIEX", 100.0) for d in DAYS]
    for code, (p1, _) in CASES.items():
        px.append((DAYS[0], code, 100.0))
        if p1 is not None:
            px.append((DAYS[1], code, p1))
    c.executemany("INSERT INTO prices (date, code, close) VALUES (?, ?, ?)", px)
    c.commit()
    return c


def test_direction_and_spec():
    assert [targets.direction(x, 0.01) for x in (0.02, -0.02, 0.01, -0.01, 0.0, None)] == \
        ["up", "down", "flat", "flat", "flat", None]
    assert targets.observed_by("2026-09-02", "2026-09-02") and not targets.observed_by("2026-09-03", "2026-09-02")
    assert not targets.observed_by(None, "2026-09-02")
    with pytest.raises(ValueError):
        targets.LabelSpec(horizon=0)
    with pytest.raises(ValueError):
        targets.LabelSpec(benchmark="sector")
    assert targets.LabelSpec(3, "raw", 0.02).version.endswith("h3|raw|t0.02")


def test_report_on_the_five_cases():
    preds = [p for _, p in CASES.values()]
    acts = ["up", "down", "flat", None, "up"]
    m = metrics.report(preds, acts, [None, None, None, "nodata", None])
    assert (m["n_total"], m["n_labeled"], m["n_signal"]) == (5, 4, 3)
    assert m["coverage"] == 0.75 and m["excluded"] == {"nodata": 1}
    assert m["hit_rate"] == pytest.approx(1 / 3) and m["hits"] == 1
    assert m["accuracy"] == 0.25                                       # 三分類：4 筆只對 1 筆
    assert m["confusion"]["up"] == {"down": 0, "flat": 1, "up": 1}
    assert m["confusion"]["flat"]["down"] == 1 and m["confusion"]["down"]["up"] == 1
    # recall：up 1/2、down 0、flat 0 → balanced 1/6
    assert m["balanced_accuracy"] == pytest.approx(1 / 6)
    assert m["actual_share"] == {"down": 0.25, "flat": 0.25, "up": 0.5}
    b = m["baselines"]
    assert b["majority"]["class"] == "up" and b["majority"]["accuracy"] == 0.5
    assert b["always_up"]["balanced_accuracy"] == pytest.approx(1 / 3)
    assert b["always_up"]["hit_rate_on_signals"] == pytest.approx(1 / 3)     # 同一批 3 筆有訊號的：漲、跌、持平
    assert b["always_down"]["hit_rate_on_signals"] == pytest.approx(1 / 3)


def test_report_rejects_bad_input():
    with pytest.raises(ValueError):
        metrics.report(["up"], ["up", "down"])
    with pytest.raises(ValueError):
        metrics.report(["buy"], ["up"])
    empty = metrics.report([], [])
    assert empty["hit_rate"] is None and empty["coverage"] is None and empty["baselines"]["majority"]["class"] is None


def test_validate_scoring_and_train_agree_on_the_five_cases(conn):
    codes = list(CASES)
    preds = [CASES[c][1] for c in codes]

    rows = [Row(i, DAYS[0], "10:00", "", "", "", None, c, c, "direct", None, 1.0, p)
            for i, (c, p) in enumerate(zip(codes, preds))]
    v = validate.validate(conn, horizon=1, rows=rows)

    samples = [Sample(c, DAYS[0], {}) for c in codes]
    train.label(conn, samples, horizon=1)
    sc = scoring.evaluate(samples, preds, 1)

    m_train = train._evaluate([p for p, s in zip(preds, samples) if s.act is not None],
                              [s for s in samples if s.act is not None])

    assert [s.act for s in samples] == ["up", "down", "flat", None, "up"]
    assert [s.status for s in samples] == ["done", "done", "done", "nodata", "done"]
    # 同一批樣本：命中率、分母、同樣本基準完全一樣
    assert v["summary"]["hit_rate"] == sc["hit_rate"] == m_train["hit_rate"] == pytest.approx(1 / 3)
    assert v["summary"]["n"] == sc["n"] == m_train["n_signal"] == 3
    assert v["metrics"] == sc["metrics"]
    for k in ("n_labeled", "n_signal", "coverage", "accuracy", "balanced_accuracy", "macro_f1", "confusion", "baselines"):
        assert m_train[k] == v["metrics"][k], k
    assert v["metrics"]["excluded"] == {"nodata": 1}
    assert sc["all_up"] == v["metrics"]["baselines"]["always_up"]["hit_rate_on_signals"]
    assert v["params"]["target_version"].startswith(targets.TARGET_VERSION)
