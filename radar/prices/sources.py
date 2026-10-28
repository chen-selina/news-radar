"""證交所與櫃買中心的歷史收盤價。

| 資料 | 網址 | 一次給 |
|---|---|---|
| 交易日曆＋加權指數 | twse.com.tw/rwd/zh/afterTrading/FMTQIK | 一個月 |
| 上市個股收盤價 | twse.com.tw/rwd/zh/afterTrading/MI_INDEX（type=ALLBUT0999） | 一天、全部上市股票 |
| 上櫃個股收盤價 | tpex.org.tw/www/zh-tw/afterTrading/otc（type=EW） | 一天、全部上櫃股票 |

FMTQIK 只列實際開市的日子，國定假日與颱風休市自然不在裡面，所以拿它當交易日曆。
官方改欄位時，改這一支的 parse_* 函式。
"""
from __future__ import annotations

from datetime import date

from .. import http
from ..stocks.sources import roc_to_iso

FMTQIK_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={ymd}&response=json"
MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json"
TPEX_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc?date={y}%2F{m:02d}%2F{d:02d}&type=EW&response=json"


def num(s) -> float | None:
    try:
        v = float(str(s).replace(",", "").strip())
    except ValueError:
        return None
    return v if v > 0 else None


def _col(fields: list[str], *names: str) -> int:
    clean = [f.strip() for f in fields]
    for n in names:
        if n in clean:
            return clean.index(n)
    raise KeyError(f"官方資料找不到欄位：{names}（目前欄位：{clean}）")


def parse_fmtqik(js: dict) -> dict[str, float]:
    """月份交易日曆：{西元日期: 加權指數收盤}。"""
    if js.get("stat") != "OK":
        return {}
    fields = js["fields"]
    i_date, i_idx = _col(fields, "日期"), _col(fields, "發行量加權股價指數")
    out = {}
    for row in js.get("data", []):
        d, v = roc_to_iso(row[i_date]), num(row[i_idx])
        if d and v:
            out[d] = v
    return out


def parse_mi_index(js: dict) -> dict[str, float]:
    """某一天全部上市證券的收盤價：{代號: 收盤價}；沒有成交（--）的略過。"""
    if js.get("stat") != "OK":
        return {}
    for t in js.get("tables", []):
        if "每日收盤行情" in (t.get("title") or ""):
            i_code, i_close = _col(t["fields"], "證券代號"), _col(t["fields"], "收盤價")
            return {r[i_code].strip(): v for r in t.get("data", []) if (v := num(r[i_close])) is not None}
    return {}


def parse_tpex(js: dict) -> dict[str, float]:
    """某一天全部上櫃證券的收盤價：{代號: 收盤價}。"""
    tables = js.get("tables") or []
    if not tables:
        return {}
    t = tables[0]
    i_code, i_close = _col(t["fields"], "代號"), _col(t["fields"], "收盤")
    return {r[i_code].strip(): v for r in t.get("data", []) if (v := num(r[i_close])) is not None}


def fetch_month(d: date) -> dict[str, float]:
    return parse_fmtqik(http.get_json(FMTQIK_URL.format(ymd=d.strftime("%Y%m01"))))


def fetch_twse_day(d: date) -> dict[str, float]:
    return parse_mi_index(http.get_json(MI_INDEX_URL.format(ymd=d.strftime("%Y%m%d"))))


def fetch_tpex_day(d: date) -> dict[str, float]:
    return parse_tpex(http.get_json(TPEX_URL.format(y=d.year, m=d.month, d=d.day)))
