"""網站的主要預測：機器學習模型（2026-11-20 起；字典規則的預測照常建立，當對照組）。

模型（使用者選定，依據見 README「消融實驗」與「面板迴歸」）：
- 只看新聞的三分類邏輯迴歸（radar/ablation.py 的 Model N）：特徵＝正面詞數、負面詞數、加總分數、log(1＋報導則數)。
  逐月 walk-forward 裡只有這個模型在樣本外顯著（IC 約 +0.01～+0.05）；加入市場資料的模型沒有比較好。
- 每個觀察期間（1／3／5／10 日）各訓練一個，標籤＝第 N 日扣除大盤的超額報酬漲／持平／跌（radar/targets.py，還原股價）。
- 訓練資料：回補的歷史新聞（train.build_samples）。正則化強度用訓練期內往前滾動 3 折挑（ablation.fit_classifier），
  再用全部有標籤的樣本擬合。參數整個存在 ml_models，建立後不可修改；要改就訓練新版本。
- 算特徵的字典：論文字典＋個股擴充（和訓練時相同，不是網站詞典頁那一部）。

預測與鎖定（和字典預測同一套原則，規格書 6.1）：
- 新聞收錄當下（predict.create_prediction 之後）替「直接提及、而且有情緒詞」的個股，各期間各建立一筆，存進 ml_predictions，不可修改。
  產業連動的個股不預測（模型只用直接提及的樣本訓練）。
- 單則新聞的特徵＝這則新聞對該股的詞數；報導量＝網站上同一個進場日、已經直接提及這檔股票（有情緒詞）的新聞則數（含這一則）。
  訓練樣本是「每檔每日、同事件取一篇再平均」，單則預測用這則新聞本身，兩者略有不同。
- 用新模型套到舊新聞 → ml_recalcs（重算結果），和原始預測分開。

誠實標示：這個模型的 balanced accuracy 只有約 35%（亂猜 33%），命中率輸給全猜跌，扣完整交易成本後不賺錢。

執行：python -m radar.mlmodel train          （訓練新版本，含逐月 walk-forward 評估；約 15～30 分鐘）
      python -m radar.mlmodel train --no-eval
      python -m radar.mlmodel recalc         （用最新模型重算網站上所有新聞）
      python -m radar.mlmodel status
需要 requirements-ml.txt。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass

from . import config, db, metrics, targets
from .analysis import lexicon, remi
from .analysis.engine import Analyzer, production_analyzer
from .significance import newey_west
from .stocks.query import all_stocks
from .validate import HORIZONS

FEATURES = ("pos", "neg", "score", "log_news")      # 和 radar/ablation.py 的 NEWS 相同
LABEL_OF = {"up": "pos", "down": "neg", "flat": "neu"}
# 網站上的判斷門檻（2026-11-20，使用者同意）：上漲機率 − 下跌機率 ≥ 5 個百分點才算利多，≤ −5 才算利空，其餘中性。
# 模型的機率都很接近 1/3，取最大類別的話很多判斷只差 1～3 個百分點。資料庫裡存的 label 是原始的最大類別（不變、鎖定），
# 門檻在讀取時套用（validate、API、貼連結預覽），所以新舊預測一律用同一套規則，改門檻也不用動鎖定的資料。
MARGIN = 0.05


def decide(p_up: float, p_down: float, margin: float = MARGIN) -> str:
    """網站顯示與驗證用的方向：pos／neg／neu。"""
    d = p_up - p_down
    return "pos" if d >= margin - 1e-12 else "neg" if d <= -margin + 1e-12 else "neu"


# ---------- 模型 ----------

@dataclass
class Model:
    version: int
    horizon: int
    classes: list[str]              # sklearn 的類別順序（down／flat／up）
    coef: list[list[float]]         # 類別 × 特徵
    intercept: list[float]
    med: list[float]                # 補值（訓練期中位數）
    mu: list[float]                 # 標準化
    sd: list[float]
    C: float
    eval: dict | None = None
    created_at: str = ""
    train_first: str | None = None
    train_last: str | None = None
    n_train: int = 0

    def proba(self, x: dict[str, float | None]) -> dict[str, float]:
        """{down, flat, up: 機率}。多類別邏輯迴歸＝softmax（sklearn 的 lbfgs 預設 multinomial）。"""
        z = []
        for j, f in enumerate(FEATURES):
            v = x.get(f)
            v = self.med[j] if v is None or (isinstance(v, float) and math.isnan(v)) else v
            z.append((v - self.mu[j]) / self.sd[j])
        logits = [sum(c * zi for c, zi in zip(row, z)) + b for row, b in zip(self.coef, self.intercept)]
        m = max(logits)
        e = [math.exp(v - m) for v in logits]
        s = sum(e)
        return {k: v / s for k, v in zip(self.classes, e)}

    def predict(self, x: dict[str, float | None]) -> tuple[str, dict[str, float]]:
        p = self.proba(x)
        return LABEL_OF[max(p, key=p.get)], p

    def params(self) -> dict:
        return {"classes": self.classes, "coef": self.coef, "intercept": self.intercept,
                "med": self.med, "mu": self.mu, "sd": self.sd, "C": self.C}


def has_models(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM ml_models LIMIT 1").fetchone() is not None


def load(conn: sqlite3.Connection, version: int | None = None) -> dict[int, Model]:
    """{期間: 模型}；version 為 None＝最新版。沒有模型回傳空 dict。"""
    if version is None:
        version = conn.execute("SELECT MAX(version) FROM ml_models").fetchone()[0]
        if version is None:
            return {}
    out = {}
    for r in conn.execute("SELECT * FROM ml_models WHERE version = ? ORDER BY horizon", (version,)):
        p = json.loads(r["params_json"])
        out[r["horizon"]] = Model(r["version"], r["horizon"], p["classes"], p["coef"], p["intercept"], p["med"],
                                  p["mu"], p["sd"], p["C"], json.loads(r["eval_json"]) if r["eval_json"] else None,
                                  r["created_at"], r["train_first"], r["train_last"], r["n_train"])
    return out


def feature_lexicon() -> lexicon.Lexicon:
    """算特徵用的字典：和訓練樣本（train.build_samples 的預設）相同的「論文字典＋個股擴充」。"""
    return remi.as_lexicon(lexicon.load_file(lexicon.HANDMADE_PATH), extend=True)


_AN: dict = {}


def analyzer(conn: sqlite3.Connection) -> Analyzer:
    stocks = all_stocks(conn)
    lex = feature_lexicon()
    key = (lex.checksum(), len(stocks), stocks[0].code if stocks else "")
    if key not in _AN:
        _AN.clear()
        _AN[key] = production_analyzer(lex, stocks)
    return _AN[key]


# ---------- 訓練 ----------

def _walk_forward_n(rows, horizon: int) -> dict:
    """只跑 N（只看新聞）的逐月 walk-forward，回傳樣本外指標（radar/ablation.py 同一套切分與檢定）。"""
    from . import ablation
    rows = sorted(rows, key=lambda r: r.sample.day)
    te_all, reg_all, cls_all, months = [], [], [], []
    for m, tr, te, _ in ablation.monthly_folds(rows):
        if len({r.sample.act for r in tr}) < 2:
            continue
        reg, _ = ablation.fit_regression(tr, te, FEATURES, False)
        cls, _ = ablation.fit_classifier(tr, te, FEATURES, False)
        te_all += te
        reg_all += [float(x) for x in reg]
        cls_all += list(cls)
        months.append(m)
    if not te_all:
        return {}
    ic = newey_west(list(ablation.daily_ic(te_all, reg_all).values()), horizon - 1)
    sp = list(ablation.daily_spread(te_all, reg_all).values())
    rep = metrics.report(cls_all, [r.sample.act for r in te_all])
    return {"months": months, "n_test": len(te_all), "test_first": te_all[0].sample.day, "test_last": te_all[-1].sample.day,
            "ic": ic["mean"], "ic_p": ic["p"], "spread": newey_west(sp, horizon - 1)["mean"],
            "net": {f"{c:g}": newey_west([x - 2 * c for x in sp], horizon - 1)["mean"] for c in ablation.COSTS},
            "balanced_accuracy": rep["balanced_accuracy"], "hit_rate": rep["hit_rate"],
            "all_down_hit_rate": rep["baselines"]["always_down"]["hit_rate_on_signals"], "coverage": rep["coverage"]}


def train(conn: sqlite3.Connection, horizons=HORIZONS, evaluate: bool = True, log=print) -> int:
    """訓練新版本（每個期間一個），存進 ml_models，回傳版本號。"""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from . import ablation
    from .train import build_samples, label

    log("分析回補新聞、整理樣本（約 10～15 分鐘）")
    samples = build_samples(conn, lexicon.load_file(lexicon.HANDMADE_PATH), "backfill")
    rows = [ablation.Row(s, ablation.news_features(s), "") for s in samples]
    version = (conn.execute("SELECT MAX(version) FROM ml_models").fetchone()[0] or 0) + 1
    out = []
    for h in horizons:
        for s in samples:
            s.ex = s.y = s.act = s.end_day = s.status = None
        label(conn, samples, h)
        lab = sorted((r for r in rows if r.sample.act is not None and r.sample.ex is not None), key=lambda r: r.sample.day)
        if len({r.sample.act for r in lab}) < 2:
            raise ValueError(f"第 {h} 日有標籤的樣本不夠（{len(lab)} 筆）")
        _, c = ablation.fit_classifier(lab, lab[:1], FEATURES, False)      # 只用來挑 C（訓練期內往前滾動 3 折）
        d = ablation.Design(FEATURES, False).fit(lab)
        m = LogisticRegression(C=c, max_iter=2000, class_weight="balanced").fit(
            d.transform(lab), np.array([r.sample.act for r in lab]))
        model = Model(version, h, [str(k) for k in m.classes_], m.coef_.tolist(), m.intercept_.tolist(),
                      d.med.tolist(), d.mu.tolist(), d.sd.tolist(), c, n_train=len(lab),
                      train_first=lab[0].sample.day, train_last=lab[-1].sample.day)
        if evaluate:
            log(f"第 {h} 日：逐月 walk-forward 評估")
            model.eval = _walk_forward_n(lab, h)
        out.append(model)
        log(f"第 {h} 日：{len(lab)} 筆、{model.train_first}～{model.train_last}，C＝{c}")
    now = config.now_iso()
    checksum = feature_lexicon().checksum()
    with conn:
        conn.executemany(
            "INSERT INTO ml_models (version, horizon, created_at, features, params_json, lexicon_checksum, target_version, "
            "train_first, train_last, n_train, eval_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(version, m.horizon, now, json.dumps(FEATURES), json.dumps(m.params()), checksum, targets.TARGET_VERSION,
              m.train_first, m.train_last, m.n_train, json.dumps(m.eval, ensure_ascii=False) if m.eval else None)
             for m in out])
    return version


# ---------- 預測 ----------

def _entry_key(days: list[str], *isos: str | None) -> str:
    d, t = targets.available_at(*isos)
    bi = targets.entry_index(days, d, t, targets.ENTRY_RULE) if d else None
    return days[bi] if bi is not None and bi >= 0 else "?" + d


def news_features(an: Analyzer, title: str, body: str) -> dict[str, dict[str, float]]:
    """{個股: {pos, neg, score}}：直接提及、而且有情緒詞的個股。"""
    out = {}
    for v in an.analyze(title, body).impacts:
        if v.type != "direct" or not v.terms:
            continue
        pos = sum(x for x in v.terms.values() if x > 0)
        neg = -sum(x for x in v.terms.values() if x < 0)
        out[v.stock.code] = {"pos": pos, "neg": neg, "score": pos - neg}
    return out


def _rows(models: dict[int, Model], news_id: int, code: str, feats: dict, now: str) -> list[tuple]:
    out = []
    for h, m in sorted(models.items()):
        lab, p = m.predict(feats)
        out.append((news_id, code, h, m.version, lab, p.get("up", 0.0), p.get("flat", 0.0), p.get("down", 0.0),
                    json.dumps(feats, ensure_ascii=False), now))
    return out


INSERT = ("INSERT INTO {t} (news_id, stock_code, horizon, model_version, label, p_up, p_flat, p_down, features_json, "
          "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


def create_for_news(conn: sqlite3.Connection, news_id: int, models: dict[int, Model] | None = None,
                    an: Analyzer | None = None) -> int:
    """建立並鎖定這則新聞的機器學習預測（最新版模型），回傳筆數；沒有模型或已經有預測就不做。"""
    if conn.execute("SELECT 1 FROM ml_predictions WHERE news_id = ? LIMIT 1", (news_id,)).fetchone():
        return 0
    models = models if models is not None else load(conn)
    if not models:
        return 0
    n = conn.execute("SELECT title, body, published_at, fetched_at FROM news WHERE id = ?", (news_id,)).fetchone()
    if n is None:
        raise KeyError(f"沒有 id={news_id} 的新聞")
    now = config.now_iso()
    days = [r[0] for r in conn.execute("SELECT date FROM trading_days ORDER BY date")]
    key = _entry_key(days, n["published_at"] or n["fetched_at"], n["fetched_at"], now)
    rows = []
    for code, f in news_features(an or analyzer(conn), n["title"], n["body"] or "").items():
        prior = conn.execute(
            "SELECT COUNT(DISTINCT news_id) FROM ml_predictions WHERE stock_code = ? "
            "AND json_extract(features_json, '$.entry') = ?", (code, key)).fetchone()[0]
        feats = {**f, "log_news": math.log1p(prior + 1), "news": prior + 1, "entry": key}
        rows += _rows(models, news_id, code, feats, now)
    with conn:
        conn.executemany(INSERT.format(t="ml_predictions"), rows)
    return len(rows)


def recalc(conn: sqlite3.Connection, version: int | None = None, log=print) -> int:
    """用指定（預設最新）版本的模型重算網站上所有新聞，存進 ml_recalcs（覆寫同版本上次的重算）。回傳筆數。"""
    models = load(conn, version)
    if not models:
        raise RuntimeError("還沒有訓練好的模型，請先執行 python -m radar.mlmodel train")
    v = next(iter(models.values())).version
    an = analyzer(conn)
    days = [r[0] for r in conn.execute("SELECT date FROM trading_days ORDER BY date")]
    news = conn.execute("SELECT n.id, n.title, n.body, COALESCE(n.published_at, n.fetched_at) AS t, n.fetched_at, "
                        "p.created_at FROM news n LEFT JOIN predictions p ON p.news_id = n.id").fetchall()
    # 依「系統第一次能用」的時間排序，報導量只數之前（含自己）的新聞
    news = sorted(news, key=lambda r: (targets.available_at(r["t"], r["fetched_at"], r["created_at"]), r["id"]))
    seen: dict[tuple[str, str], int] = {}
    now = config.now_iso()
    rows = []
    for n in news:
        key = _entry_key(days, n["t"], n["fetched_at"], n["created_at"])
        for code, f in news_features(an, n["title"], n["body"] or "").items():
            seen[(code, key)] = seen.get((code, key), 0) + 1
            k = seen[(code, key)]
            rows += _rows(models, n["id"], code, {**f, "log_news": math.log1p(k), "news": k, "entry": key}, now)
    with conn:
        conn.execute("DELETE FROM ml_recalcs WHERE model_version = ?", (v,))
        conn.executemany(INSERT.format(t="ml_recalcs"), rows)
    log(f"已用模型第 {v} 版重算 {len(news)} 則新聞，{len(rows)} 筆（個股 × 期間）")
    return len(rows)


def preview(conn: sqlite3.Connection, title: str, body: str) -> dict[str, list[dict]]:
    """貼連結分析用：用最新模型預測這段文字（不存檔）。報導量當作 1 則。{期間: [個股預測]}；沒有模型回傳 {}。"""
    models = load(conn)
    if not models:
        return {}
    feats = news_features(analyzer(conn), title, body)
    names = {r["code"]: r["name"] for r in conn.execute(
        f"SELECT code, name FROM stocks WHERE code IN ({','.join('?' * len(feats))})", list(feats))} if feats else {}
    out: dict[str, list[dict]] = {}
    for h, m in sorted(models.items()):
        for code, f in feats.items():
            x = {**f, "log_news": math.log1p(1), "news": 1}
            raw, p = m.predict(x)
            out.setdefault(str(h), []).append({
                "code": code, "name": names.get(code, code), "label": decide(p.get("up", 0.0), p.get("down", 0.0)),
                "raw_label": raw, "p_up": p.get("up", 0.0),
                "p_flat": p.get("flat", 0.0), "p_down": p.get("down", 0.0), "model_version": m.version, "features": x})
    return out


def summary(conn: sqlite3.Connection) -> list[dict]:
    """各版本、各期間的模型資訊（網站與 status 用）。"""
    out = []
    for r in conn.execute("SELECT version, horizon, created_at, train_first, train_last, n_train, eval_json, "
                          "target_version, params_json FROM ml_models ORDER BY version DESC, horizon"):
        out.append({"version": r["version"], "horizon": r["horizon"], "created_at": r["created_at"],
                    "train_first": r["train_first"], "train_last": r["train_last"], "n_train": r["n_train"],
                    "target_version": r["target_version"], "C": json.loads(r["params_json"])["C"],
                    "eval": json.loads(r["eval_json"]) if r["eval_json"] else None})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="網站的機器學習預測：訓練、重算、查看")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train", help="訓練新版本（每個觀察期間一個）")
    t.add_argument("--no-eval", action="store_true", help="不跑逐月 walk-forward 評估（快很多，但網站沒有樣本外指標）")
    sub.add_parser("recalc", help="用最新模型重算網站上所有新聞（存成重算結果，不改原始預測）")
    sub.add_parser("status", help="列出各版本與樣本外指標")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    try:
        import sklearn  # noqa: F401
        import scipy  # noqa: F401
    except ImportError:
        print("需要先安裝機器學習套件：pip install -r requirements-ml.txt")
        return 1
    conn = db.connect(args.db)
    if args.cmd == "train":
        v = train(conn, evaluate=not args.no_eval)
        print(f"已建立模型第 {v} 版。")
    elif args.cmd == "recalc":
        try:
            recalc(conn)
        except RuntimeError as e:
            print(e)
            return 1
        return 0
    pc = lambda x, f="{:.1f}%": "—" if x is None else f.format(x * 100)
    rows = summary(conn)
    if not rows:
        print("還沒有訓練好的模型。")
    for r in rows:
        e = r["eval"] or {}
        print(f"第 {r['version']} 版 第 {r['horizon']:>2} 日　訓練 {r['n_train']} 筆 {r['train_first']}～{r['train_last']}　C＝{r['C']}"
              + (f"　樣本外 IC {e['ic']:+.3f}（p {e['ic_p']:.3f}）　balanced {pc(e['balanced_accuracy'])}"
                 f"　命中率 {pc(e['hit_rate'])}（全猜跌 {pc(e['all_down_hit_rate'])}）"
                 f"　報酬差 {pc(e['spread'], '{:+.2f}%')}，扣 0.585% 後 {pc(e['net'].get('0.00585'), '{:+.2f}%')}"
                 if e else "　（沒有評估）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
