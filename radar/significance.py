"""顯著性檢定：字典判斷利多的個股，之後的表現真的比判斷利空的好嗎？還是只是運氣？

作法（財務實證常用的「逐日橫斷面」作法，Fama & MacBeth, 1973）：
1. 每一個交易日各算一次「多空報酬差」＝當天判斷利多的個股之後的平均報酬 − 當天判斷利空的個股之後的平均報酬。
   同一天的個股會一起漲跌，所以一天只算一個觀察值，不把每一筆預測都當成獨立樣本。
2. 檢定這些每日報酬差的平均是不是大於 0（t 檢定）。
3. 觀察期間超過 1 天時，相鄰兩天的報酬期間互相重疊，標準誤用 Newey & West (1987) 修正，
   落後期數＝觀察天數 − 1。

另外檢定「論文字典的每日報酬差 − 手工詞典的每日報酬差」是不是大於 0（同一天配對）。

這個指令只讀資料庫，不會寫入任何東西。

執行：python -m radar.significance --backfill
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys

from . import db
from .analysis import lexicon
from .compare import VARIANTS, lexicons, predict_rows
from .validate import HORIZONS, Row, validate


def newey_west(xs: list[float], lags: int = 0) -> dict:
    """平均、Newey–West 標準誤、t 值、雙尾 p 值。lags＝0 就是一般的 t 檢定。"""
    n = len(xs)
    if n < 3:
        return {"n": n, "mean": sum(xs) / n if n else None, "se": None, "t": None, "p": None}
    mean = sum(xs) / n
    e = [x - mean for x in xs]
    var = sum(v * v for v in e) / n
    for k in range(1, min(lags, n - 1) + 1):
        cov = sum(e[i] * e[i - k] for i in range(k, n)) / n
        var += 2 * (1 - k / (lags + 1)) * cov
    se = math.sqrt(max(var, 0.0) / (n - 1))
    if se == 0:
        return {"n": n, "mean": mean, "se": 0.0, "t": None, "p": None}
    t = mean / se
    return {"n": n, "mean": mean, "se": se, "t": t, "p": _p_two_sided(t, n - 1)}


def _p_two_sided(t: float, df: int) -> float:
    try:
        from scipy import stats
        return float(2 * stats.t.sf(abs(t), df))
    except ImportError:                      # 沒有 scipy 就用常態分配近似（天數多時差別很小）
        return math.erfc(abs(t) / math.sqrt(2))


def daily_spreads(rows: list[dict]) -> dict[str, float]:
    """每個基準日一個數字：當天利多個股的平均報酬 − 利空個股的平均報酬。當天缺其中一邊就不算。"""
    by_day: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        if r["status"] == "done" and r["pred"] in ("up", "down"):
            by_day.setdefault(r["base_date"], {"up": [], "down": []})[r["pred"]].append(r["ex"])
    return {d: sum(g["up"]) / len(g["up"]) - sum(g["down"]) / len(g["down"])
            for d, g in sorted(by_day.items()) if g["up"] and g["down"]}


def test(conn: sqlite3.Connection, base: lexicon.Lexicon, benchmark: str = "excess", scope: str = "direct",
         data: str = "live") -> dict:
    """回傳 {"variants": [每個版本×觀察期間的檢定], "paired": [論文字典 − 手工詞典 的配對檢定]}。"""
    spreads: dict[tuple[str, int], dict[str, float]] = {}
    out = {"variants": [], "paired": []}
    for name, lex in lexicons(base).items():
        rows = predict_rows(conn, lex, data)
        for h in HORIZONS:
            res = validate(conn, h, benchmark, 0.01, scope, rows=[Row(**vars(r)) for r in rows], dedupe=True)
            sp = spreads[name, h] = daily_spreads(res["rows"])
            out["variants"].append({"variant": name, "horizon": h, **newey_west(list(sp.values()), h - 1),
                                    "positive_days": sum(v > 0 for v in sp.values())})
    for other in VARIANTS[1:]:
        for h in HORIZONS:
            a, b = spreads[other, h], spreads[VARIANTS[0], h]
            diff = [a[d] - b[d] for d in sorted(set(a) & set(b))]
            out["paired"].append({"variant": other, "horizon": h, **newey_west(diff, h - 1)})
    return out


def _stars(p: float | None) -> str:
    return "" if p is None else "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.10 else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="多空報酬差的顯著性檢定")
    ap.add_argument("--all", action="store_true", help="連產業連動的個股一起算（預設只看直接提及）")
    ap.add_argument("--raw", action="store_true", help="用個股漲跌（預設扣除大盤）")
    ap.add_argument("--backfill", action="store_true", help="改用回補的歷史新聞")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    res = test(conn, lexicon.load_file(lexicon.HANDMADE_PATH), "raw" if args.raw else "excess", "all" if args.all else "direct",
               "backfill" if args.backfill else "live")
    f = lambda x, s="{:+.2f}%": "—" if x is None else s.format(x * 100)
    g = lambda x, s="{:.2f}": "—" if x is None else s.format(x)
    print("每個交易日算一次多空報酬差，再檢定平均是否不等於 0（Newey–West 標準誤，落後期數＝觀察天數−1）")
    print("* p<0.10　** p<0.05　*** p<0.01")
    for name in VARIANTS:
        print(f"\n{name}")
        for r in (x for x in res["variants"] if x["variant"] == name):
            print(f"  第 {r['horizon']:>2} 日　{r['n']} 個交易日，其中 {r['positive_days']} 天報酬差為正　"
                  f"平均 {f(r['mean'])}　標準誤 {f(r['se'], '{:.2f}%')}　t＝{g(r['t'])}　p＝{g(r['p'], '{:.3f}')} {_stars(r['p'])}")
    print(f"\n和{VARIANTS[0]}相比（同一天的報酬差相減）")
    for r in res["paired"]:
        print(f"  {r['variant']}　第 {r['horizon']:>2} 日　{r['n']} 天　平均多 {f(r['mean'])}　"
              f"t＝{g(r['t'])}　p＝{g(r['p'], '{:.3f}')} {_stars(r['p'])}")
    if max((r["n"] for r in res["variants"]), default=0) < 40:
        print("\n注意：交易日不到 40 天，檢定力很低，沒有顯著不代表沒有效果。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
