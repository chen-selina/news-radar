"""字典調整：以論文字典為基礎，用歷史新聞之後的股價反應估計每個詞的權重。

依據：Jegadeesh & Wu (2013)〈Word power: A new approach for content analysis〉——以既有字典的詞為準，
把文件之後的報酬對各個詞的出現次數做迴歸，迴歸係數就是詞的權重。Loughran & McDonald (2011) 說明了
通用字典需要依領域調整的理由。

做法
- 樣本：一檔個股、一個基準交易日（同 radar/train.py）。特徵是論文字典每個詞在提到這檔個股的句子裡的
  出現次數，帶方向（正面詞為正、負面詞為負，否定句反轉）。所以「權重 1」就是論文字典原本的算法。
- 目標：之後第 N 個交易日扣除大盤的報酬（訓練時把最極端的 1% 壓到邊界，避免少數暴漲暴跌主導結果）。
- 依時間切分：前面的交易日訓練，最後 35% 測試。訓練期最後 N 天的樣本不用（它們的報酬期間會跨進測試期）。
- 只估計訓練期出現夠多次的詞，其餘的詞維持權重 1。

三種調整（由保守到積極）
- 只刪詞：估出來方向和論文字典相反的詞停用（權重 0），其餘維持 1。
- 調強度、不改方向：限制權重不能是負的（論文說正面就維持正面），只估計強弱。
- 完全由資料決定：方向和強度都由資料估計。
後兩種估完會把權重整體縮放，讓常見詞的平均權重是 1，才能和沒估計的詞放在一起用。

評估（只看測試期）
- 多空報酬差：分數大於 0 算利多、小於 0 算利空，逐日計算後檢定（同 radar/significance.py）。
- 和原本的字典（每個詞 ±1）配對比較：同一天的報酬差相減後檢定。
- 逐日迴歸：分數標準化後的係數；再加入當日報酬，看控制股價動能後是否還顯著（同 radar/momentum.py）。

結果存在 data/tuned-h{N}.json，不會改動 config/lexicon.json，也不會改變正式預測。
需要 numpy、scikit-learn（pip install -r requirements-ml.txt）。

執行：python -m radar.tune
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from . import config, db
from .analysis import lexicon, remi
from .momentum import fama_macbeth, past_returns
from .scoring import evaluate
from .significance import _stars, newey_west
from .train import Sample, build_samples, label
from .validate import HORIZONS

METHODS = ("原本（每個詞 ±1）", "只刪詞", "調強度、不改方向", "完全由資料決定")
ALPHAS = (10.0, 100.0, 1000.0, 10000.0)     # 脊迴歸的懲罰強度候選值（越大越保守，權重越接近彼此）


def split(samples: list[Sample], horizon: int, test_share: float = 0.35) -> tuple[list[Sample], list[Sample]]:
    """依時間切成訓練與測試；訓練期最後 horizon 個交易日不用。"""
    data = [s for s in samples if s.ex is not None]
    days = sorted({s.day for s in data})
    cut_i = len(days) - max(1, round(len(days) * test_share))
    cut, last_train = days[cut_i], days[max(0, cut_i - horizon - 1)]
    return [s for s in data if s.day <= last_train and s.day < cut], [s for s in data if s.day >= cut]


def _matrix(samples: list[Sample], terms: list[str]):
    import numpy as np
    col = {t: j for j, t in enumerate(terms)}
    x = np.zeros((len(samples), len(terms)), dtype=np.float32)
    for i, s in enumerate(samples):
        for t, v in s.features.items():
            j = col.get(t)
            if j is not None:
                x[i, j] = v
    return x


def fit_weights(train: list[Sample], min_count: int = 30) -> dict:
    """回傳 {"terms": 估計的詞, "counts": 出現筆數, "alpha": {...}, "weights": {調整方式: {詞: 權重}}}。
    沒列在 weights 裡的詞權重是 1。"""
    import numpy as np
    from sklearn.linear_model import Ridge

    counts: dict[str, int] = {}
    for s in train:
        for t, v in s.features.items():
            if v:
                counts[t] = counts.get(t, 0) + 1
    terms = sorted(t for t, n in counts.items() if n >= min_count)
    if not terms:
        raise ValueError(f"沒有任何詞在訓練期出現 {min_count} 筆以上")
    x = _matrix(train, terms)
    y = np.array([s.ex for s in train])
    lo, hi = np.quantile(y, [0.01, 0.99])
    y = np.clip(y, lo, hi)
    day = np.array([s.day for s in train])
    days = sorted(set(day))

    def choose(positive: bool) -> float:
        """只在訓練期裡往前滾動驗證 3 次，挑分數和之後報酬最相關的懲罰強度。"""
        if len(days) < 12:
            return ALPHAS[-2]
        starts = [days[int(len(days) * f)] for f in (0.5, 0.67, 0.84)]
        ends = starts[1:] + ["9999"]
        best, best_r = ALPHAS[-1], -9.0
        for a in ALPHAS:
            rs = []
            for st, en in zip(starts, ends):
                tr, va = day < st, (day >= st) & (day < en)
                if va.sum() < 50:
                    continue
                pred = Ridge(alpha=a, positive=positive).fit(x[tr], y[tr]).predict(x[va])
                if pred.std() > 0:
                    rs.append(float(np.corrcoef(pred, y[va])[0, 1]))
            r = float(np.mean(rs)) if rs else -9.0
            if r > best_r:
                best, best_r = a, r
        return best

    def rescale(w):
        """讓常見詞的平均權重（依出現筆數加權）是 1。"""
        n = np.array([counts[t] for t in terms], dtype=float)
        scale = float((np.abs(w) * n).sum() / n.sum())
        return w / scale if scale > 0 else np.ones_like(w)

    a_free, a_pos = choose(False), choose(True)
    free = Ridge(alpha=a_free).fit(x, y).coef_.astype(float)
    pos = Ridge(alpha=a_pos, positive=True).fit(x, y).coef_.astype(float)
    as_dict = lambda w: {t: float(v) for t, v in zip(terms, w)}
    return {"terms": terms, "counts": {t: counts[t] for t in terms}, "alpha": {"free": a_free, "positive": a_pos},
            "weights": {"原本（每個詞 ±1）": {},
                        "只刪詞": {t: 0.0 for t, v in zip(terms, free) if v < 0},
                        "調強度、不改方向": as_dict(rescale(pos)),
                        "完全由資料決定": as_dict(rescale(free))}}


def score(s: Sample, weights: dict[str, float]) -> float:
    return sum(v * weights.get(t, 1.0) for t, v in s.features.items())


def _daily(samples: list[Sample], xs: list[float]) -> dict[str, float]:
    by_day: dict[str, dict[str, list[float]]] = {}
    for s, x in zip(samples, xs):
        if x != 0 and s.ex is not None:
            by_day.setdefault(s.day, {"up": [], "down": []})["up" if x > 0 else "down"].append(s.ex)
    return {d: sum(g["up"]) / len(g["up"]) - sum(g["down"]) / len(g["down"])
            for d, g in by_day.items() if g["up"] and g["down"]}


def assess(test: list[Sample], r0: list[float | None], fitted: dict, horizon: int) -> list[dict]:
    out = []
    base = _daily(test, [score(s, {}) for s in test])
    for name in METHODS:
        w = fitted["weights"][name]
        xs = [score(s, w) for s in test]
        ev = evaluate(test, ["up" if x > 0 else "down" if x < 0 else "flat" for x in xs], horizon)
        mine = _daily(test, xs)
        paired = newey_west([mine[d] - base[d] for d in sorted(set(mine) & set(base))], horizon - 1)
        alone = fama_macbeth([(s.day, s.ex, [x]) for s, x in zip(test, xs)], horizon - 1)
        ctrl = fama_macbeth([(s.day, s.ex, [x, r]) for s, x, r in zip(test, xs, r0) if r is not None], horizon - 1)
        out.append({"method": name, "horizon": horizon, **ev, "paired": paired,
                    "coef": alone[0] if alone else None, "coef_ctrl": ctrl[0] if ctrl else None,
                    "coef_r0": ctrl[1] if ctrl else None})
    return out


def run(conn: sqlite3.Connection, base: lexicon.Lexicon, data: str = "backfill", min_count: int = 30,
        test_share: float = 0.35) -> dict:
    samples = build_samples(conn, base, data, lex=remi.as_lexicon(base))
    past = {id(s): p.r0 for s, p in zip(samples, past_returns(conn, samples))}
    out = {"samples": len(samples), "horizons": {}}
    for h in HORIZONS:
        for s in samples:
            s.ex = s.y = None
        label(conn, samples, h)
        train, test = split(samples, h, test_share)
        fitted = fit_weights(train, min_count)
        out["horizons"][h] = {"n_train": len(train), "n_test": len(test),
                              "train_days": (train[0].day, train[-1].day), "test_days": (test[0].day, test[-1].day),
                              "fitted": fitted, "rows": assess(test, [past[id(s)] for s in test], fitted, h)}
    return out


def save(res: dict) -> list[str]:
    paths = []
    for h, r in res["horizons"].items():
        f = r["fitted"]
        p = config.DATA_DIR / f"tuned-h{h}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"term": t, "samples": f["counts"][t],
                 "prune": f["weights"]["只刪詞"].get(t, 1.0),
                 "positive": round(f["weights"]["調強度、不改方向"][t], 4),
                 "free": round(f["weights"]["完全由資料決定"][t], 4)} for t in f["terms"]]
        p.write_text(json.dumps({
            "_說明": "python -m radar.tune 的結果：以論文字典為基礎估計的詞權重（1＝論文字典原本的算法，0＝停用，"
                     "負數＝方向和論文字典相反）。沒列出的詞權重是 1。只是估計結果，沒有套用到 config/lexicon.json。",
            "created_at": config.now_iso(), "horizon": h, "train_days": r["train_days"], "test_days": r["test_days"],
            "n_train": r["n_train"], "n_test": r["n_test"], "ridge_alpha": f["alpha"],
            "weights": sorted(rows, key=lambda x: -x["free"])}, ensure_ascii=False, indent=1), encoding="utf-8")
        paths.append(str(p))
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="以論文字典為基礎，用歷史新聞估計每個詞的權重")
    ap.add_argument("--min-count", type=int, default=30, help="詞至少要出現在幾筆訓練樣本裡才估計（預設 30）")
    ap.add_argument("--test-share", type=float, default=0.35, help="最後多少比例的交易日留作測試（預設 0.35）")
    ap.add_argument("--live", action="store_true", help="用網站收錄的新聞（預設用回補的歷史新聞）")
    ap.add_argument("--top", type=int, default=12, help="列出權重最大、最小的各幾個詞（預設 12）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    try:
        res = run(conn, lexicon.load_file(lexicon.HANDMADE_PATH), "live" if args.live else "backfill",
                  args.min_count, args.test_share)
    except ImportError:
        print("需要先安裝機器學習套件：pip install -r requirements-ml.txt")
        return 1
    pc = lambda x, f="{:+.2f}%": "—" if x is None else f.format(x * 100)
    g = lambda x, f="{:.3f}": "—" if x is None else f.format(x)
    cell = lambda t, k="mean": "—" if not t else f"{pc(t[k])}（{g(t['p'])}）{_stars(t['p'])}"
    print(f"樣本 {res['samples']} 個（個股, 交易日）；以下都是測試期的結果；括號內是 p 值；* p<0.10　** p<0.05　*** p<0.01")
    for h, r in res["horizons"].items():
        f = r["fitted"]
        print(f"\n第 {h} 日　訓練 {r['train_days'][0]}～{r['train_days'][1]}（{r['n_train']} 筆）　"
              f"測試 {r['test_days'][0]}～{r['test_days'][1]}（{r['n_test']} 筆）　估計 {len(f['terms'])} 個詞，"
              f"其中刪掉 {len(f['weights']['只刪詞'])} 個")
        for x in r["rows"]:
            print(f"  {x['method']:<10}　利多 {x['up_n']:>5}／利空 {x['down_n']:>5}　命中率 {x['hit_rate'] * 100:.0f}%"
                  f"（全猜跌 {x['all_down'] * 100:.0f}%）　每日報酬差 {cell(x['test'])}　比原本多 {cell(x['paired'])}"
                  f"　迴歸係數 {cell(x['coef'])}　控制當日報酬後 {cell(x['coef_ctrl'])}")
    h = 5 if 5 in res["horizons"] else next(iter(res["horizons"]))
    f = res["horizons"][h]["fitted"]
    for name in ("調強度、不改方向", "完全由資料決定"):
        w = f["weights"][name]
        order = sorted(w, key=lambda t: -w[t])
        show = lambda ts: "　".join(f"{t} {w[t]:+.1f}（{f['counts'][t]}）" for t in ts)
        print(f"\n第 {h} 日「{name}」權重最大的 {args.top} 個詞（括號是訓練期出現筆數；論文字典原本都是 1）：\n  {show(order[:args.top])}")
        print(f"權重最小的 {args.top} 個詞：\n  {show(order[-args.top:])}")
    print("\n完整結果存在 " + "、".join(save(res)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
