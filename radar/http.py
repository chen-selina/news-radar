"""有禮貌的 HTTP 用戶端：先查 robots.txt，固定 User-Agent、同網站請求間隔、逾時與重試。"""
from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

from . import config

_last_hit: dict[str, float] = {}


class Blocked(Exception):
    """robots.txt 不允許讀取這個網址。"""


def _wait_turn(url: str) -> None:
    host = urlparse(url).hostname or ""
    interval = config.HOST_INTERVALS.get(host, config.REQUEST_INTERVAL)
    gap = time.monotonic() - _last_hit.get(host, 0.0)
    if gap < interval:
        time.sleep(interval - gap)
    _last_hit[host] = time.monotonic()


def get(url: str, *, retries: int = 2, respect_robots: bool = True, **kwargs) -> httpx.Response:
    if respect_robots:
        from .crawl import robots  # 延後載入，避免循環 import
        if not robots.allowed(url):
            raise Blocked(f"robots.txt 不允許讀取：{url}")
    headers = {"User-Agent": config.USER_AGENT, **kwargs.pop("headers", {})}
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        _wait_turn(url)
        try:
            r = httpx.get(url, headers=headers, timeout=config.REQUEST_TIMEOUT,
                          follow_redirects=True, **kwargs)
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            last_err = e
            if e.response.status_code < 500 and e.response.status_code != 429:
                break  # 404、403 這類重試也沒用
        except httpx.TransportError as e:
            last_err = e
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"讀取失敗：{url}（{last_err}）") from last_err


def get_json(url: str, **kwargs):
    return get(url, **kwargs).json()
