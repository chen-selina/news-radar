"""在命令列分析一則新聞，方便調整詞典時快速檢查。

執行：python -m radar.analysis "標題" "內文"
"""
from __future__ import annotations

import sys

from .. import db
from ..stocks.query import all_stocks
from . import lexicon
from .engine import LABELS, production_analyzer, strength


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    title, body = argv[0], " ".join(argv[1:])
    conn = db.connect()
    stocks = all_stocks(conn)
    if not stocks:
        print("資料庫沒有公司資料，請先執行 python -m radar.stocks.load")
        return 1
    lex = lexicon.current(conn)
    a = production_analyzer(lex, stocks).analyze(title, body)

    print(f"{LABELS[a.label]}  {a.total:+.2f}  影響程度{a.strength}  （詞典第 {lex.version} 版）")
    print("命中詞：" + ("、".join(f"{h.term} {h.w:+.2f}" + (f"（{h.note}）" if h.note else "") for h in a.hits) or "無"))
    for v in a.impacts:
        print(f"  {v.stock.code} {v.stock.name:<6} {v.score:+.2f} {strength(v.score)}  {v.reason}")
    if not a.impacts:
        print("  沒有比對到上市櫃公司或產業關鍵字。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
