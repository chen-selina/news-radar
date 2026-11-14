import math

from radar.significance import daily_spreads, newey_west


def test_plain_t_test_matches_hand_calculation():
    r = newey_west([1.0, 2.0, 3.0, 4.0, 5.0])
    assert r["mean"] == 3.0
    assert math.isclose(r["se"], math.sqrt(2.5 / 5))          # 樣本標準差 / √n
    assert math.isclose(r["t"], 3.0 / math.sqrt(0.5))
    assert 0 < r["p"] < 0.05


def test_positive_autocorrelation_widens_standard_error():
    xs = [1, 1, 1, 1, -1, -1, -1, -1, 1, 1, 1, 1, -1, -1, -1, -1, 1.5]
    assert newey_west(xs, lags=3)["se"] > newey_west(xs)["se"]


def test_too_few_days_gives_no_test():
    assert newey_west([0.1, 0.2])["t"] is None


def test_daily_spreads_need_both_sides_and_ignore_unfinished():
    row = lambda d, pred, ex, status="done": {"base_date": d, "pred": pred, "ex": ex, "status": status}
    rows = [row("2026-01-02", "up", 0.03), row("2026-01-02", "up", 0.01), row("2026-01-02", "down", -0.01),
            row("2026-01-03", "up", 0.05),                       # 這天沒有利空，不算
            row("2026-01-04", "up", 0.02), row("2026-01-04", "down", 0.04), row("2026-01-04", "flat", 0.5),
            row("2026-01-05", "up", None, "wait"), row("2026-01-05", "down", 0.0)]
    sp = daily_spreads(rows)
    assert list(sp) == ["2026-01-02", "2026-01-04"]
    assert math.isclose(sp["2026-01-02"], 0.03) and math.isclose(sp["2026-01-04"], -0.02)
