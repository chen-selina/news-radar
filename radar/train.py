"""由資料估計情緒詞的權重：用「新聞裡出現哪些詞」去解釋「之後股價相對大盤是漲還是跌」。

手工詞典的分數（飆漲 +2、大漲 +1.5）是憑感覺給的。這裡改用邏輯迴歸（logistic regression）從資料估計：
每個詞得到一個係數，正的代表出現之後股價傾向漲、負的代表傾向跌，絕對值越大影響越大。

樣本與特徵
- 一筆樣本＝一檔個股、一個基準交易日（與 python -m radar.validate --dedupe 相同的單位）。
- 特徵＝這檔個股被直接提及的範圍裡，每個情緒詞的出現次數（否定算負的、標題與加強詞有加權，規則同分析引擎）。
  詞的來源是「論文字典＋個股擴充」；同一事件的多篇報導只取一篇，同一天不同事件取平均。
- 標籤＝第 N 個交易日扣除大盤後的報酬，規則在 radar/targets.py（與預測驗證、計分比較共用）：
  高於門檻為漲、低於負門檻為跌，其餘為持平。
  --target 3class（預設）：三類都進入訓練與測試，預測持平＝不發訊號；這才是部署時會遇到的母體。
  --target strong（舊做法）：只用實際漲跌超過門檻的樣本，結果只能說是「已知會明顯漲跌時猜方向」，
  不能當成全市場的準確率。
- 只用出現在足夠多筆樣本裡的詞（--min-count），太少見的詞估不準。

怎麼避免自己騙自己
- 依時間切分：前面的交易日訓練，最後一段交易日測試，測試期的資料完全不參與訓練（含挑選正則化強度）。
- 剔除重疊標籤（purging）：第 N 日報酬在測試開始日收盤時還沒觀測完的訓練樣本不用；
  挑正則化強度的每一折也一樣。N 越大，切點前被剔除的樣本越多。
  隨機切分會讓同一天、同一事件的新聞同時出現在訓練與測試，準確率會虛高。
- 與基準比較：全猜訓練期最多的那一類；不估權重、每個詞 ±1 直接加總（就是字典比較裡的做法）；
  以及同一批測試樣本上的全猜漲／全猜跌／全猜持平。指標由 radar/metrics.py 計算。
  模型在測試期贏不過這兩個基準，就代表估出來的權重沒有用。

執行：python -m radar.train                    （回補的新聞、第 1 個交易日）
      python -m radar.train --horizon 3 --min-count 20 --test-share 0.3
      python -m radar.train --live             （改用網站收錄的新聞）
需要另外安裝：pip install -r requirements-ml.txt
結果存在 data/weights-h{N}.json，不會改動 config/lexicon.json。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field

from . import backfill, config, db, metrics, targets
from .analysis import lexicon, remi
from .analysis.engine import production_analyzer
from .compare import NEWS_SQL, available_at
from .dedupe import event_groups
from .stocks.query import all_stocks
from .validate import Row, validate

MIN_DAYS = 40          # 交易日少於這個數字就提醒結果不可靠
C_GRID = (0.003, 0.01, 0.03, 0.1, 0.3, 1.0)   # 正則化強度的候選值（越小越保守，權重越接近 0）


@dataclass
class Sample:
    code: str
    day: str                      # 基準交易日
    features: dict[str, float]    # 詞 → 加權出現次數
    news: int = 1                 # 合併了幾則新聞
    ex: float | None = None       # 第 N 日扣除大盤後的報酬
    y: int | None = None          # 舊的二元標籤：1＝漲，0＝跌；持平或還沒有結果為 None
    act: str | None = None        # up／down／flat（radar/targets.py）；沒有結果為 None
    end_day: str | None = None    # 標籤結束日（第 N 個交易日）
    status: str | None = None     # label() 之後：done／wait／wait_base／nodata（沒有結果的原因）


@dataclass
class Result:
    horizon: int
    n_train: int
    n_test: int
    train_days: tuple[str, str]
    test_days: tuple[str, str]
    n_days: int
    n_terms: int
    best_c: float
    metrics: dict[str, dict]      # 模型／全猜多數／字典加總 → radar/metrics.py 的 report() ＋ 報酬差
    target: str = "3class"
    n_purged: int = 0             # 標籤和測試期重疊、被剔除的訓練樣本
    weights: dict[str, float] = field(repr=False, default_factory=dict)
    counts: dict[str, int] = field(repr=False, default_factory=dict)
    intercept: float = 0.0


def build_samples(conn: sqlite3.Connection, base: lexicon.Lexicon, data: str = "backfill",
                  lex: lexicon.Lexicon | None = None, skip: set[int] | None = None,
                  cache: dict | None = None, entry: str = targets.ENTRY_RULE) -> list[Sample]:
    """分析所有新聞，整理成「一檔個股、一個基準交易日」一筆的特徵。還沒有標籤。

    lex：改用別的字典（預設是論文＋個股擴充）。skip：不要算進來的新聞 id。
    cache：傳同一個 dict 進來，重複呼叫時（只換 skip）不必重新分析新聞。
    entry：進場規則（radar/targets.py）。預設 close：基準日＝資訊可取得之後第一個買得到的收盤，
    同一筆樣本只用在那個收盤之前（13:00 截止）就能取得的新聞。legacy＝舊規則，只供比較。
    """
    cache = {} if cache is None else cache
    if cache.get("entry") not in (None, entry):
        raise ValueError("cache 是用別的進場規則建的，請換一個 cache")
    cache["entry"] = entry
    if "news" not in cache:
        an = production_analyzer(lex or remi.as_lexicon(base, extend=True), all_stocks(conn))
        days = [r[0] for r in conn.execute("SELECT date FROM trading_days ORDER BY date")]
        news = conn.execute(NEWS_SQL[data]).fetchall()
        cache["event"] = event_groups([(n["id"], n["title"], n["t"]) for n in news])
        cache["news"] = []          # [(新聞 id, 基準日, [(個股代號, 詞 → 加權次數)])]
        for n in news:
            d, t = available_at(n)
            bi = targets.entry_index(days, d, t, entry)
            if bi is None or bi < 0:
                continue
            body = backfill.clean_body(n["body"] or "") if data == "backfill" else (n["body"] or "")
            hits = [(v.stock.code, dict(v.terms)) for v in an.analyze(n["title"], body).impacts
                    if v.type == "direct" and v.terms]
            cache["news"].append((n["id"], days[bi], hits))
    event = cache["event"]
    # (個股, 基準日) → 事件 → 這個事件裡情緒詞最多的那一篇
    groups: dict[tuple[str, str], dict[int, dict[str, float]]] = {}
    seen: dict[tuple[str, str], int] = {}
    for nid, day, hits in cache["news"]:
        if skip and nid in skip:
            continue
        for code, terms in hits:
            key = (code, day)
            seen[key] = seen.get(key, 0) + 1
            best = groups.setdefault(key, {})
            e = event[nid]
            if e not in best or sum(map(abs, terms.values())) > sum(map(abs, best[e].values())):
                best[e] = terms
    out = []
    for (code, day), events in groups.items():
        feats: dict[str, float] = {}
        for f in events.values():
            for term, x in f.items():
                feats[term] = feats.get(term, 0.0) + x / len(events)
        out.append(Sample(code, day, feats, seen[(code, day)]))
    return sorted(out, key=lambda s: (s.day, s.code))


def label(conn: sqlite3.Connection, samples: list[Sample], horizon: int = 1, benchmark: str = "excess",
          threshold: float = 0.01) -> None:
    """用和預測驗證相同的規則填入每筆樣本的報酬與標籤。"""
    rows = [Row(i, s.day, "", "", "", "", None, s.code, s.code, "direct", None, 1.0, "up")
            for i, s in enumerate(samples)]
    res = validate(conn, horizon, benchmark, threshold, rows=rows, entry="given")   # s.day 已經是起點交易日
    for r in res["rows"]:
        s = samples[r["news_id"]]
        s.status = r["status"]
        if r["status"] == "done":
            s.ex, s.act, s.end_day = r["ex"], r["act"], r["end_date"]
            s.y = 1 if r["act"] == "up" else 0 if r["act"] == "down" else None


def _matrix(samples: list[Sample], terms: list[str]):
    import numpy as np
    col = {t: j for j, t in enumerate(terms)}
    x = np.zeros((len(samples), len(terms)))
    for i, s in enumerate(samples):
        for t, v in s.features.items():
            if t in col:
                x[i, col[t]] = v
    return x


def _evaluate(pred: list[str], samples: list[Sample]) -> dict:
    """pred：每筆樣本 up／down／flat。共用指標＋「猜漲的平均報酬 − 猜跌的平均報酬」。"""
    m = metrics.report(pred, [s.act for s in samples])
    ups = [s.ex for p, s in zip(pred, samples) if p == "up"]
    downs = [s.ex for p, s in zip(pred, samples) if p == "down"]
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    m.update({"up_n": len(ups), "down_n": len(downs), "up_avg": avg(ups), "down_avg": avg(downs),
              "spread": avg(ups) - avg(downs) if ups and downs else None})
    return m


TARGETS = ("3class", "strong")


def train(samples: list[Sample], horizon: int = 1, min_count: int = 15, test_share: float = 0.3,
          target: str = "3class") -> Result:
    """依時間切分後估計權重，回傳測試期的表現與每個詞的權重。samples 要先經過 label()。

    target＝3class：down／flat／up 三類都用（預設）；strong：只用實際漲跌超過門檻的樣本（舊做法）。
    """
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        raise RuntimeError("需要先安裝機器學習套件：pip install -r requirements-ml.txt") from None
    if target not in TARGETS:
        raise ValueError(f"target 只能是 {'／'.join(TARGETS)}")

    data = [s for s in samples if s.act is not None and (target == "3class" or s.act != "flat")]
    if any(s.end_day is None for s in data):
        raise ValueError("有樣本缺標籤結束日（end_day），無法剔除重疊標籤；請先用 label() 填標籤")
    dates = sorted({s.day for s in data})
    if len(dates) < 4:
        raise ValueError(f"有結果的交易日只有 {len(dates)} 天，至少要 4 天才能切分訓練與測試")
    n_test_days = max(1, round(len(dates) * test_share))
    cut = dates[-n_test_days]
    te = [s for s in data if s.day >= cut]
    before = [s for s in data if s.day < cut]
    tr = [s for s in before if targets.observed_by(s.end_day, cut)]   # purging：模型在測試開始日收盤擬合
    if len({s.act for s in tr}) < 2:
        raise ValueError("訓練期的樣本只有一種結果（只有漲或只有跌），無法估計")

    counts: dict[str, int] = {}
    for s in tr:   # 只看訓練期：用測試期決定要留哪些詞，也算偷看
        for t, v in s.features.items():
            if v:
                counts[t] = counts.get(t, 0) + 1
    terms = sorted(t for t, n in counts.items() if n >= min_count)
    if not terms:
        raise ValueError(f"沒有任何詞在訓練期出現 {min_count} 筆以上，請降低 --min-count")
    xtr, ytr = _matrix(tr, terms), np.array([s.act for s in tr])

    def fit(x, y, c):
        # class_weight＝balanced：持平通常是最多的一類，不加權的話模型容易全猜持平
        return LogisticRegression(C=c, max_iter=2000, class_weight="balanced").fit(x, y)

    def bacc(pred, y) -> float:
        return metrics.report(list(pred), list(y))["balanced_accuracy"] or 0.0

    # 挑正則化強度：只在訓練期裡做，前段訓練、後段驗證（往前滾動 3 次），每折一樣剔除重疊標籤
    tr_dates = sorted({s.day for s in tr})
    best_c, best_score = C_GRID[0], -1.0
    if len(tr_dates) >= 6:
        idx_day = np.array([s.day for s in tr])
        idx_end = np.array([s.end_day for s in tr])
        folds = [tr_dates[int(len(tr_dates) * f)] for f in (0.5, 0.67, 0.84)]
        ends = folds[1:] + ["9999"]
        for c in C_GRID:
            scores = []
            for start, end in zip(folds, ends):
                a, b = (idx_day < start) & (idx_end <= start), (idx_day >= start) & (idx_day < end)
                if b.sum() == 0 or len(set(ytr[a])) < 2:
                    continue
                scores.append(bacc(fit(xtr[a], ytr[a], c).predict(xtr[b]), ytr[b]))
            score = float(np.mean(scores)) if scores else -1.0
            if score > best_score:
                best_c, best_score = c, score
    model = fit(xtr, ytr, best_c)

    xte = _matrix(te, terms)
    majority = max(sorted(set(ytr)), key=lambda c: (int((ytr == c).sum()), c == "flat"))
    unit = xte.sum(axis=1)   # 不估權重：每個詞 ±1 加總
    neutral = "flat" if target == "3class" else majority
    res_metrics = {
        "模型（估計的權重）": _evaluate(list(model.predict(xte)), te),
        "全猜多數": _evaluate([majority] * len(te), te),
        "字典加總（每個詞 ±1）": _evaluate(["up" if u > 0 else "down" if u < 0 else neutral for u in unit], te),
    }
    cls = list(model.classes_)
    coef = model.coef_
    if len(cls) == 2:      # 二元時 sklearn 只給一列：正的代表偏向 cls[1]
        sign = 1.0 if cls[1] == "up" or cls[0] == "down" else -1.0
        w, b0 = sign * coef[0], sign * model.intercept_[0]
    else:                  # 三類：漲對跌的對數勝算
        w = coef[cls.index("up")] - coef[cls.index("down")]
        b0 = model.intercept_[cls.index("up")] - model.intercept_[cls.index("down")]
    weights = {t: float(x) for t, x in zip(terms, w)}
    return Result(horizon, len(tr), len(te), (tr[0].day, tr[-1].day), (te[0].day, te[-1].day), len(dates),
                  len(terms), best_c, res_metrics, target, len(before) - len(tr), weights,
                  {t: counts[t] for t in terms}, float(b0))


def save(res: Result, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    order = sorted(res.weights, key=lambda t: -res.weights[t])
    path.write_text(json.dumps({
        "_說明": "python -m radar.train 的結果：邏輯迴歸係數，正的代表出現之後股價（扣除大盤）傾向漲。"
                 "只是估計結果，沒有套用到 config/lexicon.json。",
        "created_at": config.now_iso(), "horizon": res.horizon, "target": res.target,
        "target_version": targets.TARGET_VERSION, "train_days": res.train_days, "test_days": res.test_days,
        "n_train": res.n_train, "n_test": res.n_test, "n_purged": res.n_purged,
        "regularization_C": res.best_c, "intercept": res.intercept,
        "metrics": res.metrics,
        "weights": [{"term": t, "weight": round(res.weights[t], 4), "samples": res.counts[t]} for t in order],
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="由資料估計情緒詞的權重（邏輯迴歸，依時間切分訓練與測試）")
    ap.add_argument("--horizon", type=int, default=1, help="用第幾個交易日的報酬當標籤（預設 1）")
    ap.add_argument("--min-count", type=int, default=15, help="詞至少要出現在幾筆訓練樣本裡才納入（預設 15）")
    ap.add_argument("--test-share", type=float, default=0.3, help="最後多少比例的交易日留作測試（預設 0.3）")
    ap.add_argument("--threshold", type=float, default=1.0, help="漲跌門檻，單位 %%（預設 1；中間的算持平）")
    ap.add_argument("--target", choices=TARGETS, default="3class",
                    help="3class＝漲／持平／跌都用（預設）；strong＝只用實際漲跌超過門檻的樣本（舊做法）")
    ap.add_argument("--raw", action="store_true", help="用個股漲跌（預設扣除大盤）")
    ap.add_argument("--live", action="store_true", help="改用網站收錄的新聞（預設用回補的歷史新聞）")
    ap.add_argument("--top", type=int, default=15, help="列出權重最大、最小的各幾個詞（預設 15）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    samples = build_samples(conn, lexicon.load_file(lexicon.HANDMADE_PATH), "live" if args.live else "backfill")
    label(conn, samples, args.horizon, "raw" if args.raw else "excess", args.threshold / 100)
    try:
        res = train(samples, args.horizon, args.min_count, args.test_share, args.target)
    except (RuntimeError, ValueError) as e:
        print(e)
        return 1
    pct = lambda x, f="{:.0f}%": "—" if x is None else f.format(x * 100)
    labeled = sum(s.act is not None for s in samples)
    print(f"樣本：{len(samples)} 個（個股, 交易日），有結果的 {labeled} 個；"
          f"{'三類都用' if res.target == '3class' else '只用實際漲跌超過門檻的（條件於實際非持平，不是全市場準確率）'}，共 {res.n_days} 個交易日")
    print(f"訓練：{res.train_days[0]} ～ {res.train_days[1]}，{res.n_train} 筆（另剔除 {res.n_purged} 筆標籤跨進測試期的）"
          f"　測試：{res.test_days[0]} ～ {res.test_days[1]}，{res.n_test} 筆")
    print(f"納入 {res.n_terms} 個詞（訓練期出現 {args.min_count} 筆以上），正則化強度 C＝{res.best_c}")
    print(f"\n測試期（第 {args.horizon} 個交易日、{'個股漲跌' if args.raw else '扣除大盤'}、門檻 ±{args.threshold}%）：")
    for name, m in res.metrics.items():
        print(f"  {name:<14} 準確率 {pct(m['accuracy']):>4}　balanced {pct(m['balanced_accuracy']):>4}"
              f"　macro F1 {pct(m['macro_f1']):>4}　coverage {pct(m['coverage']):>4}　命中率 {pct(m['hit_rate']):>4}"
              f"　猜漲 {m['up_n']} 筆平均 {pct(m['up_avg'], '{:+.1f}%')}"
              f"　猜跌 {m['down_n']} 筆平均 {pct(m['down_avg'], '{:+.1f}%')}　報酬差 {pct(m['spread'], '{:+.1f}%')}")
    print("  同一批測試樣本：" + metrics.brief(res.metrics["模型（估計的權重）"]))
    order = sorted(res.weights, key=lambda t: -res.weights[t])
    show = lambda ts: "　".join(f"{t} {res.weights[t]:+.2f}（{res.counts[t]}）" for t in ts)
    print(f"\n權重最大的 {args.top} 個詞（括號是訓練期出現的筆數）：\n  {show(order[:args.top])}")
    print(f"權重最小的 {args.top} 個詞：\n  {show(order[::-1][:args.top])}")
    out = config.DATA_DIR / f"weights-h{args.horizon}.json"
    save(res, out)
    print(f"\n完整結果存在 {out}")
    if res.n_days < MIN_DAYS:
        print(f"\n注意：只有 {res.n_days} 個交易日的資料（建議至少 {MIN_DAYS} 個）。同一天的個股會一起漲跌，"
              "獨立的樣本比筆數少得多；現在的權重和準確率可能只反映這幾天的行情，不能當成結論。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
