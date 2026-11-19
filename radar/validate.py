"""預測驗證（回測）：新聞發布後第 N 個交易日，用真實收盤價檢查預測準不準。

規則（PROJECT_BRIEF.md 第 6.2 節，與原型 evaluate() 相同）：
- 基準價（2026-11-19 起，entry="close"，規則在 radar/targets.py 的 entry_index）：資訊可取得的時間在交易日
  13:00 以前 → 當日收盤；13:00 以後、休市日或沒有時間 → 下一個交易日收盤。起點一定是資訊出來以後買得到的價格。
  網站收錄的新聞以系統第一次看到的時間為準（發布與抓取取較晚的；原始預測用預測建立時間），回補新聞用發布時間。
  舊規則（entry="legacy"，--legacy-entry）：09:00 開盤前發布 → 前一個交易日收盤；開盤後發布（盤中與盤後）→ 當日收盤；
  沒有發布時間的新聞視為盤前。開盤前與盤後的新聞會把資訊出來以前的價格當起點，只留著和舊結果比較。
  交易日以證交所實際開市日為準（trading_days 資料表）。
  2026-11-13 以前的規則以 13:30 收盤為界（與原型相同），盤中新聞的基準價是前一日收盤，於是 10 點的
  「某某亮燈漲停」會被算成「預測利多、當天果然漲」，其實只是在報導已經發生的漲跌。回補資料實測，盤中新聞的
  第 1 日命中率 57%、盤前盤後只有 41%。只有每日收盤價時，盤中才出來的消息從當日收盤後開始算。
- 觀察期間：第 1、3、5、10 個交易日。
- 比較基準：預設扣除大盤（個股報酬 − 加權指數報酬），可改成個股漲跌。
- 實際方向：報酬 > 門檻為漲、< −門檻為跌，其餘持平；門檻預設 1%。
- 命中：預測利多且實際漲，或預測利空且實際跌。中性預測不計入命中率；實際持平留在分母（算沒命中）。
  標籤與指標的定義在 radar/targets.py、radar/metrics.py，和 radar/scoring.py、radar/train.py 共用。
- 計算單位（dedupe）：預設每則新聞的每檔個股各算一筆。dedupe=True 時改成「一檔個股、一個基準交易日」算一筆：
  同一事件的多篇報導（radar/dedupe.py）只取分數最大的一篇，同一天的不同事件取平均。
  同一件事被 15 家媒體報導，就不會被當成 15 次獨立的命中或未命中。

執行：python -m radar.validate                     （3 日、扣除大盤、門檻 1%）
      python -m radar.validate --horizon 5 --raw --threshold 2 --direct
      python -m radar.validate --dedupe             （一檔個股一天只算一筆）
"""
from __future__ import annotations

import argparse
import bisect
import sqlite3
import sys
from dataclasses import asdict, dataclass, replace

from . import db, metrics, targets
from .analysis.engine import label_of, strength
from .dedupe import event_groups

HORIZONS = (1, 3, 5, 10)
OPEN_TIME = "09:00"   # 這個時間（含）之後發布的新聞，基準價用當日收盤
EVENT_RANGE = range(-3, 11)  # 事件研究圖：新聞前 3 日到後 10 日
TAIEX = "TAIEX"
PRED = {"pos": "up", "neg": "down", "neu": "flat"}


@dataclass
class Row:
    news_id: int
    date: str
    time: str
    title: str
    url: str
    source: str
    via_outlet: str | None
    code: str
    name: str
    type: str
    sector: str | None
    score: float
    pred: str                    # up／down／flat
    status: str = "done"         # done；wait＝還沒滿 N 日；wait_base＝基準日的收盤價還沒出來；nodata＝缺價格
    left: int | None = None      # 還差幾個交易日
    base_date: str | None = None
    end_date: str | None = None
    p0: float | None = None
    p1: float | None = None
    ret: float | None = None     # 個股報酬
    ex: float | None = None      # 用來判斷方向的報酬（扣除大盤或個股本身）
    act: str | None = None       # up／down／flat
    hit: bool | None = None      # 中性預測為 None
    merged: int = 1              # dedupe 時這一筆合併了幾則新聞
    avail_date: str | None = None  # 資訊可取得的日期、時間（台北）；None＝用 date／time（發布時間）
    avail_time: str | None = None
    strength_label: str | None = None  # 機器學習的影響程度（依機率差分級）；None＝依字典分數

    def when(self) -> tuple[str, str]:
        """決定進場日用的（日期, 時間）。"""
        return (self.avail_date, self.avail_time or "") if self.avail_date else (self.date, self.time)

    @property
    def strength(self) -> str:
        return self.strength_label or strength(self.score)


