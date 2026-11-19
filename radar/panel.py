"""合併所有個股的面板迴歸，依規模分組（2026-11-20）。

問題：股票範圍從市值前 20 檔擴大到前 100 檔以後，新聞情緒有沒有比較好的預測力？訊號是不是只出現在中型股？

做法（同一批樣本，「一檔個股、一個進場交易日」，樣本與特徵沿用 radar/ablation.py）：

一、合併迴歸（pooled OLS）
    第 N 日超額報酬 ＝ b × 新聞情緒 ＋ 控制變數 ＋ 固定效果
    - 新聞情緒：字典加總分數（正 − 負），全樣本標準化，所以 b＝「情緒高一個標準差，之後超額報酬多幾 %」。
    - 控制變數：log(1＋報導則數)、進場日當天超額報酬 r0（動能對照，同 radar/momentum.py）、
      前 1／5／20 日超額報酬、20 日波動度。r0 是控制變數，不是能交易的特徵。
    - 固定效果：一律有「日期」固定效果（每天減掉當天平均，只看同一天個股之間的相對強弱），
      再分兩種設定：(1) 產業固定效果＋市值；(2) 個股固定效果（每檔股票自己的平均報酬，已經包含產業）。
    - 標準誤：Driscoll & Kraay (1998)。先把同一天所有樣本的分數加總，再對這個逐日序列做 Newey–West，
      落後期數 N − 1（第 N 日報酬在相鄰 N − 1 天互相重疊）。第 1 日（落後 0 期）就是「按日期群聚」的標準誤；
      同一天大盤消息讓所有股票一起動，所以不能把每筆樣本當成獨立的。
    - 規模差異：合併迴歸另外放「情緒 × 中型股」交乘項，檢定兩組的係數差。

二、依規模分組
    大型＝市值排名 1～50、中型＝51～100（stocks.market_cap 現在的排名，不是當時的，輕微偷看）。
    每組各自做上面的迴歸，並且不經過任何訓練、直接用字典分數排序：每天組內分數前一半 − 後一半的超額報酬，
    扣交易成本（同 radar/ablation.py 的 COSTS）。--walk-forward 另外在每組跑一次 ablation 的逐月 A／N／B 比較。

只用「回補時有查過」的個股：回補的新聞是向 FinMind 依個股查的，沒查過的個股只會在別檔的新聞裡順帶出現，
樣本有選樣偏差，不能用。查詢天數不到整段期間 90% 的個股也不用。

執行：python -m radar.panel                 （第 1、3、5、10 日）
      python -m radar.panel --horizon 5 --walk-forward
需要 requirements-ml.txt。這個指令只讀資料庫。
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from dataclasses import dataclass, field

from . import ablation, db, targets
from .analysis import lexicon
from .momentum import past_returns
from .significance import _p_two_sided, _stars, newey_west
from .train import build_samples, label
from .validate import HORIZONS

GROUPS = (("大型（市值 1～50）", 1, 50), ("中型（市值 51～100）", 51, 100))
CONTROLS = ("log_news", "r0", "r1", "r5", "r20", "vol20")
MIN_COVERAGE = 0.9      # 回補查詢天數至少要有整段期間的幾成
MIN_ROWS = 200          # 樣本少於這個數就不做迴歸


@dataclass
class Obs:
    row: ablation.Row
    rank: int           # 市值排名（1＝最大）
    r0: float | None


def covered_codes(conn: sqlite3.Connection, min_coverage: float = MIN_COVERAGE) -> set[str]:
    """回補時有依個股查過、而且查詢天數夠完整的個股。"""
    rows = conn.execute("SELECT code, COUNT(*) n, MIN(date) a, MAX(date) b FROM backfill_days GROUP BY code").fetchall()
    if not rows:
        return set()
    span = max(r["n"] for r in rows)
    return {r["code"] for r in rows if r["n"] >= span * min_coverage}


def cap_ranks(conn: sqlite3.Connection) -> dict[str, int]:
    codes = [r[0] for r in conn.execute(
        "SELECT code FROM stocks WHERE market_cap IS NOT NULL ORDER BY market_cap DESC, code")]
    return {c: i + 1 for i, c in enumerate(codes)}


def group_of(rank: int) -> str | None:
    return next((name for name, lo, hi in GROUPS if lo <= rank <= hi), None)


def build_obs(conn: sqlite3.Connection, data: str = "backfill", incidental: bool = False,
              cache: dict | None = None) -> tuple[list[Obs], dict]:
    """incidental＝True：也用「只在別檔新聞裡順帶出現」的個股（探索用，有選樣偏差：只有和大型股一起被報導時才有樣本）。
    cache：傳同一個 dict 進來，第二次呼叫不必重新分析新聞。"""
    samples = build_samples(conn, lexicon.load_file(lexicon.HANDMADE_PATH), data, cache=cache)
    covered = covered_codes(conn) if data == "backfill" and not incidental else None
    ranks = cap_ranks(conn)
    keep = [s for s in samples if (covered is None or s.code in covered) and group_of(ranks.get(s.code, 10 ** 6))]
    info = {"samples": len(samples), "kept": len(keep), "covered": len(covered) if covered is not None else None,
            "dropped_uncovered": sum(1 for s in samples if covered is not None and s.code not in covered),
            "incidental": incidental}
    rows = ablation.build_rows(conn, keep)
    past = past_returns(conn, keep)
    return [Obs(r, ranks[r.sample.code], p.r0) for r, p in zip(rows, past)], info


# ---------- 迴歸 ----------

def fe_ols(y, x, dates, absorb_date: bool = True, lags: int = 0) -> dict:
    """y：長度 n；x：n × k（不含常數）；dates：每筆的日期。

    absorb_date：日期固定效果用「每天減掉當天平均」吸收（Frisch–Waugh–Lovell，係數和放日期虛擬變數一樣）。
    標準誤用 Driscoll–Kraay：逐日加總 x·e，再做 Newey–West（Bartlett 權重，落後 lags 期）。
    回傳 {"beta", "se", "t", "p", "n", "days"}。
    """
    import numpy as np
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    days = sorted(set(dates))
    pos = {d: i for i, d in enumerate(days)}
    di = np.array([pos[d] for d in dates])
    if absorb_date:
        cnt = np.bincount(di, minlength=len(days)).astype(float)

        def demean(a):
            if a.ndim == 1:
                return a - (np.bincount(di, weights=a, minlength=len(days)) / cnt)[di]
            return np.column_stack([demean(a[:, j]) for j in range(a.shape[1])])
        y, x = demean(y), demean(x)
    else:
        x = np.column_stack([np.ones(len(y)), x])
    # 拿掉吸收之後全為 0 的欄（例如某個產業每天只有一檔），避免矩陣不可逆
    live = np.abs(x).sum(axis=0) > 1e-12
    xl = x[:, live]
    xtx_inv = np.linalg.pinv(xl.T @ xl)
    beta_l = xtx_inv @ xl.T @ y
    e = y - xl @ beta_l
    t_days = len(days)
    h = np.zeros((t_days, xl.shape[1]))
    np.add.at(h, di, xl * e[:, None])
    s = h.T @ h
    for lag in range(1, min(lags, t_days - 1) + 1):
        g = h[lag:].T @ h[:-lag]
        s += (1 - lag / (lags + 1)) * (g + g.T)
    s *= t_days / (t_days - 1)          # 群聚數的小樣本修正
    v = xtx_inv @ s @ xtx_inv
    k = x.shape[1]
    beta, se = np.full(k, np.nan), np.full(k, np.nan)
    beta[live], se[live] = beta_l, np.sqrt(np.maximum(np.diag(v), 0))
    if not absorb_date:
        beta, se = beta[1:], se[1:]
    t = [b / s_ if s_ and not math.isnan(s_) else None for b, s_ in zip(beta, se)]
    p = [_p_two_sided(ti, t_days - 1) if ti is not None else None for ti in t]
    return {"beta": [float(b) for b in beta], "se": [float(z) for z in se], "t": t, "p": p,
            "n": len(y), "days": t_days}


def design(obs: list[Obs], spec: str, interact: bool = False) -> tuple[list, list, list, list[str]]:
    """組出 (y, x, dates, 欄名)。缺值的列丟掉。spec：industry（產業＋市值）或 stock（個股固定效果）。"""
    cols = ["score"] + list(CONTROLS) + (["size"] if spec == "industry" else [])
    full = [o for o in obs if o.row.sample.ex is not None and o.r0 is not None
            and all((o.r0 if c == "r0" else o.row.x.get(c)) is not None for c in cols)]
    if not full:
        return [], [], [], []
    def zscore(vals):
        mu = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals)) or 1.0
        return lambda v: (v - mu) / sd
    zs = zscore([o.row.x["score"] for o in full])
    zr = zscore([o.r0 for o in full])          # 當日報酬也標準化，係數才能和情緒直接比
    cats = sorted({(o.row.industry if spec == "industry" else o.row.sample.code) for o in full})[1:]   # 第一類當基準
    names = cols + (["score×中型"] if interact else []) + [f"FE:{c}" for c in cats]
    x = []
    for o in full:
        z = zs(o.row.x["score"])
        vals = [z] + [zr(o.r0) if c == "r0" else o.row.x[c] for c in cols[1:]]
        if interact:
            vals.append(z * (1.0 if o.rank > GROUPS[0][2] else 0.0))
        key = o.row.industry if spec == "industry" else o.row.sample.code
        vals += [1.0 if key == c else 0.0 for c in cats]
        x.append(vals)
    return [o.row.sample.ex for o in full], x, [o.row.sample.day for o in full], names


def regress(obs: list[Obs], spec: str, horizon: int, interact: bool = False) -> dict | None:
    y, x, dates, names = design(obs, spec, interact)
    if len(y) < MIN_ROWS:
        return None
    res = fe_ols(y, x, dates, absorb_date=True, lags=horizon - 1)
    keep = [i for i, n in enumerate(names) if not n.startswith("FE:")]
    return {"n": res["n"], "days": res["days"],
            "coefs": {names[i]: {"b": res["beta"][i], "se": res["se"][i], "t": res["t"][i], "p": res["p"][i]}
                      for i in keep}}


# ---------- 不訓練：直接用字典分數排序 ----------

def raw_spread(obs: list[Obs], horizon: int) -> dict:
    rows = [o.row for o in obs if o.row.sample.ex is not None]
    pred = [r.x["score"] for r in rows]
    ic = ablation.daily_ic(rows, pred)
    sp = list(ablation.daily_spread(rows, pred).values())
    return {"ic": newey_west(list(ic.values()), horizon - 1), "spread": newey_west(sp, horizon - 1),
            "net": {c: newey_west([v - 2 * c for v in sp], horizon - 1) for c in ablation.COSTS}}


@dataclass
class Result:
    horizon: int
    n: int
    pooled: dict = field(default_factory=dict)        # spec → 迴歸結果
    interaction: dict | None = None
    groups: dict = field(default_factory=dict)        # 組名 → {n, stocks, reg: {spec: …}, raw, wf}


def run(conn: sqlite3.Connection, horizons=HORIZONS, data: str = "backfill", wf: bool = False,
        incidental: bool = False, cache: dict | None = None) -> tuple[dict, list[Result]]:
    obs, info = build_obs(conn, data, incidental, cache)
    samples = [o.row.sample for o in obs]
    out = []
    for h in horizons:
        for s in samples:
            s.ex = s.y = s.act = s.end_day = s.status = None
        label(conn, samples, h)
        res = Result(h, sum(s.ex is not None for s in samples))
        for spec in ("industry", "stock"):
            res.pooled[spec] = regress(obs, spec, h)
        res.interaction = regress(obs, "stock", h, interact=True)
        for name, lo, hi in GROUPS:
            g = [o for o in obs if lo <= o.rank <= hi]
            labeled = [o for o in g if o.row.sample.ex is not None]
            entry = {"n": len(labeled), "stocks": len({o.row.sample.code for o in g}),
                     "reg": {spec: regress(g, spec, h) for spec in ("industry", "stock")},
                     "raw": raw_spread(g, h) if labeled else None, "wf": None}
            if wf and len({o.row.sample.day for o in labeled}) > 80:
                try:
                    entry["wf"] = ablation.walk_forward([o.row for o in g], h)
                except ValueError:
                    pass
            res.groups[name] = entry
        out.append(res)
    return info, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="合併所有個股的面板迴歸（固定效果、按日期群聚），依規模分組")
    ap.add_argument("--horizon", type=int, choices=HORIZONS, help="只跑某個觀察期間（預設 1、3、5、10 都跑）")
    ap.add_argument("--walk-forward", action="store_true", help="每組另外跑 ablation 的逐月 A／N／B 比較（較慢）")
    ap.add_argument("--incidental", action="store_true",
                    help="另外跑一次「探索版」：加入只在別檔新聞裡順帶出現的個股（有選樣偏差，結論要保守）")
    ap.add_argument("--live", action="store_true", help="用網站收錄的新聞（預設回補的歷史新聞）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError:
        print("需要先安裝機器學習套件：pip install -r requirements-ml.txt")
        return 1
    conn = db.connect(args.db)
    cache: dict = {}
    hs, data = (args.horizon,) if args.horizon else HORIZONS, "live" if args.live else "backfill"
    show_results(*run(conn, hs, data, args.walk_forward, cache=cache))
    if args.incidental:
        print("\n" + "=" * 70 + "\n探索版：加入只在別檔新聞裡順帶出現的個股。這些個股只有和回補過的大型股一起被報導時才有樣本，"
              "\n有選樣偏差（偏向大新聞、產業新聞），只能當方向參考。\n" + "=" * 70)
        show_results(*run(conn, hs, data, args.walk_forward, incidental=True, cache=cache))
    return 0


def show_results(info: dict, res: list[Result]) -> None:
    pc = lambda x, f="{:+.2f}%": "—" if x is None else f.format(x * 100)
    g = lambda x, f="{:.2f}": "—" if x is None else f.format(x)
    scope = ("市值前 100 的所有個股（含順帶出現的）" if info["incidental"] else
             f"回補有完整查詢的 {info['covered']} 檔裡、市值前 100 的個股（只在別檔新聞裡順帶出現的 {info['dropped_uncovered']} 個不用）")
    print(f"進場規則 {targets.ENTRY_RULE}（{targets.TARGET_VERSION}）。樣本 {info['samples']} 個（個股, 交易日），"
          f"用 {info['kept']} 個：{scope}。")
    print("係數＝情緒（或當日報酬）高一個標準差，之後超額報酬多幾 %；報導量是 log(1＋則數) 每多 1 的效果；日期固定效果；標準誤 Driscoll–Kraay（按日期群聚＋重疊期 Newey–West）。"
          "* p<0.10 ** p<0.05 *** p<0.01")
    show = ("score", "r0", "log_news")
    label_ = {"score": "情緒", "r0": "當日報酬", "log_news": "報導量", "score×中型": "情緒×中型"}

    def coef_line(r):
        if not r:
            return "樣本不足"
        parts = [f"{label_[c]} {pc(r['coefs'][c]['b'])}（t {g(r['coefs'][c]['t'])}）{_stars(r['coefs'][c]['p'])}"
                 for c in show if c in r["coefs"]]
        return "　".join(parts) + f"　〔{r['n']} 筆、{r['days']} 天〕"

    for r in res:
        print(f"\n第 {r.horizon} 日　有結果的樣本 {r.n} 個")
        print(f"  合併　產業＋市值：{coef_line(r.pooled['industry'])}")
        print(f"  合併　個股固定效果：{coef_line(r.pooled['stock'])}")
        it = r.interaction
        if it and "score×中型" in it["coefs"]:
            c = it["coefs"]["score×中型"]
            print(f"  中型股情緒係數比大型股多 {pc(c['b'])}（t {g(c['t'])}，p {g(c['p'], '{:.3f}')}）{_stars(c['p'])}")
        for name, e in r.groups.items():
            print(f"  【{name}】{e['stocks']} 檔、有結果 {e['n']} 筆")
            if not e["n"]:
                print("    沒有樣本（這一組的個股還沒回補新聞）")
                continue
            print(f"    產業＋市值：{coef_line(e['reg']['industry'])}")
            print(f"    個股固定效果：{coef_line(e['reg']['stock'])}")
            raw = e["raw"]
            print(f"    直接用字典分數排序：IC {g(raw['ic']['mean'], '{:+.3f}')}（t {g(raw['ic']['t'])}）{_stars(raw['ic']['p'])}"
                  f"　每日前一半 − 後一半 {pc(raw['spread']['mean'])}（{raw['spread']['n']} 天，t {g(raw['spread']['t'])}）"
                  f"{_stars(raw['spread']['p'])}")
            print("    扣成本後：" + "　".join(f"來回 {k * 100:.2f}% → {pc(v['mean'])}（t {g(v['t'])}）{_stars(v['p'])}"
                                         for k, v in raw["net"].items()))
            w = e["wf"]
            if w:
                ic = {n: w.models[n]["ic"] for n in ablation.MODELS}
                print("    逐月 walk-forward IC：" + "　".join(
                    f"{n[:1]} {g(v['mean'], '{:+.3f}')}{_stars(v['p'])}" for n, v in ic.items())
                    + f"　B − A {g(w.b_vs_a['mean'], '{:+.3f}')}（p {g(w.b_vs_a['p'], '{:.2f}')}）")


if __name__ == "__main__":
    sys.exit(main())
