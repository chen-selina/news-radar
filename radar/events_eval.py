"""時事新聞的預測力：用 radar/events.py 抓的時事新聞，檢驗兩件事。

一、大盤：每個交易日把時事新聞的情緒總分取平均，預測加權指數之後 N 個交易日的漲跌。
  - 命中率兩種判斷：和 0 比（總分 > 0 猜漲），以及和全期平均比（新聞整體偏正面，和 0 比會幾乎全猜漲）。
  - 對照：同一批交易日「每天都猜漲」的命中率。
  - 迴歸：之後 N 日報酬 ＝ a ＋ b×情緒（標準化）［＋ c×基準日當天的大盤報酬］，Newey–West 標準誤。
    控制當天報酬，是為了排除「新聞只是在描述今天的漲跌」（同 radar/momentum.py 的想法）。
    註：「和全期平均比」用到了整段期間的平均，嚴格說有一點偷看；只當參考，看迴歸比較準。

二、個股與產業：正式分析器（直接提及＋產業連動整個產業）給出的每檔個股預測，
  用和 python -m radar.validate --dedupe 相同的規則驗證（扣除大盤、門檻 ±1%、一檔個股一天算一筆），
  並控制基準日當天個股的超額報酬（Fama–MacBeth）。

基準日規則同預測驗證：09:00 開盤前發布的用前一個交易日收盤，開盤後的用當天收盤。
只讀資料庫，不會寫入。需要 numpy（pip install -r requirements-ml.txt）。

執行：python -m radar.events_eval
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys

from . import db
from .analysis import lexicon
from .analysis.engine import label_of, production_analyzer
from .events import CATEGORIES
from .momentum import fama_macbeth, past_returns
from .significance import _p_two_sided, _stars, daily_spreads, newey_west
from .stocks.query import all_stocks
from .validate import HORIZONS, PRED, TAIEX, Row, _prices, _split_time, base_index, validate

MIN_DAYS = 30   # 交易日太少就不做大盤的檢定


def ols_nw(y: list[float], cols: list[list[float]], lags: int = 0) -> list[dict]:
    """一般最小平方法，Newey–West 標準誤。回傳每個解釋變數（不含截距）的係數、t、p。"""
    import numpy as np
    X = np.column_stack([np.ones(len(y)), *[np.asarray(c, dtype=float) for c in cols]])
    Y = np.asarray(y, dtype=float)
    b = np.linalg.lstsq(X, Y, rcond=None)[0]
    e = Y - X @ b
    inv = np.linalg.pinv(X.T @ X)
    Xe = X * e[:, None]
    S = Xe.T @ Xe
    for k in range(1, min(lags, len(Y) - 1) + 1):
        G = Xe[k:].T @ Xe[:-k]
        S += (1 - k / (lags + 1)) * (G + G.T)
    se = np.sqrt(np.maximum(np.diag(inv @ S @ inv), 0))
    out = []
    for j in range(1, X.shape[1]):
        t = float(b[j] / se[j]) if se[j] > 0 else None
        out.append({"coef": float(b[j]), "t": t, "p": _p_two_sided(t, len(Y) - X.shape[1]) if t is not None else None})
    return out


def market_test(daily: dict[str, list[float]], days: list[str], index: dict[str, float], horizon: int) -> dict | None:
    """daily：基準交易日 → 那天時事新聞的總分。index：交易日 → 加權指數收盤。"""
    pos = {d: i for i, d in enumerate(days)}
    xs, ys, r0 = [], [], []
    for d, scores in sorted(daily.items()):
        i = pos.get(d)
        if i is None or i < 1 or i + horizon >= len(days):
            continue
        p_prev, p0, p1 = index.get(days[i - 1]), index.get(d), index.get(days[i + horizon])
        if not (p_prev and p0 and p1):
            continue
        xs.append(sum(scores) / len(scores))
        ys.append(p1 / p0 - 1)
        r0.append(p0 / p_prev - 1)
    n = len(xs)
    if n < MIN_DAYS:
        return None
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / n) or 1.0
    z = [(x - mean) / sd for x in xs]
    vs_zero = [(x > 0) == (y > 0) for x, y in zip(xs, ys) if x != 0]
    return {"horizon": horizon, "days": n,
            "hit_vs_zero": sum(vs_zero) / len(vs_zero) if vs_zero else None,
            "pred_up_vs_zero": sum(x > 0 for x in xs) / n,
            "hit_vs_mean": sum((x > mean) == (y > 0) for x, y in zip(xs, ys)) / n,
            "all_up": sum(y > 0 for y in ys) / n,
            "alone": ols_nw(ys, [z], horizon - 1)[0],
            "control": ols_nw(ys, [z, r0], horizon - 1)[0]}


class _Key:   # past_returns 只需要 code 與 day
    def __init__(self, code: str, day: str):
        self.code, self.day = code, day


def stock_test(conn: sqlite3.Connection, rows: list[Row], horizon: int, scope: str) -> dict:
    res = validate(conn, horizon, "excess", 0.01, scope, rows=[Row(**vars(r)) for r in rows], dedupe=True)
    done = [r for r in res["rows"] if r["status"] == "done"]
    judged = [r for r in done if r["pred"] != "flat"]
    past = past_returns(conn, [_Key(r["code"], r["base_date"]) for r in done])
    reg = [(r["base_date"], r["ex"], [r["score"], q.r0]) for r, q in zip(done, past) if q.r0 is not None]
    fm = fama_macbeth(reg, horizon - 1) or [None]
    s = res["summary"]
    return {"horizon": horizon, "scope": scope, "n": s["n"], "hit_rate": s["hit_rate"],
            "up_n": s["up_n"], "down_n": s["down_n"],
            "all_down": sum(r["act"] == "down" for r in judged) / len(judged) if judged else None,
            "spread": newey_west(list(daily_spreads(res["rows"]).values()), horizon - 1),
            "control": fm[0]}


def run(conn: sqlite3.Connection, lex: lexicon.Lexicon) -> dict:
    an = production_analyzer(lex, all_stocks(conn))
    days = [r[0] for r in conn.execute("SELECT date FROM trading_days ORDER BY date")]
    index = _prices(conn, {TAIEX}).get(TAIEX, {})
    groups = {**{name: {cat} for cat, name in CATEGORIES.items()}, "全部時事": set(CATEGORIES)}
    daily = {g: {} for g in groups}
    rows = {g: [] for g in groups}
    n_news = {g: 0 for g in groups}
    for n in conn.execute("SELECT id, category, title, body, url, published_at FROM events_news ORDER BY id"):
        d, t = _split_time(n["published_at"])
        a = an.analyze(n["title"], n["body"])
        bi = base_index(days, d, t)
        new_rows = [Row(n["id"], d, t, n["title"], n["url"], "events", None, v.stock.code, v.stock.name, v.type,
                        v.sector, v.score, PRED[label_of(v.score)]) for v in a.impacts]
        for g, cats in groups.items():
            if n["category"] not in cats:
                continue
            n_news[g] += 1
            if bi is not None and bi >= 0:
                daily[g].setdefault(days[bi], []).append(a.total)
            rows[g].extend(new_rows)
    out = {"news": n_news, "market": [], "stocks": []}
    for g in groups:
        for h in HORIZONS:
            m = market_test(daily[g], days, index, h)
            if m:
                out["market"].append({"group": g, **m})
        for scope in ("direct", "all"):
            for h in HORIZONS:
                out["stocks"].append({"group": g, **stock_test(conn, rows[g], h, scope)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="時事新聞能不能預測大盤與個股")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    if not conn.execute("SELECT 1 FROM events_news LIMIT 1").fetchone():
        print("還沒有時事新聞，請先執行 python -m radar.events")
        return 1
    res = run(conn, lexicon.load_file())
    pct = lambda x, f="{:.0f}%": "—" if x is None else f.format(x * 100)
    reg = lambda r: "—" if not r or r.get("p") is None else \
        f"{r.get('coef', r.get('mean')) * 100:+.2f}%（p＝{r['p']:.3f}）{_stars(r['p'])}"
    print("時事新聞：" + "、".join(f"{g} {n} 則" for g, n in res["news"].items()))
    print("\n一、大盤：每天時事新聞的平均總分 → 加權指數之後 N 日漲跌")
    print("  情緒係數＝情緒高一個標準差，之後大盤報酬多幾 %；* p<0.10 ** p<0.05 *** p<0.01")
    for r in res["market"]:
        print(f"  {r['group']} 第 {r['horizon']:>2} 日（{r['days']} 天）　命中率：和 0 比 {pct(r['hit_vs_zero'])}"
              f"（猜漲 {pct(r['pred_up_vs_zero'])} 的天數）、和平均比 {pct(r['hit_vs_mean'])}　每天都猜漲 {pct(r['all_up'])}"
              f"　情緒係數 {reg(r['alone'])}　控制當天大盤報酬後 {reg(r['control'])}")
    print("\n二、個股與產業（扣除大盤、門檻 ±1%、一檔個股一天算一筆）")
    for r in res["stocks"]:
        sp = r["spread"]
        print(f"  {r['group']} {'只看直接提及' if r['scope'] == 'direct' else '含產業連動'} 第 {r['horizon']:>2} 日"
              f"　{r['n']} 筆　命中率 {pct(r['hit_rate'])}　全猜跌 {pct(r['all_down'])}"
              f"　多空報酬差 {reg(sp)}　控制當日報酬後的情緒係數 {reg(r['control'])}")
    days = max((r["days"] for r in res["market"]), default=0)
    if days < 120:
        print(f"\n注意：只有 {days} 個交易日，檢定力很低，沒有顯著不代表沒有效果。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
