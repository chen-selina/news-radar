"""證交所與櫃買中心的公開資料：抓取與轉成統一格式。

網址都是官方 OpenAPI（https://openapi.twse.com.tw、https://www.tpex.org.tw/openapi），
回傳 JSON 陣列。官方改欄位名稱時，只要改這一支的 parse_* 函式。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import http
from .industries import INDUSTRIES

TWSE_BASIC_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_BASIC_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
TWSE_DAY_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_DAY_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


@dataclass
class Company:
    code: str
    name: str
    full_name: str
    industry: str | None
    market: str  # 上市／上櫃
    shares: int | None


@dataclass
class Quote:
    close: float
    date: str  # 西元 YYYY-MM-DD


def roc_to_iso(s: str) -> str | None:
    """民國年日期轉西元，支援 1151022、115/10/22、115-10-22。"""
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{2,3})(\d{2})(\d{2})", s) or re.fullmatch(r"(\d{2,3})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
    if not m:
        return None
    y, mo, d = int(m[1]) + 1911, int(m[2]), int(m[3])
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _num(s) -> float | None:
    try:
        v = float(str(s).replace(",", "").strip())
    except ValueError:
        return None
    return v if v > 0 else None


def _industry(code: str, unknown: set[str]) -> str | None:
    code = (code or "").strip()
    if code in INDUSTRIES:
        return INDUSTRIES[code]
    if code:
        unknown.add(code)
    return None


def parse_twse_basic(rows: list[dict], unknown: set[str]) -> list[Company]:
    out = []
    for r in rows:
        code, name = r.get("公司代號", "").strip(), r.get("公司簡稱", "").strip()
        if not code or not name:
            continue
        shares = _num(r.get("已發行普通股數或TDR原股發行股數"))
        out.append(Company(code, name, r.get("公司名稱", "").strip(), _industry(r.get("產業別"), unknown),
                           "上市", int(shares) if shares else None))
    return out


def parse_tpex_basic(rows: list[dict], unknown: set[str]) -> list[Company]:
    out = []
    for r in rows:
        code, name = r.get("SecuritiesCompanyCode", "").strip(), r.get("CompanyAbbreviation", "").strip()
        if not code or not name:
            continue
        shares = _num(r.get("IssueShares"))
        out.append(Company(code, name, r.get("CompanyName", "").strip(),
                           _industry(r.get("SecuritiesIndustryCode"), unknown),
                           "上櫃", int(shares) if shares else None))
    return out


def parse_twse_day(rows: list[dict]) -> dict[str, Quote]:
    out = {}
    for r in rows:
        close, date = _num(r.get("ClosingPrice")), roc_to_iso(r.get("Date", ""))
        if close and date:
            out[r.get("Code", "").strip()] = Quote(close, date)
    return out


def parse_tpex_day(rows: list[dict]) -> dict[str, Quote]:
    out = {}
    for r in rows:
        close, date = _num(r.get("Close")), roc_to_iso(r.get("Date", ""))
        if close and date:
            out[r.get("SecuritiesCompanyCode", "").strip()] = Quote(close, date)
    return out


def fetch_all() -> dict[str, list[dict]]:
    """抓四份原始資料；回傳原始 JSON，方便存檔或除錯。"""
    return {
        "twse_basic": http.get_json(TWSE_BASIC_URL),
        "tpex_basic": http.get_json(TPEX_BASIC_URL),
        "twse_day": http.get_json(TWSE_DAY_URL),
        "tpex_day": http.get_json(TPEX_DAY_URL),
    }