def _split_time(iso: str | None) -> tuple[str, str]:
    """ISO 時間 → 台北時間的（日期, HH:MM）；其他時區會換算（radar/targets.py 的 local_time）。"""
    d, t, _ = targets.local_time(iso)
    return d, t


def base_index(days: list[str], day: str, time: str, rule: str = "legacy") -> int | None:
    """基準價是第幾個交易日；None＝當天的收盤資料還沒出來，-1＝新聞早於價格資料。

    預設是舊規則（legacy），留著給舊程式與比較用；新的可成交規則是 targets.entry_index(…, "close")。
    """
    return targets.entry_index(days, day, time, rule)


def ml_strength(score: float) -> str:
    """機器學習預測的影響程度：上漲機率 − 下跌機率的絕對值。低＝不到判斷門檻（顯示為中性）、中＝5～10 個百分點、高＝10 以上。"""
    a = abs(score)
    return "高" if a >= 0.10 else "中" if a >= 0.05 else "低"


def load_ml_rows(conn: sqlite3.Connection, horizon: int, version: int | None = None) -> list[Row]:
    """機器學習預測（radar/mlmodel.py）：version 為 None＝鎖定的原始預測，否則為該版模型的重算結果。只讀這個期間的模型。
    方向用 mlmodel.decide()：上漲機率 − 下跌機率要達門檻（MARGIN）才算利多／利空，其餘中性。"""
    from .mlmodel import decide
    table, extra, args = ("ml_predictions", "", (horizon,)) if version is None else         ("ml_recalcs", " AND m.model_version = ?", (horizon, version))
    created = "m.created_at" if version is None else "NULL"
    sql = f"""SELECT n.id, n.title, n.url, n.source, n.via_outlet, COALESCE(n.published_at, n.fetched_at) AS t,
                     n.fetched_at AS fetched, {created} AS created, m.stock_code, m.p_up - m.p_down AS score,
                     m.p_up, m.p_down, s.name
              FROM {table} m JOIN news n ON n.id = m.news_id LEFT JOIN stocks s ON s.code = m.stock_code
              WHERE m.horizon = ?{extra}"""
    out = []
    for r in conn.execute(sql, args):
        d, t = _split_time(r["t"])
        ad, at = targets.available_at(r["t"], r["fetched"], r["created"])
        out.append(Row(r["id"], d, t, r["title"], r["url"], r["source"], r["via_outlet"], r["stock_code"],
                       r["name"] or r["stock_code"], "direct", None, r["score"], PRED[decide(r["p_up"], r["p_down"])],
                       avail_date=ad or None, avail_time=at, strength_label=ml_strength(r["score"])))
    return out


def load_rows(conn: sqlite3.Connection, lexicon_version: int | None = None) -> list[Row]:
    """讀出每一檔個股的預測。lexicon_version 為 None＝原始（鎖定）預測，否則為該版詞典的重算結果。"""
    if lexicon_version is None:
        sql = """SELECT n.id, n.title, n.url, n.source, n.via_outlet, COALESCE(n.published_at, n.fetched_at) AS t,
                        n.fetched_at AS fetched, p.created_at AS created,
                        ps.stock_code, ps.score, ps.label, ps.type, ps.sector, s.name
                 FROM predictions p JOIN prediction_stocks ps ON ps.prediction_id = p.id
                 JOIN news n ON n.id = p.news_id LEFT JOIN stocks s ON s.code = ps.stock_code"""
        args: tuple = ()
    else:
        sql = """SELECT n.id, n.title, n.url, n.source, n.via_outlet, COALESCE(n.published_at, n.fetched_at) AS t,
                        n.fetched_at AS fetched, NULL AS created,
                        rs.stock_code, rs.score, rs.label, rs.type, rs.sector, s.name
                 FROM recalc_stocks rs JOIN news n ON n.id = rs.news_id
                 LEFT JOIN stocks s ON s.code = rs.stock_code WHERE rs.lexicon_version = ?"""
        args = (lexicon_version,)
    out = []
    for r in conn.execute(sql, args):
        d, t = _split_time(r["t"])
        ad, at = targets.available_at(r["t"], r["fetched"], r["created"])   # 系統第一次能用的時間
        out.append(Row(r["id"], d, t, r["title"], r["url"], r["source"], r["via_outlet"], r["stock_code"],
                       r["name"] or r["stock_code"], r["type"], r["sector"], r["score"], PRED[r["label"]],
                       avail_date=ad or None, avail_time=at))
    return out


