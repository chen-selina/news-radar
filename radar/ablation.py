"""消融實驗（P1-1）：不看新聞的市場資料模型，和加入新聞後比，有沒有變好？

同一批樣本、同一個進場時點、同一個測試期，只換特徵組合：

- Model 0：基準。全猜漲／全猜跌／全猜多數（三分類）；預測報酬一律相同（IC 無意義）。
- Model A：只用市場資料，完全不看新聞。
- Model N：只用新聞（現行字典的正負詞數、加總分數、報導量），給參考。
- Model B：A ＋ N。B 要在同一批測試樣本上贏過 A，才能說新聞有「額外」的預測資訊。

樣本：一檔個股、一個進場交易日（radar/train.py 的 build_samples，進場規則見 radar/targets.py）。
只用有新聞的樣本，A 和 B 才能在完全相同的列上比較。

市場特徵（全部截至進場日「前一個」交易日收盤，進場日 13:00 下決定時一定已經知道）：
- r1、r5、r20：前 1／5／20 個交易日扣除大盤後的報酬
- vol20：前 20 個交易日個股日報酬的標準差
- mkt5：前 5 個交易日加權指數報酬
- size：log(流通股數 × 前一日收盤)。注意股數是 stocks 資料表現在的值，不是當時的，輕微偷看。
- 產業：stocks.industry（現在的分類），只用訓練期出現過的產業做 one-hot。
沒有成交量、沒有當時的市值，所以規格書裡的「成交量異常」做不到。

新聞特徵：正面詞數、負面詞數、加總分數（正 − 負）、log(1 ＋ 報導則數)。

兩種預測目標（radar/targets.py）：
1. 連續：第 N 日超額報酬 → Ridge 迴歸。看逐日橫斷面 Spearman IC（預測與實際報酬的等級相關），
   以及每天「預測前一半 − 後一半」的報酬差；B 與 A 的差用逐日配對、Newey–West 檢定。
2. 三分類：down／flat／up → 邏輯迴歸（class_weight＝balanced），指標由 radar/metrics.py 計算。

切分：依時間，最後三成交易日測試；訓練樣本的標籤結束日必須不晚於測試開始日（purging）。
--walk-forward（P1-4）：逐月往前滾動，每個月當一次測試期、只用之前的資料訓練（expanding window，同樣 purging），
所有測試月合起來算總指標，幾乎整年都變成樣本外，檢定力比一次切分高。每一折印出日期範圍、筆數、標籤最晚結束日。
報酬差另外扣交易成本（COSTS：多空來回 0／0.3%／0.585%）看還剩多少。
標準化、補值、產業類別與正則化強度都只用訓練期決定（正則化在訓練期內再往前滾動 3 折挑，一樣剔除重疊標籤）。

執行：python -m radar.ablation                 （第 1、3、5、10 日）
      python -m radar.ablation --horizon 5
需要 requirements-ml.txt。這個指令只讀資料庫。
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from dataclasses import dataclass, field

from . import db, metrics, targets
from .analysis import lexicon
from .scoring import pos_neg
from .significance import _stars, newey_west
from .train import Sample, build_samples, label
from .validate import HORIZONS, TAIEX, _prices

MARKET = ("r1", "r5", "r20", "vol20", "mkt5", "size")
NEWS = ("pos", "neg", "score", "log_news")
MODELS = {"A：只用市場": (MARKET, True), "N：只用新聞": (NEWS, False), "B：市場＋新聞": (MARKET + NEWS, True)}
ALPHAS = (0.1, 1.0, 10.0, 100.0, 1000.0)
C_GRID = (0.003, 0.01, 0.03, 0.1, 0.3, 1.0)
MIN_CROSS = 5       # 當天測試樣本少於這個數就不算 IC


@dataclass
class Row:
    sample: Sample
    x: dict[str, float | None]
    industry: str = ""


def market_features(conn: sqlite3.Connection, samples: list[Sample]) -> list[tuple[dict[str, float | None], str]]:
    """每筆樣本的市場特徵與產業；只用進場日前一個交易日（含）以前的收盤價。"""
    days = [r[0] for r in conn.execute("SELECT date FROM trading_days ORDER BY date")]
    idx = {d: i for i, d in enumerate(days)}
    px = _prices(conn, {s.code for s in samples})
    mkt = px.get(TAIEX, {})
    info = {r["code"]: (r["industry"] or "", r["shares"]) for r in conn.execute("SELECT code, industry, shares FROM stocks")}

    def close(code, i):
        return px.get(code, {}).get(days[i]) if 0 <= i < len(days) else None

    def ret(code, i0, i1):
        a, b = close(code, i0), close(code, i1)
        return b / a - 1 if a and b else None

    def excess(code, i0, i1):
        r, m = ret(code, i0, i1), ret(TAIEX, i0, i1) if mkt else None
        return r - m if r is not None and m is not None else None

    out = []
    for s in samples:
        i = idx.get(s.day)
        ind, shares = info.get(s.code, ("", None))
        if i is None:
            out.append(({k: None for k in MARKET}, ind))
            continue
        last = i - 1          # 進場日前一個交易日：下決定的時候一定已經收盤
        daily = [ret(s.code, j - 1, j) for j in range(last - 19, last + 1)]
        daily = [d for d in daily if d is not None]
        vol = math.sqrt(sum((d - sum(daily) / len(daily)) ** 2 for d in daily) / (len(daily) - 1)) if len(daily) >= 10 else None
        c = close(s.code, last)
        out.append(({"r1": excess(s.code, last - 1, last), "r5": excess(s.code, last - 5, last),
                     "r20": excess(s.code, last - 20, last), "vol20": vol, "mkt5": ret(TAIEX, last - 5, last),
                     "size": math.log(shares * c) if shares and c else None}, ind))
    return out


def news_features(s: Sample) -> dict[str, float]:
    p, n = pos_neg(s)
    return {"pos": p, "neg": n, "score": p - n, "log_news": math.log1p(s.news)}


def build_rows(conn: sqlite3.Connection, samples: list[Sample]) -> list[Row]:
    return [Row(s, {**m, **news_features(s)}, ind) for s, (m, ind) in zip(samples, market_features(conn, samples))]


def split(rows: list[Row], test_share: float = 0.3) -> tuple[list[Row], list[Row], int]:
    """依時間切分＋purging。回傳（訓練, 測試, 被剔除的訓練筆數）。"""
    dates = sorted({r.sample.day for r in rows})
    if len(dates) < 10:
        raise ValueError(f"有結果的交易日只有 {len(dates)} 天，太少")
    cut = dates[-max(1, round(len(dates) * test_share))]
    before = [r for r in rows if r.sample.day < cut]
    tr = [r for r in before if targets.observed_by(r.sample.end_day, cut)]
    return tr, [r for r in rows if r.sample.day >= cut], len(before) - len(tr)


class Design:
    """把特徵列變成矩陣：產業類別、補值（中位數）、標準化都只用 fit() 看過的訓練資料決定。"""

    def __init__(self, cols: tuple[str, ...], industry: bool):
        self.cols, self.industry = cols, industry

    def fit(self, rows: list[Row]) -> "Design":
        import numpy as np
        self.inds = sorted({r.industry for r in rows if r.industry}) if self.industry else []
        raw = self._raw(rows)
        med = np.nanmedian(raw, axis=0)
        self.med = np.where(np.isnan(med), 0.0, med)
        filled = np.where(np.isnan(raw), self.med, raw)
        self.mu, sd = filled.mean(axis=0), filled.std(axis=0)
        self.sd = np.where(sd > 0, sd, 1.0)
        return self

    def _raw(self, rows: list[Row]):
        import numpy as np
        num = [[(math.nan if r.x[c] is None else r.x[c]) for c in self.cols] for r in rows]
        one = [[1.0 if r.industry == k else 0.0 for k in self.inds] for r in rows]
        return np.array([a + b for a, b in zip(num, one)], dtype=float).reshape(len(rows), len(self.cols) + len(self.inds))

    def transform(self, rows: list[Row]):
        import numpy as np
        raw = self._raw(rows)
        return (np.where(np.isnan(raw), self.med, raw) - self.mu) / self.sd


def daily_ic(rows: list[Row], pred) -> dict[str, float]:
    """逐日 Spearman 等級相關（預測 vs 實際超額報酬）。"""
    from scipy.stats import spearmanr
    by: dict[str, list[tuple[float, float]]] = {}
    for r, p in zip(rows, pred):
        by.setdefault(r.sample.day, []).append((float(p), r.sample.ex))
    out = {}
    for d, xs in sorted(by.items()):
        if len(xs) < MIN_CROSS or len({x for x, _ in xs}) < 2 or len({y for _, y in xs}) < 2:
            continue
        out[d] = float(spearmanr([x for x, _ in xs], [y for _, y in xs])[0])
    return out


def daily_spread(rows: list[Row], pred) -> dict[str, float]:
    """每天：預測前一半的平均報酬 − 後一半。"""
    by: dict[str, list[tuple[float, float]]] = {}
    for r, p in zip(rows, pred):
        by.setdefault(r.sample.day, []).append((float(p), r.sample.ex))
    out = {}
    for d, xs in sorted(by.items()):
        if len(xs) < MIN_CROSS or len({x for x, _ in xs}) < 2:   # 預測全部一樣就分不出前後
            continue
        xs.sort(key=lambda t: t[0])
        h = len(xs) // 2
        out[d] = sum(y for _, y in xs[-h:]) / h - sum(y for _, y in xs[:h]) / h
    return out


def _inner_folds(rows: list[Row]):
    """訓練期裡往前滾動 3 折（每折一樣剔除重疊標籤）：回傳 [(訓練列, 驗證列)]。"""
    dates = sorted({r.sample.day for r in rows})
    if len(dates) < 6:
        return []
    starts = [dates[int(len(dates) * f)] for f in (0.5, 0.67, 0.84)]
    ends = starts[1:] + ["9999"]
    return [([r for r in rows if r.sample.day < a and targets.observed_by(r.sample.end_day, a)],
             [r for r in rows if a <= r.sample.day < b]) for a, b in zip(starts, ends)]


def fit_regression(tr: list[Row], te: list[Row], cols, industry):
    import numpy as np
    from sklearn.linear_model import Ridge

    def fit_pred(a, b, alpha):
        d = Design(cols, industry).fit(a)
        m = Ridge(alpha=alpha).fit(d.transform(a), np.array([r.sample.ex for r in a]))
        return m.predict(d.transform(b))

    best, best_s = ALPHAS[-1], -math.inf
    for alpha in ALPHAS:
        ss = [v for a, b in _inner_folds(tr) if a and b
              for v in [_mean(list(daily_ic(b, fit_pred(a, b, alpha)).values()))] if v is not None]
        s = sum(ss) / len(ss) if ss else -math.inf
        if s > best_s:
            best, best_s = alpha, s
    return fit_pred(tr, te, best), best


def fit_classifier(tr: list[Row], te: list[Row], cols, industry):
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    def fit_pred(a, b, c):
        d = Design(cols, industry).fit(a)
        m = LogisticRegression(C=c, max_iter=2000, class_weight="balanced")
        return m.fit(d.transform(a), np.array([r.sample.act for r in a])).predict(d.transform(b))

    best, best_s = C_GRID[0], -math.inf
    for c in C_GRID:
        ss = []
        for a, b in _inner_folds(tr):
            if b and len({r.sample.act for r in a}) >= 2:
                ss.append(metrics.report(list(fit_pred(a, b, c)), [r.sample.act for r in b])["balanced_accuracy"] or 0.0)
        s = sum(ss) / len(ss) if ss else -math.inf
        if s > best_s:
            best, best_s = c, s
    return list(fit_pred(tr, te, best)), best


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


# 成本敏感度：每一筆「多一檔、空一檔」的來回成本（多、空各一次進出）。0.585%＝手續費 0.1425% × 2 ＋ 證交稅 0.3%。
COSTS = (0.0, 0.003, 0.00585)


@dataclass
class Result:
    horizon: int
    n_train: int
    n_test: int
    n_purged: int
    test_days: tuple[str, str]
    models: dict[str, dict] = field(default_factory=dict)
    b_vs_a: dict = field(default_factory=dict)
    folds: list[dict] = field(default_factory=list)    # walk-forward 時每一折的資訊


def fit_fold(tr: list[Row], te: list[Row]) -> dict[str, dict]:
    """在一組訓練／測試列上擬合每個模型，回傳 {模型: {reg: 預測報酬, cls: 預測方向, alpha, C}}。"""
    out = {}
    for name, (cols, industry) in MODELS.items():
        reg, alpha = fit_regression(tr, te, cols, industry)
        cls, c = fit_classifier(tr, te, cols, industry)
        out[name] = {"reg": [float(x) for x in reg], "cls": list(cls), "alpha": alpha, "C": c}
    return out


def score(te: list[Row], preds: dict[str, dict], horizon: int) -> tuple[dict[str, dict], dict]:
    """同一批測試列上每個模型的指標，以及 B − A 的逐日 IC 差檢定。"""
    acts = [r.sample.act for r in te]
    base = metrics.report(["flat"] * len(te), acts)["baselines"]
    models: dict[str, dict] = {"0：基準": {"cls": {k: base[k] for k in ("always_up", "always_down", "majority")}}}
    ics = {}
    for name, p in preds.items():
        ics[name] = daily_ic(te, p["reg"])
        spread = list(daily_spread(te, p["reg"]).values())
        models[name] = {"ic": newey_west(list(ics[name].values()), horizon - 1),
                        "spread": newey_west(spread, horizon - 1),
                        "net": {c: newey_west([x - 2 * c for x in spread], horizon - 1) for c in COSTS},
                        "cls": metrics.report(p["cls"], acts)}
    a, b = ics["A：只用市場"], ics["B：市場＋新聞"]
    return models, newey_west([b[d] - a[d] for d in sorted(set(a) & set(b))], horizon - 1)


def _labeled(rows: list[Row]) -> list[Row]:
    return [r for r in rows if r.sample.act is not None and r.sample.ex is not None]


def evaluate(rows: list[Row], horizon: int, test_share: float = 0.3) -> Result:
    """一次時間切分。rows 要先有標籤（label()）；沒有標籤的列不用。所有模型用同一批訓練／測試列。"""
    tr, te, purged = split(_labeled(rows), test_share)
    res = Result(horizon, len(tr), len(te), purged, (te[0].sample.day, te[-1].sample.day))
    preds = fit_fold(tr, te)
    res.models, res.b_vs_a = score(te, preds, horizon)
    for name, p in preds.items():
        res.models[name].update(alpha=p["alpha"], C=p["C"])
    return res


def monthly_folds(rows: list[Row], min_train_days: int = 60) -> list[tuple[str, list[Row], list[Row], int]]:
    """逐月往前滾動（expanding window）：每個月當一次測試期，訓練用那個月以前、標籤在月初收盤前已觀測完的列。

    回傳 [(月份, 訓練列, 測試列, 被剔除的訓練筆數)]。訓練期少於 min_train_days 個交易日的月份不測。
    特徵只用過去的價格，所以不需要 embargo；標籤重疊由 purging 處理。
    """
    dates = sorted({r.sample.day for r in rows})
    out = []
    for m in sorted({d[:7] for d in dates}):
        start = next(d for d in dates if d[:7] == m)
        if sum(d < start for d in dates) < min_train_days:
            continue
        before = [r for r in rows if r.sample.day < start]
        tr = [r for r in before if targets.observed_by(r.sample.end_day, start)]
        out.append((m, tr, [r for r in rows if r.sample.day[:7] == m], len(before) - len(tr)))
    return out


def walk_forward(rows: list[Row], horizon: int, min_train_days: int = 60) -> Result:
    """逐月 walk-forward：每折重新挑正則化、重新補值與標準化；所有測試月合起來算總指標。"""
    rows = sorted(_labeled(rows), key=lambda r: r.sample.day)
    folds = monthly_folds(rows, min_train_days)
    if not folds:
        raise ValueError(f"資料不到 {min_train_days} 個交易日，無法做 walk-forward")
    all_te: list[Row] = []
    pooled: dict[str, dict] = {name: {"reg": [], "cls": []} for name in MODELS}
    info = []
    for m, tr, te, purged in folds:
        if len({r.sample.act for r in tr}) < 2:
            continue
        preds = fit_fold(tr, te)
        models, bva = score(te, preds, horizon)
        train_end = max(r.sample.end_day for r in tr)
        info.append({"month": m, "train": (tr[0].sample.day, tr[-1].sample.day), "n_train": len(tr),
                     "n_purged": purged, "train_label_end": train_end,
                     "test": (te[0].sample.day, te[-1].sample.day), "n_test": len(te),
                     "no_overlap": train_end <= te[0].sample.day,
                     "ic": {n: models[n]["ic"]["mean"] for n in MODELS},
                     "balanced": {n: models[n]["cls"]["balanced_accuracy"] for n in MODELS},
                     "b_minus_a_ic": bva["mean"]})
        all_te += te
        for name, p in preds.items():
            pooled[name]["reg"] += p["reg"]
            pooled[name]["cls"] += p["cls"]
    res = Result(horizon, sum(f["n_train"] for f in info), len(all_te), sum(f["n_purged"] for f in info),
                 (all_te[0].sample.day, all_te[-1].sample.day), folds=info)
    res.models, res.b_vs_a = score(all_te, pooled, horizon)
    return res


def run(conn: sqlite3.Connection, horizons=HORIZONS, data: str = "backfill", wf: bool = False) -> list[Result]:
    samples = build_samples(conn, lexicon.load_file(lexicon.HANDMADE_PATH), data)
    rows = build_rows(conn, samples)
    out = []
    for h in horizons:
        for s in samples:
            s.ex = s.y = s.act = s.end_day = s.status = None
        label(conn, samples, h)
        out.append(walk_forward(rows, h) if wf else evaluate(rows, h))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="消融實驗：只用市場資料 vs 加入新聞")
    ap.add_argument("--horizon", type=int, choices=HORIZONS, help="只跑某個觀察期間（預設 1、3、5、10 都跑）")
    ap.add_argument("--live", action="store_true", help="用網站收錄的新聞（預設回補的歷史新聞）")
    ap.add_argument("--walk-forward", action="store_true",
                    help="逐月往前滾動：每個月當一次測試期（預設是一次切分，最後三成交易日測試）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    try:
        import sklearn  # noqa: F401
        import scipy  # noqa: F401
    except ImportError:
        print("需要先安裝機器學習套件：pip install -r requirements-ml.txt")
        return 1
    conn = db.connect(args.db)
    res = run(conn, (args.horizon,) if args.horizon else HORIZONS, "live" if args.live else "backfill",
              args.walk_forward)
    pc = lambda x, f="{:.1f}%": "—" if x is None else f.format(x * 100)
    g = lambda x, f="{:.2f}": "—" if x is None else f.format(x)
    print(f"進場規則 {targets.ENTRY_RULE}（{targets.TARGET_VERSION}）；同一批測試樣本；IC＝逐日 Spearman 等級相關的平均，"
          "報酬差＝每天預測前一半 − 後一半；檢定用 Newey–West。* p<0.10 ** p<0.05 *** p<0.01")
    for r in res:
        if r.folds:
            print(f"\n第 {r.horizon} 日　逐月 walk-forward，{len(r.folds)} 折（每折重新挑參數、補值、標準化）")
            for f in r.folds:
                ic, ba = f["ic"], f["balanced"]
                print(f"  {f['month']}　訓練 {f['train'][0]}～{f['train'][1]} {f['n_train']} 筆（剔除 {f['n_purged']}，"
                      f"標籤最晚 {f['train_label_end']}{'' if f['no_overlap'] else ' 重疊！'}）　測試 {f['n_test']} 筆"
                      f"　IC A {g(ic['A：只用市場'], '{:+.3f}')}／N {g(ic['N：只用新聞'], '{:+.3f}')}"
                      f"／B {g(ic['B：市場＋新聞'], '{:+.3f}')}"
                      f"　balanced A {pc(ba['A：只用市場'])}／B {pc(ba['B：市場＋新聞'])}")
            print("  ── 所有測試月合計 ──")
        print(f"\n第 {r.horizon} 日　{'各折' if r.folds else ''}訓練 {r.n_train} 筆（剔除重疊 {r.n_purged}）　測試 {r.n_test} 筆　{r.test_days[0]} ～ {r.test_days[1]}")
        b0 = r.models["0：基準"]["cls"]
        print(f"  0：基準         全猜跌 balanced {pc(b0['always_down']['balanced_accuracy'])}　全猜多數（{b0['majority']['class']}）"
              f" accuracy {pc(b0['majority']['accuracy'])}")
        for name in MODELS:
            m = r.models[name]
            ic, sp, c = m["ic"], m["spread"], m["cls"]
            print(f"  {name:<10} IC {g(ic['mean'], '{:+.3f}')}（t {g(ic['t'])}）{_stars(ic['p'])}"
                  f"　報酬差 {pc(sp['mean'], '{:+.2f}%')}（t {g(sp['t'])}）{_stars(sp['p'])}"
                  f"　三分類 balanced {pc(c['balanced_accuracy'])}、macro F1 {pc(c['macro_f1'])}"
                  f"、命中率 {pc(c['hit_rate'])}（同批全猜跌 {pc(c['baselines']['always_down']['hit_rate_on_signals'])}）"
                  f"、coverage {pc(c['coverage'])}")
            print("               扣成本後報酬差：" + "　".join(
                f"來回 {k * 100:.2f}% → {pc(v['mean'], '{:+.2f}%')}（t {g(v['t'])}）{_stars(v['p'])}"
                for k, v in m["net"].items()))
        d = r.b_vs_a
        print(f"  B − A 的逐日 IC 差 {g(d['mean'], '{:+.3f}')}（{d['n']} 天，t {g(d['t'])}，p {g(d['p'], '{:.3f}')}）{_stars(d['p'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
