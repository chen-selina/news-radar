"""robots.txt 檢查（RFC 9309）。

Python 內建的 urllib.robotparser 不支援 * 與 $ 萬用字元，也不是「最長規則優先」，
例如 Google新聞的規則會判斷錯，所以自己實作。
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from .. import config

_cache: dict[str, "Robots"] = {}


class Robots:
    def __init__(self, text: str, agent: str):
        self.rules = _rules_for(_parse(text), agent.lower())

    def allowed(self, path: str) -> bool:
        best_len, allow = -1, True
        for is_allow, pattern, regex in self.rules:
            if regex.match(path):
                n = len(pattern)
                # 最長的規則勝出；一樣長時 Allow 優先
                if n > best_len or (n == best_len and is_allow):
                    best_len, allow = n, is_allow
        return allow


def _parse(text: str) -> list[tuple[list[str], list[tuple[bool, str]]]]:
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:  # 上一組規則結束，開始新的一組
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(val.lower())
        elif key in ("allow", "disallow") and agents:
            if val:  # 「Disallow:」空白代表全部允許，不必記
                rules.append((key == "allow", val))
    if agents:
        groups.append((agents, rules))
    return groups


def _rules_for(groups, agent: str):
    """有點名我們（User-agent: twnewsradar）的組優先，否則用「*」組。"""
    token = agent.split("/")[0].strip()
    mine = [r for ags, rs in groups if token in ags for r in rs]
    if not any(token in ags for ags, _ in groups):
        mine = [r for ags, rs in groups if "*" in ags for r in rs]
    return [(allow, p, _compile(p)) for allow, p in mine]


def _compile(pattern: str) -> re.Pattern:
    end = pattern.endswith("$")
    body = re.escape(pattern[:-1] if end else pattern).replace(r"\*", ".*")
    return re.compile(body + ("$" if end else ""))


def allowed(url: str) -> bool:
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    if base not in _cache:
        try:
            r = httpx.get(base + "/robots.txt", headers={"User-Agent": config.USER_AGENT},
                          timeout=config.REQUEST_TIMEOUT, follow_redirects=True)
        except httpx.TransportError:
            return False  # 連不上就先當作不允許，不快取，下次再試
        # 404 等代表沒有 robots.txt：全部允許；5xx 依 RFC 視為全部禁止
        text = r.text if r.status_code == 200 else ("User-agent: *\nDisallow: /" if r.status_code >= 500 else "")
        _cache[base] = Robots(text, config.USER_AGENT)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return _cache[base].allowed(path)
