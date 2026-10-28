"""預測驗證（回測）：新聞發布後第 N 個交易日，用真實收盤價檢查預測準不準。

規則（PROJECT_BRIEF.md 第 6.2 節，與原型 evaluate() 相同）：
- 基準價：13:30 收盤前發布 → 前一個交易日收盤；收盤後發布 → 當日收盤（當日休市則為之前最後一個交易日）。
  沒有發布時間的新聞視為盤前。交易日以證交所實際開市日為準（trading_days 資料表）。
- 觀察期間：第 1、3、5、10 個交易日。
- 比較基準：預設扣除大盤（個股報酬 − 加權指數報酬），可改成個股漲跌。
- 實際方向：報酬 > 門檻為漲、< −門檻為跌，其餘持平；門檻預設 1%。
- 命中：預測利多且實際漲，或預測利空且實際跌。中性預測不計入命中率。

執行：python -m radar.validate                     （3 日、扣除大盤、門檻 1%）
      python -m radar.validate --horizon 5 --raw --threshold 2 --direct
"""
from __future__ import annotations

import argparse
import bisect
import sqlite3
import sys
from dataclasses import asdict, dataclass

from . import db
from .analysis.engine import strength

HORIZONS = (1, 3, 5, 10)
CLOSE_TIME = "13:30"
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

    @property
    def strength(self) -> str:
        return strength(self.score)


def _split_time(iso: str | None) -> tuple[str, str]:
    iso = iso or ""
    return iso[:10], (iso[11:16] if len(iso) >= 16 else "")


def base_index(days: list[str], day: str, time: str) -> int | None:
    """基準價是第幾個交易日；None＝當天的收盤資料還沒出來，-1＝新聞早於價格資料。"""
    if not days or day > days[-1]:
        return None
    after = (time or "08:00") >= CLOSE_TIME
    i = bisect.bisect_left(days, day) - 1  # 最後一個早於新聞日期的交易日
    if after and i + 1 < len(days) and days[i + 1] == day:
        i += 1
    return i


def load_rows(conn: sqlite3.Connection, lexicon_version: int | None = None) -> list[Row]:
    """讀出每一檔個股的預測。lexicon_version 為 None＝原始（鎖定）預測，否則為該版詞典的重算結果。"""
    if lexicon_version is None:
        sql = """SELECT n.id, n.title, n.url, n.source, n.via_outlet, COALESCE(n.published_at, n.fetched_at) AS t,
                        ps.stock_code, ps.score, ps.label, ps.type, ps.sector, s.name
                 FROM predictions p JOIN prediction_stocks ps ON ps.prediction_id = p.id
                 JOIN news n ON n.id = p.news_id LEFT JOIN stocks s ON s.code = ps.stock_code"""
        args: tuple = ()
    else:
        sql = """SELECT n.id, n.title, n.url, n.source, n.via_outlet, COALESCE(n.published_at, n.fetched_at) AS t,
                        rs.stock_code, rs.score, rs.label, rs.type, rs.sector, s.name
                 FROM recalc_stocks rs JOIN news n ON n.id = rs.news_id
                 LEFT JOIN stocks s ON s.code = rs.stock_code WHERE rs.lexicon_version = ?"""
        args = (lexicon_version,)
    out = []
    for r in conn.execute(sql, args):
        d, t = _split_time(r["t"])
        out.append(Row(r["id"], d, t, r["title"], r["url"], r["source"], r["via_outlet"], r["stock_code"],
                       r["name"] or r["stock_code"], r["type"], r["sector"], r["score"], PRED[r["label"]]))
    return out


