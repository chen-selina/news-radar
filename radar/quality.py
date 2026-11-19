"""新聞資料品質抽查（P0-3）：自動統計＋分層抽樣給人工標註＋標註完成後計分。

三個指令（結果都放在 data/quality/，不進版控）：

  python -m radar.quality report      全部新聞的自動統計 → data_quality_report.csv
  python -m radar.quality sample      分層抽 250 則 → audit_sample.csv（用 Excel／Google 試算表開，填「人工_」開頭的欄位）
  python -m radar.quality score       讀填好的 audit_sample.csv → audit_results.csv（規則分類的 precision／recall 等）

自動欄位（每則新聞）
- 時間：發布時間來源（published_at／只有 listed_at／只有抓取時間／沒有）、發布時段（盤前 <09:00、盤中 09:00～13:30、
  盤後 >13:30、休市日、沒有時間）、網站新聞的抓取延遲（小時）。回補新聞的抓取時間是事後補抓的，不代表當時何時可取得。
- 公司對應：分析引擎直接提及的個股、產業連動的個股數；回補新聞另有 FinMind 標記的個股（backfill_tags），
  「標記個股有被引擎找到」可當作公司對應的自動檢查（只能檢查漏抓，不能檢查錯抓）。
- 規模：主要個股的市值排名（stocks.market_cap，現在的值）：前 50 大、51～300、其他。
- 規則分類（auto_type）：7＝沒有對應到公司；5＝行情報導（radar/novelty.py 的 RECAP 標題規則）；
  6＝重複（同事件裡不是最早的一篇，radar/dedupe.py；或是 2～10 天前的舊聞）；其餘「?」＝1～4 類要人工判斷。

人工欄位（規格書第 3 節的七類）
  人工_新聞類型：1 公司公告／財報／營收／法說　2 訂單、合約、產品、產能、供應鏈　3 產業與總經　4 分析師、法人觀點
                 5 行情回顧（描述已發生的股價、成交量）　6 重複轉載、舊事件延伸　7 對不到公司、資訊不完整或時間不可信
  人工_公司對應：Y＝引擎找到的公司都對、也沒漏　部分＝有對有錯或有漏　N＝都不對
  人工_漏掉的公司：引擎沒找到、但新聞主要在講的公司代號（多個用空白隔開）
  人工_時間可信：Y／N／?（內文或網頁上的時間和 published_at 對不對得上）
  人工_含新資訊：Y＝讀者在發布當下得到市場還不知道的事　N＝重述已知的事或已發生的股價
  標註者、人工_備註

抽樣：依（資料來源、發布時段、是否行情報導、規模）分層，每層至少 2 則、其餘依層大小的平方根分配，
小的層也抽得到。每則有 weight＝層的總數 ÷ 層的抽樣數，score 用它推回全體的比例（同時給未加權的數字）。
固定亂數種子，重跑會抽到同一批。
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import backfill, config, db, targets
from .analysis import lexicon, remi
from .analysis.engine import production_analyzer
from .dedupe import event_groups
from .novelty import is_recap, stale_ids
from .stocks.query import all_stocks

OUT_DIR = config.DATA_DIR / "quality"
TYPES = {"1": "公司公告／財報／營收／法說", "2": "訂單、合約、產品、產能、供應鏈", "3": "產業與總經",
         "4": "分析師、法人觀點", "5": "行情回顧", "6": "重複轉載、舊事件延伸", "7": "對不到公司、不完整、時間不可信"}
HUMAN = ("人工_新聞類型", "人工_公司對應", "人工_漏掉的公司", "人工_時間可信", "人工_含新資訊", "標註者", "人工_備註")
# AI 建議欄：只是給人工核對的初稿，不能直接當標註結果（規格書第 3 節）。時間可信要看原網頁，AI 不填。
AI = ("AI_新聞類型", "AI_公司對應", "AI_漏掉的公司", "AI_含新資訊", "AI_理由", "AI_版本")
SEED = 20261119
EXCERPT = 300


def _bucket(day: str, time: str, trading: set[str]) -> str:
    if not day:
        return "沒有時間"
    if day not in trading:
        return "休市日"
    if not time:
        return "沒有時間"
    return "盤前" if time < "09:00" else "盤中" if time <= "13:30" else "盤後"


def _hours(a: str | None, b: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def _size_ranks(conn: sqlite3.Connection) -> dict[str, str]:
    caps = [r[0] for r in conn.execute("SELECT code FROM stocks WHERE market_cap IS NOT NULL ORDER BY market_cap DESC")]
    return {c: "前 50 大" if i < 50 else "51～300" if i < 300 else "其他" for i, c in enumerate(caps)}


def collect(conn: sqlite3.Connection, include_live: bool = True) -> list[dict]:
    """每則新聞一筆、含所有自動欄位。分析全部新聞要幾分鐘。"""
    base = lexicon.load_file(lexicon.HANDMADE_PATH)
    an = production_analyzer(remi.as_lexicon(base, extend=True), all_stocks(conn))
    trading = {r[0] for r in conn.execute("SELECT date FROM trading_days")}
    size = _size_ranks(conn)
    tags: dict[int, list[str]] = defaultdict(list)
    for nid, code in conn.execute("SELECT news_id, code FROM backfill_tags ORDER BY code"):
        tags[nid].append(code)
    items = []
    for r in conn.execute("""SELECT id, outlet, url, CASE WHEN list_title != '' THEN list_title ELSE title END AS title,
                                    body, published_at, listed_at, fetched_at FROM backfill_news WHERE status = 'ok'"""):
        items.append({"data": "回補", "id": r["id"], "outlet": r["outlet"] or "", "url": r["url"], "title": r["title"] or "",
                      "body": backfill.clean_body(r["body"] or ""), "published_at": r["published_at"],
                      "listed_at": r["listed_at"], "fetched_at": r["fetched_at"], "tags": tags.get(r["id"], [])})
    if include_live:
        for r in conn.execute("SELECT id, source, via_outlet, url, title, body, published_at, fetched_at FROM news"):
            items.append({"data": "網站", "id": r["id"], "outlet": r["via_outlet"] or r["source"] or "", "url": r["url"],
                          "title": r["title"] or "", "body": r["body"] or "", "published_at": r["published_at"],
                          "listed_at": None, "fetched_at": r["fetched_at"], "tags": []})
    for it in items:
        if it["published_at"]:
            src, t = "published_at", it["published_at"]
        elif it["listed_at"]:
            src, t = "listed_at", it["listed_at"]
        elif it["fetched_at"]:
            src, t = "只有抓取時間", it["fetched_at"]
        else:
            src, t = "沒有", None
        d, hm, q = targets.local_time(t)
        it.update(t=t, ts_source=src, ts_quality=q, bucket=_bucket(d, hm, trading),
                  delay_h=_hours(it["published_at"], it["fetched_at"]) if it["data"] == "網站" else None)
        imp = an.analyze(it["title"], it["body"]).impacts
        it["direct"] = sorted({v.stock.code for v in imp if v.type == "direct"})
        it["sector_n"] = len({v.stock.code for v in imp if v.type != "direct"})
        it["tag_found"] = (any(c in it["direct"] for c in it["tags"]) if it["tags"] else None)
        main = (it["tags"] or it["direct"] or [None])[0]
        it["size"] = size.get(main, "其他") if main else "沒有公司"
        it["recap"] = is_recap(it["title"])
    for data in ("回補", "網站"):
        group = [it for it in items if it["data"] == data]
        key = [(i, it["title"], it["t"]) for i, it in enumerate(group)]
        ev = event_groups(key)
        stale = stale_ids(key)
        members: dict[int, list[int]] = defaultdict(list)
        for i, _, _ in key:
            members[ev[i]].append(i)
        for e, idx in members.items():
            idx.sort(key=lambda i: (group[i]["t"] or "9999", i))
            for rank, i in enumerate(idx):
                group[i].update(event=f"{data}-{e}", event_size=len(idx), first_in_event=rank == 0)
        for i, it in enumerate(group):
            it["stale"] = i in stale
    for it in items:
        it["auto_type"] = ("7" if not it["direct"] else "5" if it["recap"]
                           else "6" if not it["first_in_event"] or it["stale"] else "?")
    return items


def _share(xs: list, pred) -> float | None:
    return round(sum(1 for x in xs if pred(x)) / len(xs), 4) if xs else None


def _median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    return round(xs[len(xs) // 2], 2) if xs else None


REPORT_COLS = ("維度", "值", "則數", "有 published_at", "只有 listed_at", "沒有可用時間", "抓取延遲中位數（小時）",
               "同事件非第一篇", "舊聞", "行情報導（規則）", "對不到公司", "平均直接提及家數", "FinMind 標記個股被找到")


def report_rows(items: list[dict], fetch_status: list[tuple[str, str, int]] = ()) -> list[dict]:
    dims = {"全部": lambda it: "全部", "資料來源": lambda it: it["data"], "媒體": lambda it: it["outlet"] or "（空白）",
            "發布時段": lambda it: it["bucket"], "規模": lambda it: it["size"],
            "月份": lambda it: (it["t"] or "")[:7] or "（沒有）", "規則分類": lambda it: it["auto_type"]}
    out = []
    for dim, f in dims.items():
        groups: dict[str, list[dict]] = defaultdict(list)
        for it in items:
            groups[f(it)].append(it)
        order = sorted(groups, key=lambda k: -len(groups[k]))
        if dim == "媒體":
            order = order[:30]
        for k in order:
            g = groups[k]
            tagged = [it for it in g if it["tag_found"] is not None]
            out.append(dict(zip(REPORT_COLS, (
                dim, k, len(g), _share(g, lambda it: it["ts_source"] == "published_at"),
                _share(g, lambda it: it["ts_source"] == "listed_at"),
                _share(g, lambda it: it["ts_source"] in ("只有抓取時間", "沒有") or it["bucket"] == "沒有時間"),
                _median([it["delay_h"] for it in g if it["delay_h"] is not None]),
                _share(g, lambda it: not it["first_in_event"]), _share(g, lambda it: it["stale"]),
                _share(g, lambda it: it["recap"]), _share(g, lambda it: not it["direct"]),
                round(sum(len(it["direct"]) for it in g) / len(g), 2),
                _share(tagged, lambda it: it["tag_found"])))))
    total = sum(n for _, _, n in fetch_status)
    for status, reason, n in fetch_status:
        out.append({"維度": "回補抓取狀態", "值": f"{status}：{reason or ''}".rstrip("："), "則數": n,
                    "有 published_at": None, "只有 listed_at": None,
                    "沒有可用時間": None, "抓取延遲中位數（小時）": None, "同事件非第一篇": None, "舊聞": None,
                    "行情報導（規則）": None, "對不到公司": None, "平均直接提及家數": None,
                    "FinMind 標記個股被找到": None, "_share": round(n / total, 4) if total else None})
    return out


def stratified_sample(items: list[dict], n: int = 250, seed: int = SEED, min_per: int = 2) -> list[dict]:
    """依（資料來源、發布時段、是否行情報導、規模）分層；每層至少 min_per，其餘依層大小的平方根分配。"""
    rng = random.Random(seed)
    strata: dict[tuple, list[dict]] = defaultdict(list)
    for it in sorted(items, key=lambda it: (it["data"], it["id"])):
        strata[(it["data"], it["bucket"], "行情" if it["recap"] else "非行情", it["size"])].append(it)
    keys = sorted(strata)
    alloc = {k: min(len(strata[k]), min_per) for k in keys}
    left = n - sum(alloc.values())
    if left > 0:
        w = {k: math.sqrt(len(strata[k])) for k in keys}
        tot = sum(w.values())
        for k in keys:
            alloc[k] = min(len(strata[k]), alloc[k] + int(left * w[k] / tot))
        rest = [k for k in sorted(keys, key=lambda k: -w[k]) if alloc[k] < len(strata[k])]
        i = 0
        while sum(alloc.values()) < n and rest:
            k = rest[i % len(rest)]
            if alloc[k] < len(strata[k]):
                alloc[k] += 1
            i += 1
            rest = [k for k in rest if alloc[k] < len(strata[k])]
    out = []
    for k in keys:
        pick = rng.sample(strata[k], alloc[k])
        for it in pick:
            out.append({**it, "stratum": "｜".join(k), "weight": round(len(strata[k]) / alloc[k], 4)})
    rng.shuffle(out)
    return out


SAMPLE_COLS = ("編號", "資料", "id", "媒體", "標題", "內文開頭", "網址", "published_at", "listed_at", "fetched_at",
               "時間來源", "發布時段", "引擎直接提及", "產業連動家數", "FinMind 標記", "規模", "規則_行情報導",
               "規則_同事件第幾篇", "規則_同事件篇數", "規則_舊聞", "規則分類", "stratum", "weight") + AI + HUMAN


def sample_rows(sample: list[dict]) -> list[dict]:
    rows = []
    for i, it in enumerate(sample, 1):
        rows.append(dict(zip(SAMPLE_COLS, (
            i, it["data"], it["id"], it["outlet"], it["title"], it["body"][:EXCERPT].replace("\n", " "), it["url"],
            it["published_at"] or "", it["listed_at"] or "", it["fetched_at"] or "", it["ts_source"], it["bucket"],
            " ".join(it["direct"]), it["sector_n"], " ".join(it["tags"]), it["size"], "Y" if it["recap"] else "",
            "第一篇" if it["first_in_event"] else "後續", it["event_size"], "Y" if it["stale"] else "",
            it["auto_type"], it["stratum"], it["weight"], *([""] * (len(AI) + len(HUMAN)))))))
    return rows


def import_ai(rows: list[dict], labels: list[list[str]], version: str) -> int:
    """把 AI 建議（每列：編號、類型、公司對應、漏掉的公司、含新資訊、理由）填進抽樣表的 AI_ 欄位；不碰人工欄位。"""
    by = {str(r["編號"]): r for r in rows}
    n = 0
    for lab in labels:
        num, typ, comp, miss, new, why = (lab + [""] * 6)[:6]
        r = by.get(num.strip())
        if r is None:
            raise ValueError(f"抽樣表裡沒有編號 {num}")
        if typ.strip() not in TYPES or comp.strip() not in ("Y", "部分", "N") or new.strip() not in ("Y", "N"):
            raise ValueError(f"編號 {num} 的值不合規定：{lab}")
        r.update({"AI_新聞類型": typ.strip(), "AI_公司對應": comp.strip(), "AI_漏掉的公司": miss.strip(),
                  "AI_含新資訊": new.strip(), "AI_理由": why.strip(), "AI_版本": version})
        n += 1
    return n


def _write(path: Path, rows: list[dict], cols) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:     # utf-8-sig：Excel 直接開不會亂碼
        w = csv.DictWriter(f, fieldnames=list(cols), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _yes(v: str) -> bool | None:
    v = (v or "").strip().upper()
    return True if v in ("Y", "YES", "是", "1") else False if v in ("N", "NO", "否", "0") else None


def score_rows(rows: list[dict]) -> list[dict]:
    """填好的抽樣表 → 指標列：(指標, 值, 加權值, 分母, 說明)。只用「人工_新聞類型」有填的列。"""
    done = [r for r in rows if (r.get("人工_新聞類型") or "").strip()[:1] in TYPES]
    out = []

    def add(name, xs, pred, note=""):
        xs = [x for x in xs if pred(x) is not None]
        n, wsum = len(xs), sum(float(x["weight"] or 1) for x in xs)
        val = sum(1 for x in xs if pred(x)) / n if n else None
        wval = sum(float(x["weight"] or 1) for x in xs if pred(x)) / wsum if wsum else None
        out.append({"指標": name, "值": None if val is None else round(val, 4),
                    "加權值（推回全體）": None if wval is None else round(wval, 4), "分母": n, "說明": note})

    h = lambda r: r["人工_新聞類型"].strip()[:1]
    out.append({"指標": "已標註則數", "值": len(done), "加權值（推回全體）": None, "分母": len(rows), "說明": ""})
    for t, name in TYPES.items():
        add(f"人工類型占比：{t} {name}", done, lambda r, t=t: h(r) == t)
    for t, name in (("5", "行情報導"), ("6", "重複"), ("7", "對不到公司")):
        add(f"規則「{name}」precision", [r for r in done if r["規則分類"] == t], lambda r, t=t: h(r) == t,
            f"規則判為 {t} 的，人工也判 {t} 的比例")
        add(f"規則「{name}」recall", [r for r in done if h(r) == t], lambda r, t=t: r["規則分類"] == t,
            f"人工判為 {t} 的，規則也抓到的比例")
    add("公司對應完全正確", done, lambda r: {"Y": True, "N": False, "部分": False}.get((r["人工_公司對應"] or "").strip()),
        "Y ÷（Y＋部分＋N）")
    add("公司對應至少部分正確", done, lambda r: {"Y": True, "部分": True, "N": False}.get((r["人工_公司對應"] or "").strip()))
    add("有漏掉的公司", done, lambda r: bool((r["人工_漏掉的公司"] or "").strip()))
    add("時間可信", done, lambda r: _yes(r["人工_時間可信"]), "填 ? 的不算在分母")
    add("含新資訊", done, lambda r: _yes(r["人工_含新資訊"]))
    for t, name in TYPES.items():
        add(f"含新資訊（類型 {t} {name}）", [r for r in done if h(r) == t], lambda r: _yes(r["人工_含新資訊"]))
    for src in sorted({r["資料"] for r in done}):
        add(f"含新資訊（{src}）", [r for r in done if r["資料"] == src], lambda r: _yes(r["人工_含新資訊"]))
    ai = [r for r in done if (r.get("AI_新聞類型") or "").strip()]
    if ai:   # AI 建議和人工一致的比例（人工為準）
        add("AI 新聞類型＝人工", ai, lambda r: r["AI_新聞類型"].strip() == h(r))
        add("AI 公司對應＝人工", [r for r in ai if (r["人工_公司對應"] or "").strip()],
            lambda r: r["AI_公司對應"].strip() == r["人工_公司對應"].strip())
        add("AI 含新資訊＝人工", [r for r in ai if _yes(r["人工_含新資訊"]) is not None],
            lambda r: _yes(r["AI_含新資訊"]) == _yes(r["人工_含新資訊"]))
        add("人工改了 AI 建議的類型", ai, lambda r: r["AI_新聞類型"].strip() != h(r),
            "比例高代表 AI 建議不可靠，或標註者只是照抄（比例接近 0 也要懷疑）")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="新聞資料品質抽查（P0-3）")
    ap.add_argument("cmd", choices=("report", "sample", "score", "ai-import"))
    ap.add_argument("--labels", help="ai-import：AI 建議檔（TSV：編號、類型、公司對應、漏掉的公司、含新資訊、理由）")
    ap.add_argument("--ai-version", default="", help="ai-import：寫進 AI_版本 欄的說明（模型、日期）")
    ap.add_argument("--n", type=int, default=250, help="抽樣則數（預設 250）")
    ap.add_argument("--seed", type=int, default=SEED, help=f"亂數種子（預設 {SEED}）")
    ap.add_argument("--file", help="score 要讀的檔案（預設 data/quality/audit_sample.csv）")
    ap.add_argument("--force", action="store_true", help="sample：已經有 audit_sample.csv 也覆蓋（會蓋掉填好的標註）")
    ap.add_argument("--db", help="資料庫路徑（預設 data/radar.db）")
    args = ap.parse_args(argv)
    if args.cmd == "ai-import":
        path = Path(args.file) if args.file else OUT_DIR / "audit_sample.csv"
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows, cols = list(reader), list(reader.fieldnames or [])
        with open(args.labels, encoding="utf-8") as f:
            labels = [line.rstrip("\n").split("\t") for line in f if line.strip()]
        missing = [c for c in AI if c not in cols]      # 舊版抽樣表沒有 AI 欄：插在人工欄位前面，其他欄位照舊保留
        at = cols.index(HUMAN[0]) if HUMAN[0] in cols else len(cols)
        cols[at:at] = missing
        for r in rows:
            for c in missing:
                r[c] = ""
        n = import_ai(rows, labels, args.ai_version)
        _write(path, rows, cols)
        print(f"填了 {n} 則 AI 建議到 {path}（人工欄位沒有動）")
        return 0
    if args.cmd == "score":
        path = Path(args.file) if args.file else OUT_DIR / "audit_sample.csv"
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        res = score_rows(rows)
        _write(OUT_DIR / "audit_results.csv", res, ("指標", "值", "加權值（推回全體）", "分母", "說明"))
        pc = lambda x: "—" if x is None else (f"{x * 100:.1f}%" if isinstance(x, float) else str(x))
        for r in res:
            print(f"{r['指標']:<28} {pc(r['值']):>7}　加權 {pc(r['加權值（推回全體）']):>7}　（{r['分母']} 則）{r['說明']}")
        print(f"\n存在 {OUT_DIR / 'audit_results.csv'}")
        return 0
    conn = db.connect(args.db)
    if args.cmd == "sample" and (OUT_DIR / "audit_sample.csv").exists() and not args.force:
        print(f"{OUT_DIR / 'audit_sample.csv'} 已經存在（可能已經填了標註）。要重抽請加 --force。")
        return 1
    print("分析全部新聞中（要幾分鐘）…", flush=True)
    items = collect(conn)
    if args.cmd == "report":
        fs = [(r[0], r[1], r[2]) for r in
              conn.execute("SELECT status, reason, COUNT(*) FROM backfill_news GROUP BY 1, 2 ORDER BY 3 DESC")]
        rows = report_rows(items, fs)
        _write(OUT_DIR / "data_quality_report.csv", rows, REPORT_COLS + ("_share",))
        for r in rows:
            if r["維度"] in ("全部", "資料來源", "發布時段", "規模", "規則分類"):
                print(f"{r['維度']}／{r['值']}：{r['則數']} 則　published_at {r['有 published_at']}　行情 {r['行情報導（規則）']}"
                      f"　同事件非第一篇 {r['同事件非第一篇']}　舊聞 {r['舊聞']}　對不到公司 {r['對不到公司']}"
                      f"　標記被找到 {r['FinMind 標記個股被找到']}　抓取延遲 {r['抓取延遲中位數（小時）']} 小時")
        print(f"\n存在 {OUT_DIR / 'data_quality_report.csv'}")
    else:
        sample = stratified_sample(items, args.n, args.seed)
        _write(OUT_DIR / "audit_sample.csv", sample_rows(sample), SAMPLE_COLS)
        print(f"抽了 {len(sample)} 則（{len({s['stratum'] for s in sample})} 層），存在 {OUT_DIR / 'audit_sample.csv'}")
        print("請填「人工_」開頭的欄位，填完執行 python -m radar.quality score")
    return 0


if __name__ == "__main__":
    sys.exit(main())
