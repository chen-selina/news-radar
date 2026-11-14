import math

from radar.scoring import evaluate, pos_neg, predict
from radar.train import Sample


def S(day, code, feats, ex=None):
    return Sample(code, day, feats, ex=ex)


def test_pos_neg_follows_sign_after_negation():
    assert pos_neg(S("d", "1", {"成長": 2.0, "衰退": -1.0, "獲利": -0.5})) == (2.0, 1.5)


def test_zero_rule_and_tone_ignore_length():
    ss = [S("d", "1", {"a": 9.0, "b": -1.0}), S("d", "2", {"a": 1.0, "b": -3.0}), S("d", "3", {"a": 1.0, "b": -1.0})]
    assert predict(ss, "加總", "zero") == ["up", "down", "flat"]
    assert predict(ss, "情緒指標", "zero") == ["up", "down", "flat"]


def test_median_rule_splits_each_day_and_needs_enough_stocks():
    day1 = [S("d1", str(i), {"a": float(i)}) for i in range(1, 6)]         # 全部正面，分數 1～5
    day2 = [S("d2", "1", {"a": 1.0}), S("d2", "2", {"a": 2.0})]             # 只有兩檔，不比
    assert predict(day1 + day2, "加總", "median") == ["down", "down", "flat", "up", "up", "flat", "flat"]


def test_negative_only_ranks_by_negative_words():
    ss = [S("d", "1", {"a": 5.0}), S("d", "2", {"a": 5.0, "b": -1.0}), S("d", "3", {"b": -2.0}), S("d", "4", {"b": -3.0})]
    assert predict(ss, "只算負面詞", "median") == ["up", "up", "down", "down"]


def test_evaluate_counts_hits_baselines_and_daily_spread():
    ss = [S("d1", "1", {}, 0.03), S("d1", "2", {}, -0.02), S("d2", "1", {}, 0.005), S("d2", "2", {}, -0.03),
          S("d3", "1", {}, None), S("d3", "2", {}, 0.02)]
    r = evaluate(ss, ["up", "down", "up", "down", "up", "flat"], 1)
    assert (r["n"], r["up_n"], r["down_n"]) == (4, 2, 2)
    assert r["hit_rate"] == 0.75 and r["all_down"] == 0.5 and r["all_up"] == 0.25      # d2 的 +0.5% 算持平
    assert r["test"]["n"] == 2 and math.isclose(r["test"]["mean"], (0.05 + 0.035) / 2)
