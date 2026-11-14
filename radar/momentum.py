"""動能對照：扣掉股價本身的動能之後，新聞情緒還有沒有額外的預測力？

radar/novelty.py 發現預測力主要來自「標題在描述已發生漲跌」的行情報導，也就是字典可能只是間接讀到了
股價動能（今天大漲的個股之後繼續強）。這裡直接檢查：

一、策略對照（同一批樣本）
  - 情緒：字典分數（現行的加總和 0 比；以及情緒指標和當天中位數比）。
  - 動能：完全不看新聞，只看基準日當天個股扣除大盤後的漲跌（和 0 比；和當天中位數比）。
  情緒如果贏不過動能，新聞文字就沒有帶來額外的資訊。

二、逐日橫斷面迴歸（Fama & MacBeth, 1973）
  每個交易日做一次迴歸：之後 N 日的超額報酬 ＝ a ＋ b1×情緒 ＋ b2×當日報酬 ＋ b3×前 5 日報酬，
  再把每天的係數平均，用 Newey & West (1987) 標準誤檢定是否不等於 0。
  解釋變數每天各自標準化（減平均、除以標準差），係數的意思是「高一個標準差，之後報酬多幾 %」。
  重點看 b1：控制了動能之後 b1 還顯著，才能說情緒有額外的預測力。
  控制近期報酬的作法同 Tetlock, Saar-Tsechansky & Macskassy (2008)。

樣本是「一檔個股、一個基準交易日」（有新聞直接提及、論文字典找得到情緒詞的）。
這個指令只讀資料庫，不會寫入任何東西。需要 numpy（pip install -r requirements-ml.txt）。

執行：python -m radar.momentum
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from dataclasses import dataclass

from . import db
from .analysis import lexicon, remi
from .scoring import MIN_STOCKS, SCORES, evaluate, pos_neg, predict
from .significance import _stars, newey_west
from .train import Sample, build_samples, label
from .validate import HORIZONS, TAIEX, _prices

MIN_CROSS = 10      # 當天不到這麼多筆樣本就不做迴歸
PAST = 5            # 「前幾日報酬」看幾個交易日


@dataclass
class Past:
    r0: float | None    # 基準日當天扣除大盤後的報酬
    r5: float | None    # 基準日之前 PAST 個交易日扣除大盤後的報酬（不含基準日當天）


def past_returns(conn: sqlite3.Connection, samples: list[Sample]) -> list[Past]:
    days = [r[0] for r in conn.execute("SELECT date FROM trading_days ORDER BY date")]
    idx = {d: i for i, d in enumerate(days)}
    px = _prices(conn, {s.code for s in samples})
    market = px.get(TAIEX, {})

    def excess(code: str, i0: int, i1: int) -> float | None:
        if i0 < 0:
            return None
        ser = px.get(code, {})
        a, b, m0, m1 = ser.get(days[i0]), ser.get(days[i1]), market.get(days[i0]), market.get(days[i1])
        return (b / a - 1) - (m1 / m0 - 1) if a and b and m0 and m1 else None

    out = []
    for s in samples:
        i = idx.get(s.day)
        out.append(Past(None, None) if i is None else Past(excess(s.code, i - 1, i), excess(s.code, i - 1 - PAST, i - 1)))
    return out


def momentum_preds(samples: list[Sample], past: list[Past], rule: str) -> list[str]:
    xs = [p.r0 for p in past]
    if rule == "zero":
        return ["flat" if x is None else "up" if x > 0 else "down" if x < 0 else "flat" for x in xs]
    by_day: dict[str, list[float]] = {}
    for s, x in zip(samples, xs):
        if x is not None:
            by_day.setdefault(s.day, []).append(x)
    mid = {d: statistics.median(v) for d, v in by_day.items() if len(v) >= MIN_STOCKS}
    return ["flat" if x is None or s.day not in mid else "up" if x > mid[s.day] else "down" if x < mid[s.day] else "flat"
            for s, x in zip(samples, xs)]


def fama_macbeth(rows: list[tuple[str, float, list[float]]], lags: int = 0) -> list[dict] | None:
    """rows: [(交易日, 被解釋變數, [解釋變數…])]。每天做一次迴歸（解釋變數當天標準化），回傳每個解釋變數的
    平均係數與檢定（Newey–West）。某個解釋變數當天沒有變化的日子不算。"""
    import numpy as np
    by_day: dict[str, list[tuple[float, list[float]]]] = {}
    for d, y, xs in rows:
        by_day.setdefault(d, []).append((y, xs))
    coefs: list[list[float]] = []
    for d in sorted(by_day):
        obs = by_day[d]
        if len(obs) < MIN_CROSS:
            continue
        y = np.array([o[0] for o in obs])
        x = np.array([o[1] for o in obs], dtype=float)
        sd = x.std(axis=0)
        if (sd == 0).any():
            continue
        x = (x - x.mean(axis=0)) / sd
        beta = np.linalg.lstsq(np.column_stack([np.ones(len(y)), x]), y, rcond=None)[0]
        coefs.append([float(b) for b in beta[1:]])
    if not coefs:
        return None
    return [newey_west([c[j] for c in coefs], lags) for j in range(len(coefs[0]))]


SENTIMENTS = ("情緒指標", "加總")
MODELS = (("只放情緒", ("s",)), ("只放當日報酬", ("r0",)), ("情緒＋當日報酬", ("s", "r0")),
          ("情緒＋當日報酬＋前 5 日報酬", ("s", "r0", "r5")))
NAMES = {"s": "情緒", "r0": "當日報酬", "r5": "前 5 日報酬"}


def run(conn: sqlite3.Connection, base: lexicon.Lexicon, data: str = "backfill") -> dict:
    samples = build_samples(conn, base, data, lex=remi.as_lexicon(base))
    past = past_returns(conn, samples)
    strategies = (("情緒：加總，和 0 比（現行）", predict(samples, "加總", "zero")),
                  ("動能：當日漲跌，和 0 比", momentum_preds(samples, past, "zero")),
                  ("情緒：情緒指標，和當天比", predict(samples, "情緒指標", "median")),
                  ("動能：當日漲跌，和當天比", momentum_preds(samples, past, "median")))
    out = {"samples": len(samples), "strategies": [], "regressions": []}
    for h in HORIZONS:
        for s in samples:
            s.ex = s.y = None
        label(conn, samples, h)
        for name, preds in strategies:
            # 兩邊用同一批樣本：要有當日報酬才算
            keep = [p if q.r0 is not None else "flat" for p, q in zip(preds, past)]
            out["strategies"].append({"strategy": name, "horizon": h, **evaluate(samples, keep, h)})
        for sent in SENTIMENTS:
            for model, cols in MODELS:
                if sent != SENTIMENTS[0] and "s" not in cols:
                    continue
                rows = []
                for s, q in zip(samples, past):
                    v = {"s": SCORES[sent](*pos_neg(s)), "r0": q.r0, "r5": q.r5}
                    if s.ex is not None and all(v[c] is not None for c in cols):
                        rows.append((s.day, s.ex, [v[c] for c in cols]))
                res = fama_macbeth(rows, h - 1)
                out["regressions"].append({"sentiment": sent, "model": model, "horizon": h, "n": len(rows),
                                           "coefs": dict(zip(cols, res)) if res else {}})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="扣掉股價動能之後，新聞情緒還有沒有預測力")
    ap.add_argument("--live", action="store_true", help="用網站收錄的新聞（預設用回補的歷史新聞）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    res = run(conn, lexicon.load_file(lexicon.HANDMADE_PATH), "live" if args.live else "backfill")
    pc = lambda x, f="{:.0f}%": "—" if x is None else f.format(x * 100)
    g = lambda x, f="{:.2f}": "—" if x is None else f.format(x)
    print(f"樣本 {res['samples']} 個（個股, 交易日）；論文字典、只看直接提及、扣除大盤；* p<0.10　** p<0.05　*** p<0.01")
    print("\n一、策略對照（同一批樣本）")
    for h in HORIZONS:
        print(f"第 {h} 日")
        for r in (x for x in res["strategies"] if x["horizon"] == h):
            t = r["test"]
            print(f"  {r['strategy']:<16}　利多 {r['up_n']:>4}／利空 {r['down_n']:>4}　命中率 {pc(r['hit_rate']):>4}"
                  f"（全猜跌 {pc(r['all_down'])}）　每日報酬差 {pc(t['mean'], '{:+.2f}%')}（{t['n']} 天，p＝{g(t['p'], '{:.3f}')}）{_stars(t['p'])}")
    print("\n二、逐日橫斷面迴歸：係數＝解釋變數高一個標準差，之後的超額報酬多幾 %（括號內是 p 值）")
    for sent in SENTIMENTS:
        print(f"【情緒用「{sent}」】")
        for h in HORIZONS:
            print(f"第 {h} 日")
            for r in (x for x in res["regressions"] if x["sentiment"] == sent and x["horizon"] == h):
                parts = [f"{NAMES[c]} {pc(t['mean'], '{:+.2f}%')}（{g(t['p'], '{:.3f}')}）{_stars(t['p'])}" for c, t in r["coefs"].items()]
                days = next(iter(r["coefs"].values()))["n"] if r["coefs"] else 0
                print(f"  {r['model']:<14}　{'　'.join(parts)}　〔{r['n']} 筆、{days} 天〕")
    return 0


if __name__ == "__main__":
    sys.exit(main())
