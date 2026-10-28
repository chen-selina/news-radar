"""貼連結分析：讀使用者貼上的一個網址，擷取標題與內文。

讀不到時不丟例外，而是回傳白話的原因（reason），讓網頁請使用者手動貼上內文。
只讀公開網站：網址指向本機或內部網路（127.0.0.1、192.168.x.x 等）一律拒絕，
避免有人透過這個功能讓伺服器去讀內部服務。
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .. import http
from . import urls
from .extract import extract
from .models import OTHER, SOURCES


class BadUrl(ValueError):
    """網址格式不對或不能讀（例如指向內部網路）。"""


@dataclass
class Fetched:
    url: str
    source: str               # yahoo／google／udn／bw／cw／other
    source_name: str          # 中文名稱；不在五個來源內為「其他網站」
    ok: bool                  # 讀到可分析的文字（至少有標題）
    reason: str | None = None  # ok=False 時的原因；ok=True 時可能是提醒（只有摘要、付費文章）
    title: str = ""
    body: str = ""
    published_at: str | None = None
    via_outlet: str | None = None
    paywalled: bool = False
    partial: bool = False     # 只讀到摘要，沒有內文段落


def resolve(host: str) -> list[str]:
    return [ai[4][0] for ai in socket.getaddrinfo(host, None)]


def check_url(raw: str) -> str:
    """整理網址並確認可以讀；不行就丟 BadUrl。回傳整理後的網址。"""
    raw = (raw or "").strip()
    if not urls.is_http(raw):
        raise BadUrl("請貼上以 http:// 或 https:// 開頭的完整網址")
    url = urls.canonical(raw)
    host = urlsplit(url).hostname or ""
    try:
        ips = resolve(host)
    except (socket.gaierror, UnicodeError):
        raise BadUrl(f"找不到這個網站（{host}），請確認網址是否正確") from None
    for ip in ips:
        a = ipaddress.ip_address(ip.split("%")[0])
        if not a.is_global:
            raise BadUrl("這個網址指向本機或內部網路，不能讀取")
    return url


def _http_reason(status: int) -> str:
    if status in (401, 403):
        return f"網站拒絕讀取（HTTP {status}），可能需要登入或阻擋程式讀取。請開啟原文，手動貼上內文"
    if status in (404, 410):
        return f"找不到這個網頁（HTTP {status}），可能已刪除或網址不完整"
    if status == 429:
        return "網站暫時限制讀取次數（HTTP 429），請稍後再試，或手動貼上內文"
    return f"網站回應錯誤（HTTP {status}），請稍後再試，或手動貼上內文"


def fetch(raw_url: str) -> Fetched:
    """讀網址並擷取內容。網址本身不合格丟 BadUrl；讀取失敗回傳 ok=False 與原因。"""
    url = check_url(raw_url)
    src = urls.source_of(url)
    res = Fetched(url, src or "other", SOURCES[src] if src else OTHER, ok=False)
    try:
        resp = http.get(url, retries=1)
    except http.Blocked:
        res.reason = "這個網站的 robots.txt 不允許程式讀取這個網址。請開啟原文，手動貼上標題與內文"
        return res
    except RuntimeError as e:
        cause = e.__cause__
        if isinstance(cause, httpx.HTTPStatusError):
            res.reason = _http_reason(cause.response.status_code)
        else:
            res.reason = "連不上網站（逾時或網路問題），請稍後再試，或手動貼上內文"
        return res
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype.lower():
        res.reason = f"這個網址不是網頁（{ctype.split(';')[0] or '未知格式'}），請手動貼上內文"
        return res
    page = extract(resp.text, src)
    res.title, res.body = page.title, page.body
    res.published_at, res.via_outlet, res.paywalled = page.published_at, page.via_outlet, page.paywalled
    res.partial = page.partial or not page.body
    if not res.title and not res.body:
        res.reason = "網頁上讀不到文字，可能需要登入或內容由 JavaScript 載入。請手動貼上標題與內文"
        return res
    res.ok = True
    if res.paywalled and res.partial:
        res.reason = "這是付費文章，只讀到標題與摘要；有全文的話請貼上內文再分析"
    elif res.partial:
        res.reason = "只讀到標題與摘要，沒有讀到內文段落；可以手動貼上內文再分析"
    elif res.paywalled:
        res.reason = "網站標示這是付費文章，讀到的內文可能不完整"
    return res