def collapse(rows: list[Row], days: list[str], entry: str = targets.ENTRY_RULE) -> list[Row]:
    """一檔個股、一個基準交易日只留一筆（同一天 13:00 前後的新聞基準日不同，分開算）。

    1. 這檔個股當天有被直接提及，就不看產業連動的那幾筆。
    2. 同一事件的多篇報導只取分數絕對值最大的一篇。
    3. 不同事件的分數取平均，再依平均分數決定利多／利空／中性。
    留下來的那一筆顯示分數絕對值最大的新聞，merged 記錄合併了幾則。
    """
    event = event_groups(list({r.news_id: (r.news_id, r.title, f"{r.date}T{r.time or '00:00'}:00") for r in rows}.values()))
    groups: dict[tuple, list[Row]] = {}
    for r in rows:
        bi = targets.entry_index(days, *r.when(), entry)
        day = days[bi] if bi is not None and bi >= 0 else "?" + r.date
        groups.setdefault((r.code, day), []).append(r)
    out = []
    for rs in groups.values():
        use = [r for r in rs if r.type == "direct"] or rs
        best: dict[int, Row] = {}
        for r in use:
            e = event[r.news_id]
            if e not in best or abs(r.score) > abs(best[e].score):
                best[e] = r
        score = sum(r.score for r in best.values()) / len(best)
        top = max(best.values(), key=lambda r: abs(r.score))
        if top.strength_label is not None:
            # 機器學習：分數是機率差（上漲 − 下跌），不能套字典的 ±1 門檻；平均後同樣用 mlmodel.MARGIN 判斷方向
            from .mlmodel import MARGIN
            pred = "up" if score >= MARGIN - 1e-12 else "down" if score <= -MARGIN + 1e-12 else "flat"
            out.append(replace(top, score=score, pred=pred, strength_label=ml_strength(score), merged=len(rs)))
        else:
            out.append(replace(top, score=score, pred=PRED[label_of(score)], merged=len(rs)))
    return out


def _prices(conn: sqlite3.Connection, codes: set[str], adjusted: bool = True) -> dict[str, dict[str, float]]:
    """{代號: {日期: 收盤}}。adjusted＝True（預設）時乘上 price_adj 的還原係數（radar/prices/adjust.py），
    報酬才不會把除權息算成下跌；沒有係數的個股維持原始收盤。網站顯示價格用 adjusted＝False。"""
    out: dict[str, dict[str, float]] = {}
    codes = codes | {TAIEX}
    marks = ','.join('?' * len(codes))
    for r in conn.execute(f"SELECT code, date, close FROM prices WHERE code IN ({marks})", tuple(codes)):
        out.setdefault(r["code"], {})[r["date"]] = r["close"]
    if not adjusted:
        return out
    adj: dict[str, list[tuple[str, float]]] = {}
    for r in conn.execute(f"SELECT code, date, factor FROM price_adj WHERE code IN ({marks}) ORDER BY code, date",
                          tuple(codes)):
        adj.setdefault(r["code"], []).append((r["date"], r["factor"]))
    for code, fs in adj.items():
        dates = [d for d, _ in fs]
        ser = out.get(code, {})
        for d in ser:
            i = bisect.bisect_right(dates, d) - 1      # 最近一個不晚於 d 的係數；比第一個還早就用第一個
            ser[d] *= fs[max(i, 0)][1]
    return out


