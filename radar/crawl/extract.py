"""從文章網頁擷取標題、內文、發布時間、轉載媒體、是否付費。

先讀網頁的結構化資料（JSON-LD、og 標籤），內文再依各網站的段落區塊取文字；
找不到區塊時退回通用規則（與原型 extractArticle 相同：中文字 25 字以上的段落）。
網站改版時，通常只要改 BODY_SELECTORS。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from .. import config

MAX_BODY = 3000  # 內文只存分析需要的長度

# 各網站文章內文所在的區塊（CSS 選擇器，依序嘗試）
BODY_SELECTORS = {
    "bw": [".article-body", ".Single-article"],
    "cw": [".article__content", ".article-content"],
    "udn": ["#article_body", "section.article-body__editor", ".article-body", "article"],
    "yahoo": [".caas-body", ".atoms", "article"],
}
# 段落裡常見、不是新聞內容的句子：開頭符合就整段略過
_BOILERPLATE = re.compile(r"^(延伸閱讀|更多.*新聞|責任編輯|看更多|訂閱|點我|加入.*LINE|圖／|圖/|資料來源|本文.*授權|※|【.*】$)")
# 投資警語與版權聲明：段落裡出現就略過（警語裡的「審慎」「風險」會被當成利空詞）
_DISCLAIMER = re.compile(r"提醒您[:：]?$|僅供參考|投資一定有風險|有賺有賠|自行負責|不得轉載|版權所有|請詳閱公開說明書")
_TITLE_SUFFIX = re.compile(r"\s*(?:[|｜]|\s-\s).*$")
_CJK = re.compile(r"[一-鿿]")
# 中文字（含全形標點）之間的空白：原始網頁在句子中間換行造成，會讓「營收 成長」比對不到「營收成長」
_WIDE = r"[一-鿿　-〿＀-￯]"
_CJK_GAP = re.compile(rf"(?<={_WIDE})\s+(?={_WIDE})")
_DOMAIN_SUFFIX = re.compile(r"\s+[\w.-]+\.(?:com|net|org|tw)(?:\.\w{2})?$", re.I)


def squash(t: str) -> str:
    """整理空白：連續空白變一個，中文字之間的空白拿掉。"""
    return _CJK_GAP.sub("", re.sub(r"\s+", " ", t or "")).strip()
ARTICLE_TYPES = {"NewsArticle", "Article", "ReportageNewsArticle", "AnalysisNewsArticle", "BlogPosting"}


@dataclass
class Page:
    title: str
    body: str
    published_at: str | None
    via_outlet: str | None
    paywalled: bool
    partial: bool = False  # True＝讀不到內文段落，body 只是網頁上的摘要


def to_taipei_iso(value: str | None) -> str | None:
    """各種日期格式轉成台北時間 ISO 8601；沒有時區的視為台北時間。"""
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)  # RSS 的 RFC 2822 格式
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("/", "-"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.TAIPEI)
    return dt.astimezone(config.TAIPEI).isoformat(timespec="seconds")


def clean_title(t: str) -> str:
    """「標題 - 商周」「標題｜天下雜誌」→「標題」（去掉後太短就保留原樣）。"""
    t = squash(t)
    short = _TITLE_SUFFIX.sub("", t)
    return short if len(short) >= 8 else t


def cjk_count(s: str) -> int:
    return len(_CJK.findall(s))


def _json_ld(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop(0)
            if not isinstance(d, dict):
                continue
            stack.extend(d.get("@graph", []))
            types = d.get("@type")
            types = set(types) if isinstance(types, list) else {types}
            if types & ARTICLE_TYPES:
                return d
    return {}


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _name(v) -> str | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("name")
    if not isinstance(v, str) or not v.strip():
        return None
    return _DOMAIN_SUFFIX.sub("", v.strip()) or v.strip()  # 「三立新聞網 setn.com」→「三立新聞網」


def _is_noise(t: str) -> bool:
    return cjk_count(t) < 8 or bool(_BOILERPLATE.match(t) or _DISCLAIMER.search(t))


def _paragraphs(root) -> list[str]:
    out, seen = [], set()
    for p in root.find_all(["p", "h2", "h3", "li"]) or [root]:
        t = squash(p.get_text(" ", strip=True))
        if _is_noise(t) or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _body(soup: BeautifulSoup, source: str | None) -> str:
    for tag in soup(["script", "style", "noscript", "figure", "figcaption", "aside", "nav", "footer", "form"]):
        tag.decompose()
    for sel in BODY_SELECTORS.get(source or "", []) + ["article"]:
        root = soup.select_one(sel)
        if root:
            paras = _paragraphs(root)
            if sum(map(len, paras)) >= 80:
                return "\n".join(paras)
    # 通用規則：整頁中文字 25 字以上的段落
    paras, seen = [], set()
    for p in soup.find_all("p"):
        t = squash(p.get_text(" ", strip=True))
        if cjk_count(t) >= 25 and not _is_noise(t) and t not in seen:
            seen.add(t)
            paras.append(t)
    return "\n".join(paras)


def extract(html: str, source: str | None = None) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    ld = _json_ld(soup)
    h1 = soup.find("h1")
    title = clean_title(ld.get("headline") or _meta(soup, "og:title") or (h1.get_text(" ", strip=True) if h1 else "")
                        or (soup.title.string if soup.title and soup.title.string else ""))
    published = to_taipei_iso(ld.get("datePublished") or _meta(soup, "article:published_time", "pubdate",
                                                                  "publishdate", "date"))
    publisher = _name(ld.get("publisher"))
    via = _name(ld.get("provider")) or (publisher if publisher and "yahoo" not in publisher.lower() else None)
    free = ld.get("isAccessibleForFree")
    paywalled = free is False or (isinstance(free, str) and free.strip().lower() == "false")
    body, partial = _body(soup, source), False
    if len(body) < 80:  # 段落太少（付費牆、需要登入、網頁靠 JavaScript 載入）
        if ld.get("articleBody"):
            body = ld["articleBody"]
        else:
            body, partial = (ld.get("description") or _meta(soup, "og:description", "description") or body), True
        body = "\n".join(filter(None, (squash(x) for x in body.split("\n"))))
    return Page(title, body.strip()[:MAX_BODY], published, via, paywalled, partial)
