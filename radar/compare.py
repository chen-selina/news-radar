"""字典比較：同一批新聞、同一套歸屬規則、同一份收盤價，只換情緒字典，看哪一部比較準。

三個版本：
- 手工詞典：config/lexicon-handmade.json，每個詞有人工給的強度分數（−2.5～+2）。
- 論文字典：台灣財經情緒字典（黃裕烈，2025），每個詞 ±1，沒有強度。
- 論文＋個股擴充：論文字典，加上手工詞典裡的個股事件詞（營收成長、漲停、賣超…），一律 ±1。

「哪一句話算給哪一家公司」「產業連動」「否定反轉」等規則三個版本完全相同（production_analyzer），
差別只在字典。驗證方式與 python -m radar.validate --dedupe 相同。這個指令只讀資料庫，不會寫入任何東西。

執行：python -m radar.compare                （只看直接提及的個股、扣除大盤、門檻 1%）
      python -m radar.compare --all --raw --threshold 2
      python -m radar.compare --per-news       （每則新聞各算一筆；預設一檔個股一天只算一筆）
      python -m radar.compare --backfill       （改用回補的歷史新聞）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from . import backfill, db
from .analysis import lexicon, remi
from .analysis.engine import label_of, production_analyzer
from .stocks.query import all_stocks
from .validate import HORIZONS, PRED, Row, _split_time, validate

VARIANTS = ("手工詞典", "論文字典", "論文＋個股擴充")


def lexicons(base: lexicon.Lexicon) -> dict[str, lexicon.Lexicon]:
    return {"手工詞典": base, "論文字典": remi.as_lexicon(base), "論文＋個股擴充": remi.as_lexicon(base, extend=True)}


NEWS_SQL = {
    # 網站平常收錄的新聞
    "live": "SELECT id, title, body, url, source, via_outlet, COALESCE(published_at, fetched_at) AS t FROM news ORDER BY id",
    # 回補的歷史新聞（radar/backfill.py）：只用讀到內文的。標題用查網址時拿到的（有些網站的網頁標題其實是分類名稱）
    "backfill": "SELECT id, CASE WHEN list_title != '' THEN list_title ELSE title END AS title, body, url, "
                "'backfill' AS source, outlet AS via_outlet, COALESCE(published_at, listed_at) AS t "
                "FROM backfill_news WHERE status = 'ok' ORDER BY id",
}


def predict_rows(conn: sqlite3.Connection, lex: lexicon.Lexicon, data: str = "live") -> list[Row]:
    """用這部字典分析資料庫裡的新聞，回傳每一檔個股的預測（不存檔）。data：live＝網站收錄的，backfill＝回補的。"""
    an = production_analyzer(lex, all_stocks(conn))
    out = []
    for n in conn.execute(NEWS_SQL[data]):
        d, t = _split_time(n["t"])
        body = n["body"] or ""
        if data == "backfill":
            body = backfill.clean_body(body)   # 去掉內文後面夾帶的其他新聞標題與廣告
        for v in an.analyze(n["title"], body).impacts:
            out.append(Row(n["id"], d, t, n["title"], n["url"], n["source"], n["via_outlet"], v.stock.code,
                           v.stock.name, v.type, v.sector, v.score, PRED[label_of(v.score)]))
    return out


def compare(conn: sqlite3.Connection, base: lexicon.Lexicon, benchmark: str = "excess",
            threshold: float = 0.01, scope: str = "direct", dedupe: bool = True,
            data: str = "live") -> list[dict]:
    """每個版本、每個觀察期間一筆：筆數、命中率、利多／利空之後的平均報酬、多空報酬差。"""
    out = []
    for name, lex in lexicons(base).items():
        rows = predict_rows(conn, lex, data)
        for h in HORIZONS:
            res = validate(conn, h, benchmark, threshold, scope, rows=[Row(**vars(r)) for r in rows], dedupe=dedupe)
            s = res["summary"]
            done = [r for r in res["rows"] if r["status"] == "done"]
            out.append({"variant": name, "horizon": h, "terms": sum(w != 0 for w in lex.sentiment.values()),
                        "predictions": len(res["rows"]), "neutral": sum(r["pred"] == "flat" for r in res["rows"]),
                        "n": s["n"], "hits": s["hits"], "hit_rate": s["hit_rate"],
                        "up_n": s["up_n"], "up_avg": s["up_avg"], "down_n": s["down_n"], "down_avg": s["down_avg"],
                        "spread": s["spread"],
                        # 對照用：這些已驗證的個股裡，實際漲／跌的比例（亂猜的命中率大約就是這個數字）
                        "actual_up": _share(done, "up"), "actual_down": _share(done, "down")})
    return out


def _share(done: list[dict], act: str) -> float | None:
    return sum(r["act"] == act for r in done) / len(done) if done else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="比較三部情緒字典的預測準確度")
    ap.add_argument("--all", action="store_true", help="連產業連動的個股一起算（預設只看直接提及）")
    ap.add_argument("--raw", action="store_true", help="用個股漲跌（預設扣除大盤）")
    ap.add_argument("--threshold", type=float, default=1.0, help="持平門檻，單位 %%（預設 1）")
    ap.add_argument("--per-news", action="store_true", help="每則新聞各算一筆（預設一檔個股一天只算一筆，同事件不重複計算）")
    ap.add_argument("--backfill", action="store_true", help="改用回補的歷史新聞（python -m radar.backfill）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    data = "backfill" if args.backfill else "live"
    res = compare(conn, lexicon.load_file(lexicon.HANDMADE_PATH), "raw" if args.raw else "excess", args.threshold / 100,
                  "all" if args.all else "direct", dedupe=not args.per_news, data=data)
    pct = lambda x, f="{:.0f}%": "—" if x is None else f.format(x * 100)
    n_news = conn.execute(f"SELECT COUNT(*) FROM ({NEWS_SQL[data]})").fetchone()[0]
    print(f"{'回補的歷史新聞' if args.backfill else '新聞'} {n_news} 則；{'全部個股' if args.all else '只看直接提及的個股'}、"
          f"{'個股漲跌' if args.raw else '扣除大盤'}、門檻 ±{args.threshold}%、"
          f"{'每則新聞各算一筆' if args.per_news else '一檔個股一天算一筆'}")
    for name in VARIANTS:
        rows = [r for r in res if r["variant"] == name]
        print(f"\n{name}（{rows[0]['terms']} 個詞）：預測 {rows[0]['predictions']} 筆，其中中性 {rows[0]['neutral']} 筆")
        for r in rows:
            print(f"  第 {r['horizon']:>2} 日　命中率 {pct(r['hit_rate']):>4}（{r['hits']}/{r['n']}）"
                  f"　利多 {r['up_n']} 筆平均 {pct(r['up_avg'], '{:+.1f}%')}　利空 {r['down_n']} 筆平均 {pct(r['down_avg'], '{:+.1f}%')}"
                  f"　多空報酬差 {pct(r['spread'], '{:+.1f}%')}　〔實際漲 {pct(r['actual_up'])}、跌 {pct(r['actual_down'])}〕")
    small = max((r["n"] for r in res), default=0) < 300
    if small:
        print("\n注意：已驗證的筆數還很少，上面的數字差距可能只是運氣，不能當成哪一部字典比較好的結論。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
