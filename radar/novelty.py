"""新聞過濾比較：把「沒有新資訊」的新聞拿掉，預測會不會比較準？

兩種沒有新資訊的新聞：
- 行情報導：標題在描述股價已經發生的漲跌（「收盤大漲 3%」「盤中亮燈漲停」）。裡面的「大漲」是在講過去，
  不是對未來的看法。判斷方式是標題有沒有 RECAP 裡的用語，這份清單是自己訂的規則，沒有文獻出處。
- 舊聞：標題和 2～10 天前的某則新聞很像（相似度算法同 radar/dedupe.py），代表同一件事隔幾天又被報一次。
  Tetlock (2011) 發現市場對舊聞仍會反應，但之後會反轉，舊聞的情緒沒有預測力。
  （36 小時內的相似新聞是「同事件的多篇報導」，原本就只算一次，不在這裡處理。）

比較五種過濾：全部新聞、排除行情報導、排除舊聞、兩種都排除、只留行情報導（對照用）。
計分方式取 radar/scoring.py 裡的兩種：現行的「加總，和 0 比」與「只算負面詞，和當天比」。
這個指令只讀資料庫，不會寫入任何東西，也不會改變正式預測。

執行：python -m radar.novelty
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import date, timedelta

from . import db
from .analysis import lexicon, remi
from .compare import NEWS_SQL
from .dedupe import THRESHOLD, bigrams, similarity
from .scoring import evaluate, predict
from .significance import _stars
from .train import build_samples, label
from .validate import HORIZONS, _split_time

RECAP = re.compile("收盤|終場|盤中|開盤|早盤|午盤|尾盤|盤後|開高|開低|收紅|收黑|收漲|收跌|作收|漲停|跌停|亮燈|翻紅|翻黑|"
                   "漲幅|跌幅|漲逾|跌逾|漲近|跌近|大漲|大跌|重挫|飆漲|暴跌|急漲|急跌|勁揚|走高|走低|上漲|下跌|股價")
STALE_MIN_DAYS, STALE_MAX_DAYS = 2, 10
SCORINGS = (("加總，和 0 比（現行）", "加總", "zero"), ("只算負面詞，和當天比", "只算負面詞", "median"))
FILTERS = ("全部新聞", "排除行情報導", "排除舊聞", "兩種都排除", "只留行情報導")


def is_recap(title: str) -> bool:
    return bool(RECAP.search(title or ""))


def stale_ids(news: list[tuple[int, str, str | None]], threshold: float = THRESHOLD) -> set[int]:
    """news: [(新聞 id, 標題, 發布時間)]。回傳標題和 2～10 天前某則新聞很像的新聞 id。沒有日期的不判斷。"""
    by_day: dict[date, list[frozenset[str]]] = {}
    items = []
    for nid, title, t in news:
        try:
            d = date.fromisoformat(_split_time(t)[0])
        except (TypeError, ValueError):
            continue
        g = bigrams(title)
        by_day.setdefault(d, []).append(g)
        items.append((nid, d, g))
    out = set()
    for nid, d, g in items:
        for k in range(STALE_MIN_DAYS, STALE_MAX_DAYS + 1):
            if any(similarity(g, old) >= threshold for old in by_day.get(d - timedelta(days=k), ())):
                out.add(nid)
                break
    return out


def run(conn: sqlite3.Connection, base: lexicon.Lexicon, data: str = "backfill", benchmark: str = "excess") -> dict:
    news = [(n["id"], n["title"], n["t"]) for n in conn.execute(NEWS_SQL[data])]
    ids = {n[0] for n in news}
    recap = {nid for nid, title, _ in news if is_recap(title)}
    stale = stale_ids(news)
    skips = {"全部新聞": set(), "排除行情報導": recap, "排除舊聞": stale, "兩種都排除": recap | stale,
             "只留行情報導": ids - recap}
    cache: dict = {}
    rows = []
    for name in FILTERS:
        samples = build_samples(conn, base, data, lex=remi.as_lexicon(base), skip=skips[name], cache=cache)
        for h in HORIZONS:
            for s in samples:
                s.ex = s.y = None
            label(conn, samples, h, benchmark)
            for label_, score, rule in SCORINGS:
                rows.append({"filter": name, "scoring": label_, "horizon": h, "samples": len(samples),
                             **evaluate(samples, predict(samples, score, rule), h)})
    return {"news": len(news), "recap": len(recap), "stale": len(stale), "both": len(recap & stale), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="比較排除行情報導、舊聞之後的預測準確度")
    ap.add_argument("--live", action="store_true", help="用網站收錄的新聞（預設用回補的歷史新聞）")
    ap.add_argument("--raw", action="store_true", help="用個股漲跌（預設扣除大盤）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    res = run(conn, lexicon.load_file(lexicon.HANDMADE_PATH), "live" if args.live else "backfill",
              "raw" if args.raw else "excess")
    pc = lambda x, f="{:.0f}%": "—" if x is None else f.format(x * 100)
    g = lambda x, f="{:.2f}": "—" if x is None else f.format(x)
    print(f"新聞 {res['news']} 則：行情報導 {res['recap']} 則、舊聞 {res['stale']} 則（兩者皆是 {res['both']} 則）")
    print("論文字典、只看直接提及、一檔個股一天一筆、持平門檻 ±1%；* p<0.10　** p<0.05　*** p<0.01")
    for label_, _, _ in SCORINGS:
        print(f"\n【{label_}】")
        for h in HORIZONS:
            print(f"第 {h} 日")
            for r in (x for x in res["rows"] if x["scoring"] == label_ and x["horizon"] == h):
                t = r["test"]
                print(f"  {r['filter']:<7}　利多 {r['up_n']:>4}／利空 {r['down_n']:>4}　命中率 {pc(r['hit_rate']):>4}"
                      f"（全猜跌 {pc(r['all_down'])}）　利多後 {pc(r['up_avg'], '{:+.2f}%')}　利空後 {pc(r['down_avg'], '{:+.2f}%')}"
                      f"　每日報酬差 {pc(t['mean'], '{:+.2f}%')}（{t['n']} 天，p＝{g(t['p'], '{:.3f}')}）{_stars(t['p'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