def _prices(conn: sqlite3.Connection, codes: set[str]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    codes = codes | {TAIEX}
    q = f"SELECT code, date, close FROM prices WHERE code IN ({','.join('?' * len(codes))})"
    for r in conn.execute(q, tuple(codes)):
        out.setdefault(r["code"], {})[r["date"]] = r["close"]
    return out


def _avg(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _rate(rows: list[Row]) -> float | None:
    return sum(r.hit for r in rows) / len(rows) if rows else None


def validate(conn: sqlite3.Connection, horizon: int = 3, benchmark: str = "excess", threshold: float = 0.01,
             scope: str = "all", lexicon_version: int | None = None) -> dict:
    if horizon < 1:
        raise ValueError("觀察期間至少 1 個交易日")
    if benchmark not in ("excess", "raw") or scope not in ("all", "direct"):
        raise ValueError("benchmark 只能是 excess／raw，scope 只能是 all／direct")
    days = [r[0] for r in conn.execute("SELECT date FROM trading_days ORDER BY date")]
    rows = [r for r in load_rows(conn, lexicon_version) if scope == "all" or r.type == "direct"]
    px = _prices(conn, {r.code for r in rows})
    market = px.get(TAIEX, {})
    use_excess = benchmark == "excess" and bool(market)
    last = len(days) - 1
    base_of: dict[int, int | None] = {}

    for r in rows:
        bi = base_index(days, r.date, r.time)
        base_of[id(r)] = bi
        ser = px.get(r.code, {})
        if bi is None:
            r.status = "wait_base"
            continue
        if bi < 0 or ser.get(days[bi]) is None:
            r.status = "nodata"
            continue
        r.base_date, r.p0 = days[bi], ser[days[bi]]
        if bi + horizon > last:
            r.status, r.left = "wait", bi + horizon - last
            continue
        r.end_date, r.p1 = days[bi + horizon], ser.get(days[bi + horizon])
        if r.p1 is None:
            r.status = "nodata"
            continue
        r.ret = r.p1 / r.p0 - 1
        m0, m1 = market.get(r.base_date), market.get(r.end_date)
        r.ex = r.ret - (m1 / m0 - 1) if use_excess and m0 and m1 else r.ret
        r.act = "up" if r.ex > threshold else "down" if r.ex < -threshold else "flat"
        r.hit = (r.pred == r.act) if r.pred != "flat" else None

    done = [r for r in rows if r.status == "done" and r.pred != "flat"]
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
                x = ser[days[i]] / r.p0 - 1
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
                   "scope": scope, "lexicon_version": lexicon_version},
        "notes": ([] if use_excess or benchmark == "raw" else ["價格資料沒有大盤（TAIEX），已改用個股漲跌。"]),
        "data_range": {"first": days[0] if days else None, "last": days[-1] if days else None},
        "summary": {
            "hit_rate": _rate(done), "hits": sum(r.hit for r in done), "n": len(done),
            "up_hit_rate": _rate(ups), "up_n": len(ups), "up_avg": avg_up,
            "down_hit_rate": _rate(downs), "down_n": len(downs), "down_avg": avg_down,
            "spread": avg_up - avg_down if avg_up is not None and avg_down is not None else None,
            "waiting": sum(r.status in ("wait", "wait_base") for r in rows),
            "nodata": sum(r.status == "nodata" for r in rows),
        },
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
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    res = validate(conn, args.horizon, "raw" if args.raw else "excess", args.threshold / 100,
                   "direct" if args.direct else "all", args.recalc)
    s, rng = res["summary"], res["data_range"]
    fr = lambda x: "—" if x is None else f"{x * 100:.0f}%"
    print(f"價格資料 {rng['first']} ～ {rng['last']}；觀察第 {args.horizon} 個交易日、"
          f"{'扣除大盤' if res['params']['benchmark'] == 'excess' else '個股漲跌'}、門檻 ±{args.threshold}%")
    for n in res["notes"]:
        print("注意：" + n)
    print(f"整體命中率 {fr(s['hit_rate'])}（{s['hits']}/{s['n']}）　利多 {fr(s['up_hit_rate'])}（{s['up_n']} 筆，平均 {_pct(s['up_avg'])}）"
          f"　利空 {fr(s['down_hit_rate'])}（{s['down_n']} 筆，平均 {_pct(s['down_avg'])}）")
    print(f"多空報酬差 {_pct(s['spread'])}　待驗證 {s['waiting']} 筆　缺價格 {s['nodata']} 筆")
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
