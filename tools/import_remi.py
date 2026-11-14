"""把「台灣財經情緒字典」（黃裕烈，2025）的 Excel 檔轉成專案用的純文字檔。

來源：https://github.com/RemiMFB/Taiwan-Financial-Sentiment-Dictionary （資料授權 CC BY 4.0）
字典每年 3 月改版。改版後下載新的壓縮檔、解壓縮，再執行一次：

    python tools/import_remi.py "解壓縮後的資料夾/data"

會覆寫 config/remi/dictionary.tsv 與 config/remi/topics.tsv。只用 Python 內建功能讀 Excel，不必另外安裝套件。
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "config" / "remi"
M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
# 工作表 → （方向，種類）。Added／Deprecated 只是異動紀錄，內容已經反映在正負面工作表裡。
SHEETS = {"Positive": ("pos", "word"), "Negative": ("neg", "word"),
          "Negation of a Negative": ("pos", "negation"), "Negation of a Positive": ("neg", "negation")}
TOPICS = {"TopicGrowth": "景氣", "TopicMonetaryPolicy": "貨幣政策", "TopicInterestRate": "利率",
          "TopicExchangeRate": "匯率", "TopicPrice": "物價"}


def read_xlsx(path: Path) -> dict[str, list[list[str]]]:
    """工作表名稱 → 每列的儲存格文字。"""
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(M + "si"):
            shared.append("".join(t.text or "" for t in si.iter(M + "t")))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out: dict[str, list[list[str]]] = {}
    for s in ET.fromstring(z.read("xl/workbook.xml")).find(M + "sheets"):
        target = rels[s.get(R + "id")]
        target = target.lstrip("/") if target.startswith("/") else "xl/" + target
        rows = []
        for row in ET.fromstring(z.read(target)).iter(M + "row"):
            cells = []
            for c in row.findall(M + "c"):
                v = c.find(M + "v")
                if c.get("t") == "s" and v is not None:
                    cells.append(shared[int(v.text)])
                elif c.get("t") == "inlineStr":
                    cells.append("".join(t.text or "" for t in c.iter(M + "t")))
                else:
                    cells.append(v.text if v is not None else "")
            rows.append(cells)
        out[s.get("name")] = rows
    return out


def find_one(data_dir: Path, name: str) -> Path:
    """同名檔案取路徑排序最後的（Source Up to 2025 排在 Source Up To 2024 後面）。"""
    found = sorted(data_dir.rglob(name), key=lambda p: str(p).lower())
    if not found:
        raise FileNotFoundError(f"在 {data_dir} 找不到 {name}")
    return found[-1]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 1
    data_dir = Path(argv[0])
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    src = find_one(data_dir, "RemiDictionary.xlsx")
    book = read_xlsx(src)
    seen: dict[str, tuple] = {}
    both: set[str] = set()
    for sheet, (polarity, kind) in SHEETS.items():
        for row in book.get(sheet, [])[1:]:
            word = (row[0] if row else "").strip()
            if not word:
                continue
            if word in seen and seen[word][0] != polarity:
                both.add(word)
            num = lambda i: row[i] if len(row) > i and row[i].isdigit() else "0"
            seen.setdefault(word, (polarity, kind, num(1), num(2)))
    for w in both:  # 同時列在正面與負面的詞無法判斷方向，不收
        del seen[w]
    lines = ["word\tpolarity\tkind\tcount\tarticles"] + ["\t".join((w, *v)) for w, v in seen.items()]
    (OUT_DIR / "dictionary.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    n_pos = sum(v[0] == "pos" for v in seen.values())
    print(f"{src.parent.name}/{src.name}：正面 {n_pos}、負面 {len(seen) - n_pos}"
          f"（正負面都列的 {len(both)} 個不收：{'、'.join(sorted(both))}）")

    lines = ["topic\tflag\tword"]
    for stem, topic in TOPICS.items():
        book = read_xlsx(find_one(data_dir, stem + ".xlsx"))
        for sheet, flag in (("TRUE", "true"), ("FALSE", "false")):
            words = [r[0].strip() for r in book.get(sheet, [])[1:] if r and r[0].strip()]
            lines += [f"{topic}\t{flag}\t{w}" for w in dict.fromkeys(words)]
            print(f"{topic} {sheet}：{len(set(words))} 個")
    (OUT_DIR / "topics.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已寫入 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