def _avg(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _rate(rows: list[Row]) -> float | None:
    return sum(r.hit for r in rows) / len(rows) if rows else None


def validate(conn: sqlite3.Connection, horizon: int = 3, benchmark: str = "excess", threshold: float = 0.01,
             scope: str = "all", lexicon_version: int | None = None, rows: list[Row] | None = None, dedupe: bool = False,
             entry: str = targets.ENTRY_RULE, predictor: str = "lexicon", ml_version: int | None = None) -> dict:
    """predictor：lexicon＝字典規則的預測（lexicon_version 指定重算版本）；ml＝機器學習預測（ml_version 指定重算版本），
    只讀這個觀察期間的模型。
    rows：直接給預測（radar/compare.py 比較不同字典時用），不從資料庫讀；會被就地填入驗證結果。

    entry：進場規則（radar/targets.py）。close＝可成交（預設）；legacy＝舊規則，只供比較；given＝row.date 就是起點交易日。
    """
    if entry not in targets.ENTRY_RULES:
        raise ValueError(f"entry 只能是 {'／'.join(targets.ENTRY_RULES)}")
    spec = targets.LabelSpec(horizon, benchmark, threshold)   # 檢查參數；規則見 radar/targets.py
    if scope not in ("all", "direct"):
        raise ValueError("scope 只能是 all／direct")
    days = [r[0] for r in conn.execute("SELECT date FROM trading_days ORDER BY date")]
    if predictor not in ("lexicon", "ml"):
        raise ValueError("predictor 只能是 lexicon／ml")
    if rows is None:
        rows = load_ml_rows(conn, horizon, ml_version) if predictor == "ml" else load_rows(conn, lexicon_version)
    rows = [r for r in rows
            if scope == "all" or r.type == "direct"]
    if dedupe:
        rows = collapse(rows, days, entry)
    px = _prices(conn, {r.code for r in rows})                    # 還原收盤：算報酬
    shown = _prices(conn, {r.code for r in rows}, adjusted=False)  # 原始收盤：顯示基準價與第 N 日價格
    market = px.get(TAIEX, {})
    use_excess = benchmark == "excess" and bool(market)
    last = len(days) - 1
    base_of: dict[int, int | None] = {}

    for r in rows:
        bi = targets.entry_index(days, *r.when(), entry)
        base_of[id(r)] = bi
        ser = px.get(r.code, {})
        if bi is None:
            r.status = "wait_base"
            continue
        if bi < 0 or ser.get(days[bi]) is None:
            r.status = "nodata"
            continue
        raw = shown.get(r.code, {})
        r.base_date, r.p0 = days[bi], raw[days[bi]]
        ei = targets.end_index(bi, horizon)
        if ei > last:
            r.status, r.left = "wait", ei - last
            continue
        r.end_date, r.p1 = days[ei], raw.get(days[ei])
        if r.p1 is None:
            r.status = "nodata"
            continue
        r.ret = ser[r.end_date] / ser[r.base_date] - 1      # 報酬用還原收盤；p0、p1 是原始收盤（顯示用）
        m0, m1 = market.get(r.base_date), market.get(r.end_date)
        r.ex = r.ret - (m1 / m0 - 1) if use_excess and m0 and m1 else r.ret
        r.act = targets.direction(r.ex, threshold)
        r.hit = (r.pred == r.act) if r.pred != "flat" else None

    done = [r for r in rows if r.status == "done" and r.pred != "flat"]
    m = metrics.report([r.pred for r in rows], [r.act if r.status == "done" else None for r in rows],
                       [r.status for r in rows])
    ups, downs = [r for r in done if r.pred == "up"], [r for r in done if r.pred == "down"]
    avg_up, avg_down = _avg([r.ex for r in ups]), _avg([r.ex for r in downs])

    def curve(direction: str) -> list[dict]:
        pts = []
        for k in EVENT_RANGE:
            xs = []
            for r in rows:
                bi = base_of[id(r)]
                if r.pred != direction or r.status in ("nodata", "wait_base") or bi is None:
                    continue
                i = bi + k
                ser = px.get(r.code, {})
                if not 0 <= i <= last or ser.get(days[i]) is None:
                    continue
                x = ser[days[i]] / ser[r.base_date] - 1
                if use_excess and market.get(days[i]) and market.get(r.base_date):
                    x -= market[days[i]] / market[r.base_date] - 1
                xs.append(x)
            pts.append({"k": k, "y": _avg(xs), "n": len(xs)})
        return pts

    by_strength = []
    for s in ("低", "中", "高"):
        grp = [r for r in done if r.strength == s]
        by_strength.append({"strength": s, "hit_rate": _rate(grp), "n": len(grp)})

    order = sorted(rows, key=lambda r: (r.date, abs(r.score)), reverse=True)
    return {
        "params": {"horizon": horizon, "benchmark": "excess" if use_excess else "raw", "threshold": threshold,
                   "scope": scope, "lexicon_version": lexicon_version, "dedupe": dedupe, "entry": entry,
                   "predictor": predictor, "ml_version": ml_version,
                   "target_version": targets.LabelSpec(horizon, "excess" if use_excess else "raw", threshold).version},
        "notes": ([] if use_excess or benchmark == "raw" else ["價格資料沒有大盤（TAIEX），已改用個股漲跌。"]),
        "data_range": {"first": days[0] if days else None, "last": days[-1] if days else None},
        "summary": {
            "hit_rate": m["hit_rate"], "hits": m["hits"], "n": m["n_signal"],
            "up_hit_rate": _rate(ups), "up_n": len(ups), "up_avg": avg_up,
            "down_hit_rate": _rate(downs), "down_n": len(downs), "down_avg": avg_down,
            "spread": avg_up - avg_down if avg_up is not None and avg_down is not None else None,
            "waiting": sum(r.status in ("wait", "wait_base") for r in rows),
            "nodata": sum(r.status == "nodata" for r in rows),
        },
        "metrics": m,   # 完整指標（radar/metrics.py）：coverage、三分類、同樣本基準、剔除原因
        "by_strength": by_strength,
        "event_curve": {"up": curve("up"), "down": curve("down")},
        "rows": [{**asdict(r), "strength": r.strength} for r in order],
    }


def _pct(x: float | None, d: int = 1) -> str:
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="預測驗證")
    ap.add_argument("--horizon", type=int, default=3, choices=HORIZONS, help="觀察第幾個交易日")
    ap.add_argument("--raw", action="store_true", help="用個股漲跌（預設扣除大盤）")
    ap.add_argument("--threshold", type=float, default=1.0, help="持平門檻，單位 %%（預設 1）")
    ap.add_argument("--direct", action="store_true", help="只看直接提及的個股")
    ap.add_argument("--recalc", type=int, metavar="版本", help="改用某版詞典的重算結果（預設用原始預測）")
    ap.add_argument("--dedupe", action="store_true", help="一檔個股、一個交易日只算一筆（同事件的多篇報導不重複計算）")
    ap.add_argument("--legacy-entry", action="store_true",
                    help="用舊的基準價規則（09:00 前用前一日收盤），只供和舊結果比較")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    res = validate(conn, args.horizon, "raw" if args.raw else "excess", args.threshold / 100,
                   "direct" if args.direct else "all", args.recalc, dedupe=args.dedupe,
                   entry="legacy" if args.legacy_entry else "close")
    s, rng = res["summary"], res["data_range"]
    fr = lambda x: "—" if x is None else f"{x * 100:.0f}%"
    print(f"價格資料 {rng['first']} ～ {rng['last']}；觀察第 {args.horizon} 個交易日、"
          f"{'扣除大盤' if res['params']['benchmark'] == 'excess' else '個股漲跌'}、門檻 ±{args.threshold}%")
    for n in res["notes"]:
        print("注意：" + n)
    print(f"整體命中率 {fr(s['hit_rate'])}（{s['hits']}/{s['n']}）　利多 {fr(s['up_hit_rate'])}（{s['up_n']} 筆，平均 {_pct(s['up_avg'])}）"
          f"　利空 {fr(s['down_hit_rate'])}（{s['down_n']} 筆，平均 {_pct(s['down_avg'])}）")
    print(f"多空報酬差 {_pct(s['spread'])}　待驗證 {s['waiting']} 筆　缺價格 {s['nodata']} 筆")
    print("指標：" + metrics.brief(res["metrics"]))
    print("影響程度：" + "　".join(f"{b['strength']} {fr(b['hit_rate'])}（{b['n']} 筆）" for b in res["by_strength"]))
    label = {"up": "利多", "down": "利空", "flat": "中性"}
    for r in res["rows"][:30]:
        if r["status"] == "done":
            res_txt = "中性・不計" if r["hit"] is None else ("命中" if r["hit"] else "未命中")
            px = f"{r['p0']} → {r['p1']}  {_pct(r['ex'], 2)}"
        elif r["status"] == "wait":
            res_txt, px = f"等待 {r['left']} 日", f"{r['p0']} → —"
        elif r["status"] == "wait_base":
            res_txt, px = "等待基準價", "—"
        else:
            res_txt, px = "缺價格", "—"
        print(f"  {r['date'][5:]} {r['time'] or '--:--'} {r['code']} {r['name']:<5} {label[r['pred']]} {r['score']:+.1f}"
              f"  {px}  {res_txt}  {r['title'][:24]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
