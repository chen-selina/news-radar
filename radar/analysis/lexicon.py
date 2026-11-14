"""情緒詞典與版本。

正式詞典是 config/lexicon.json（進版控，隊友共用）：以論文字典（config/remi）為底，加上檔案裡列的調整。
資料庫的 lexicon_versions／lexicon_terms
存每一版的快照：檔案內容一改，current() 就會自動建立新版本。預測記下版本號，
之後詞典怎麼改，都能用 get(conn, 版本) 還原當時的詞典。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

LEXICON_PATH = config.CONFIG_DIR / "lexicon.json"
HANDMADE_PATH = config.CONFIG_DIR / "lexicon-handmade.json"   # 原本人工給分的詞典，只用來比較
_NOTE_KEY = "_說明"


@dataclass(frozen=True)
class Lexicon:
    sentiment: dict[str, float]  # 詞 → 分數，順序有意義
    sectors: dict[str, str]      # 產業關鍵字 → 族群
    version: int | None = field(default=None, compare=False)

    def checksum(self) -> str:
        body = json.dumps([list(self.sentiment.items()), list(self.sectors.items())], ensure_ascii=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------- 設定檔 ----------

def _base_terms(name: str | None) -> dict[str, float]:
    """設定檔 "base" 指定的底層字典。目前只有 remi（台灣財經情緒字典，每個詞 ±1）。"""
    if not name:
        return {}
    if name != "remi":
        raise ValueError(f"不認得的底層字典：{name}（目前只支援 remi）")
    from . import remi          # 放在這裡才不會循環匯入
    return {w: float(s) for w, s in remi.load().polarity.items()}


def load_file(path: Path = LEXICON_PATH) -> Lexicon:
    """有 "base" 時：先載入底層字典，再套上 "sentiment" 裡的調整（0＝停用底層字典的那個詞）。
    沒有 "base" 時："sentiment" 就是完整的詞典（0 的詞照樣保留）。"""
    d = json.loads(path.read_text(encoding="utf-8"))
    own = {k: float(v) for k, v in d["sentiment"].items()}
    if not d.get("base"):
        return Lexicon(own, dict(d["sectors"]))
    sentiment = {**_base_terms(d["base"]), **own}
    return Lexicon({t: w for t, w in sentiment.items() if w != 0}, dict(d["sectors"]))


def save_file(lex: Lexicon, path: Path = LEXICON_PATH) -> None:
    """一個詞一行，方便在 GitHub 上看差異。有 "base" 時只寫和底層字典不同的地方。"""
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    note, base_name = old.get(_NOTE_KEY), old.get("base")
    sentiment = dict(lex.sentiment)
    if base_name:
        base = _base_terms(base_name)
        sentiment = {t: w for t, w in lex.sentiment.items() if base.get(t) != w}
        sentiment.update({t: 0.0 for t in base if t not in lex.sentiment})    # 底層字典有、但被刪掉的詞
    dump = lambda v: json.dumps(v, ensure_ascii=False)
    num = lambda w: str(int(w)) if float(w).is_integer() else repr(w)
    lines = ["{"]
    if note:
        lines.append(f" {dump(_NOTE_KEY)}: {dump(note)},")
    if base_name:
        lines.append(f' "base": {dump(base_name)},')
    lines.append(' "sentiment": {')
    if sentiment:
        lines.append(",\n".join(f"  {dump(t)}: {num(w)}" for t, w in sentiment.items()))
    lines.append(" },")
    lines.append(' "sectors": {')
    lines.append(",\n".join(f"  {dump(k)}: {dump(s)}" for k, s in lex.sectors.items()))
    lines.append(" }")
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 版本（資料庫） ----------

def get(conn: sqlite3.Connection, version: int) -> Lexicon:
    rows = conn.execute(
        "SELECT term, kind, weight, sector FROM lexicon_terms WHERE version = ? ORDER BY position",
        (version,)).fetchall()
    if not rows and not conn.execute("SELECT 1 FROM lexicon_versions WHERE version = ?", (version,)).fetchone():
        raise KeyError(f"沒有第 {version} 版詞典")
    return Lexicon({r["term"]: r["weight"] for r in rows if r["kind"] == "sentiment"},
                   {r["term"]: r["sector"] for r in rows if r["kind"] == "sector"}, version)


def save_version(conn: sqlite3.Connection, lex: Lexicon, note: str = "") -> Lexicon:
    """存成新版本；內容與最新版本相同時不重複建立。"""
    latest = conn.execute("SELECT version, checksum FROM lexicon_versions ORDER BY version DESC LIMIT 1").fetchone()
    if latest and latest["checksum"] == lex.checksum():
        return Lexicon(lex.sentiment, lex.sectors, latest["version"])
    version = (latest["version"] if latest else 0) + 1
    terms = [(version, i, t, "sentiment", w, None) for i, (t, w) in enumerate(lex.sentiment.items())]
    terms += [(version, len(terms) + i, k, "sector", None, s) for i, (k, s) in enumerate(lex.sectors.items())]
    with conn:
        conn.execute("INSERT INTO lexicon_versions (version, checksum, created_at, note) VALUES (?, ?, ?, ?)",
                     (version, lex.checksum(), config.now_iso(), note))
        conn.executemany("INSERT INTO lexicon_terms (version, position, term, kind, weight, sector) "
                         "VALUES (?, ?, ?, ?, ?, ?)", terms)
    return Lexicon(lex.sentiment, lex.sectors, version)


def current(conn: sqlite3.Connection, path: Path = LEXICON_PATH) -> Lexicon:
    """讀設定檔；跟最新版本不同就建立新版本。回傳帶版本號的詞典。"""
    return save_version(conn, load_file(path), note=f"同步自 {path.name}")


# ---------- 新增／刪除詞（改設定檔並建立新版本） ----------

def set_term(conn: sqlite3.Connection, term: str, weight: float, path: Path = LEXICON_PATH) -> Lexicon:
    """新增情緒詞，或修改既有詞的分數（保留原本位置，與原型 Object.assign 的行為相同）。"""
    term = term.strip()
    if not term:
        raise ValueError("詞彙不能是空白")
    if not -5 <= weight <= 5 or weight == 0:
        raise ValueError("分數必須在 −5 到 5 之間，且不能是 0")
    lex = load_file(path)
    lex = Lexicon({**lex.sentiment, term: float(weight)}, lex.sectors)
    save_file(lex, path)
    return save_version(conn, lex, note=f"設定「{term}」{weight:+g}")


def remove_term(conn: sqlite3.Connection, term: str, path: Path = LEXICON_PATH) -> Lexicon:
    lex = load_file(path)
    if term not in lex.sentiment:
        raise KeyError(f"詞典裡沒有「{term}」")
    lex = Lexicon({t: w for t, w in lex.sentiment.items() if t != term}, lex.sectors)
    save_file(lex, path)
    return save_version(conn, lex, note=f"刪除「{term}」")
