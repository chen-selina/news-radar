"""計分方式比較：同一部字典（論文字典）、同一批新聞，只換「怎麼把情緒詞變成利多／利空」，看哪一種比較準。

現行作法是把提到這檔個股的句子裡的情緒詞加總（正面 +1、負面 −1），大於 0 就是利多。
財經新聞的正面用語本來就多，結果判斷利多的次數是利空的三到四倍。這裡比較幾種有文獻依據的修法：

計分
- 加總：正面詞數 − 負面詞數（現行作法）。
- 情緒指標：（正面 − 負面）÷（正面 ＋ 負面），介於 −1～+1，不受文章長短影響。
  這是黃裕烈（2025）式 (1) 的形式；論文以「句」為單位，這裡以提到這檔個股的句子裡的「詞」為單位。
- 只算負面詞：負面詞越多分數越低，不看正面詞。Tetlock (2007)、Tetlock, Saar-Tsechansky & Macskassy (2008)、
  Loughran & McDonald (2011) 都發現負面詞比正面詞有資訊。

判斷
- 和 0 比：分數大於 0 利多、小於 0 利空（現行作法）。
- 和當天比：分數高於當天所有個股的中位數才算利多，低於才算利空（橫斷面比較，利多利空大約各半）。

一檔個股、一個基準交易日算一筆（同事件的多篇報導不重複計算），只看直接提及。
顯著性檢定與 python -m radar.significance 相同（逐日多空報酬差、Newey–West 標準誤）。
這個指令只讀資料庫，不會寫入任何東西，也不會改變正式預測。

執行：python -m radar.scoring            （用回補的歷史新聞）
      python -m radar.scoring --live     （用網站收錄的新聞）
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys

from . import db
from .analysis import lexicon, remi
from .significance import _stars, newey_west
from .train import Sample, build_samples, label
from .validate import HORIZONS

MIN_STOCKS = 4      # 當天不到這麼多檔個股有新聞，就不做「和當天比」


def pos_neg(s: Sample) -> tuple[float, float]:
    """（正面詞數, 負面詞數）；否定反轉後的方向。"""
    return sum(v for v in s.features.values() if v > 0), -sum(v for v in s.features.values() if v < 0)


SCORES = {
    "加總": lambda p, n: p - n,
    "情緒指標": lambda p, n: (p - n) / (p + n) if p + n else 0.0,
    "只算負面詞": lambda p, n: -n,
}
# (名稱, 計分, 判斷)
METHODS = (("加總，和 0 比（現行）", "加總", "zero"), ("情緒指標，和 0 比", "情緒指標", "zero"),
           ("加總，和當天比", "加總", "median"), ("情緒指標，和當天比", "情緒指標", "median"),
           ("只算負面詞，和當天比", "只算負面詞", "median"))


def predict(samples: list[Sample], score: str, rule: str) -> list[str]:
    """每筆樣本的判斷：up／down／flat。"""
    xs = [SCORES[score](*pos_neg(s)) for s in samples]
    if rule == "zero":
        return ["up" if x > 0 else "down" if x < 0 else "flat" for x in xs]
    by_day: dict[str, list[float]] = {}
    for s, x in zip(samples, xs):
        by_day.setdefault(s.day, []).append(x)
    mid = {d: statistics.median(v) for d, v in by_day.items() if len(v) >= MIN_STOCKS}
    return ["flat" if s.day not in mid else "up" if x > mid[s.day] else "down" if x < mid[s.day] else "flat"
            for s, x in zip(samples, xs)]


def evaluate(samples: list[Sample], preds: list[str], horizon: int, threshold: float = 0.01) -> dict:
    """命中率、同一批樣本「全部猜跌／全部猜漲」的命中率、多空報酬差與檢定。samples 要先用 label() 填好報酬。"""
    done = [(s, p) for s, p in zip(samples, preds) if p != "flat" and s.ex is not None]
    act = lambda s: "up" if s.ex > threshold else "down" if s.ex < -threshold else "flat"
    ups, downs = [s.ex for s, p in done if p == "up"], [s.ex for s, p in done if p == "down"]
    by_day: dict[str, dict[str, list[float]]] = {}
    for s, p in done:
        by_day.setdefault(s.day, {"up": [], "down": []})[p].append(s.ex)
    daily = [sum(g["up"]) / len(g["up"]) - sum(g["down"]) / len(g["down"])
             for _, g in sorted(by_day.items()) if g["up"] and g["down"]]
    n = len(done)
    share = lambda a: sum(act(s) == a for s, _ in done) / n if n else None
    avg = lambda v: sum(v) / len(v) if v else None
    return {"n": n, "up_n": len(ups), "down_n": len(downs),
            "hit_rate": sum(p == act(s) for s, p in done) / n if n else None,
            "all_down": share("down"), "all_up": share("up"),
            "up_avg": avg(ups), "down_avg": avg(downs), "test": newey_west(daily, horizon - 1)}


def run(conn: sqlite3.Connection, base: lexicon.Lexicon, data: str = "backfill", benchmark: str = "excess") -> list[dict]:
    samples = build_samples(conn, base, data, lex=remi.as_lexicon(base))
    out = []
    for h in HORIZONS:
        for s in samples:
            s.ex = s.y = None
        label(conn, samples, h, benchmark)
        for name, score, rule in METHODS:
            out.append({"method": name, "horizon": h, **evaluate(samples, predict(samples, score, rule), h)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="比較幾種把情緒詞變成利多／利空的計分方式")
    ap.add_argument("--live", action="store_true", help="用網站收錄的新聞（預設用回補的歷史新聞）")
    ap.add_argument("--raw", action="store_true", help="用個股漲跌（預設扣除大盤）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    res = run(conn, lexicon.load_file(lexicon.HANDMADE_PATH), "live" if args.live else "backfill",
              "raw" if args.raw else "excess")
    pc = lambda x, f="{:.0f}%": "—" if x is None else f.format(x * 100)
    g = lambda x, f="{:.2f}": "—" if x is None else f.format(x)
    print("論文字典、只看直接提及、一檔個股一天一筆、持平門檻 ±1%")
    print("報酬差：每個交易日算一次（利多個股平均 − 利空個股平均）再平均；* p<0.10　** p<0.05　*** p<0.01")
    for h in HORIZONS:
        print(f"\n第 {h} 日")
        for r in (x for x in res if x["horizon"] == h):
            t = r["test"]
            print(f"  {r['method']:<14}　利多 {r['up_n']:>4}／利空 {r['down_n']:>4}　命中率 {pc(r['hit_rate']):>4}"
                  f"（全猜跌 {pc(r['all_down'])}、全猜漲 {pc(r['all_up'])}）　利多後 {pc(r['up_avg'], '{:+.2f}%')}"
                  f"　利空後 {pc(r['down_avg'], '{:+.2f}%')}　每日報酬差 {pc(t['mean'], '{:+.2f}%')}"
                  f"（{t['n']} 天，t＝{g(t['t'])}，p＝{g(t['p'], '{:.3f}')}）{_stars(t['p'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
