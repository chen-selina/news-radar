"""標籤定義的唯一來源：「實際方向」怎麼算、報酬從哪天算到哪天、哪些樣本為什麼不能評分。

radar/validate.py、radar/scoring.py、radar/train.py 都從這裡取規則，三個模組對同一筆樣本會得到同一個標籤。

目前的定義（TARGET_VERSION）
- 報酬：進場日收盤 → 進場後第 N 個交易日收盤；預設扣除同期加權指數報酬（excess），可改個股本身（raw）。
  收盤一律用還原收盤（除權息調整，radar/prices/adjust.py 匯入的係數）；沒有係數的個股用原始收盤。
- 進場日（entry_index，規則 close，預設）：資訊在交易日 13:00（ENTRY_CUTOFF）以前可取得 → 當日收盤進場；
  13:00 以後、沒有時間、或發生在休市日 → 下一個交易日收盤進場。只有每日收盤價，所以只能用收盤成交；
  13:00 留給收盤前的集合競價（13:25～13:30）下單。起點價格一定在資訊可取得之後，買得到。
  舊規則 legacy（2026-11-19 以前）：09:00 前用前一交易日收盤、09:00 以後用當日收盤。開盤前與盤後的新聞
  會把資訊出來以前就已經發生的價格（前一日收盤、當日 13:30 收盤）當起點，等於偷看；只留著和舊結果比較。
- 資訊可取得時間：回補新聞用發布時間（歷史重建，假設發布當下就讀得到）；網站收錄的新聞用「系統第一次看到」
  的時間（發布時間與抓取時間取較晚的），原始預測再以預測建立時間為準。
- 方向：報酬 > 門檻為 up、< −門檻為 down，其餘為 flat。三類都是有效標籤，flat 不是缺資料。
- 標籤結束日：第 N 個交易日。訓練時，標籤結束日晚於模型擬合時點的樣本必須剔除（purging）。

不能評分的樣本一律標示原因（EXCLUSION_REASONS），不要靜默丟掉。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))
OPEN_TIME = "09:00"       # legacy 規則的分界
ENTRY_CUTOFF = "13:00"    # close 規則：這個時間（含）以前可取得的資訊，當日收盤進場
ENTRY_RULES = ("close", "legacy", "given")
ENTRY_RULE = "close"

TARGET_VERSION = "close-to-close-v3"   # v2（2026-11-19）：起點改成可成交的收盤（ENTRY_RULE），見 entry_index()
                                       # v3（2026-11-20）：報酬改用還原收盤（price_adj，見 radar/prices/adjust.py），
                                       #   除權息不再被算成下跌；沒有匯入係數的個股與大盤仍是原始收盤
CLASSES = ("down", "flat", "up")

# 樣本不能評分的原因（與 radar/validate.py 的 Row.status 相同）
EXCLUSION_REASONS = {
    "wait": "還沒滿 N 個交易日",
    "wait_base": "基準日的收盤價還沒出來",
    "nodata": "缺價格（基準日或結束日沒有收盤價）",
    "no_label": "沒有標籤（原因未記錄）",
}


@dataclass(frozen=True)
class LabelSpec:
    horizon: int = 1
    benchmark: str = "excess"      # excess＝扣除大盤；raw＝個股本身
    threshold: float = 0.01        # 持平門檻（小數；0.01＝±1%）

    def __post_init__(self):
        if self.horizon < 1:
            raise ValueError("觀察期間至少 1 個交易日")
        if self.benchmark not in ("excess", "raw"):
            raise ValueError("benchmark 只能是 excess／raw")
        if self.threshold < 0:
            raise ValueError("門檻不能是負的")

    @property
    def version(self) -> str:
        return f"{TARGET_VERSION}|h{self.horizon}|{self.benchmark}|t{self.threshold:g}"

    def as_dict(self) -> dict:
        return {"target_version": TARGET_VERSION, "horizon": self.horizon, "benchmark": self.benchmark,
                "threshold": self.threshold}


def direction(ret: float | None, threshold: float = 0.01) -> str | None:
    """報酬 → up／down／flat；沒有報酬為 None。"""
    if ret is None:
        return None
    return "up" if ret > threshold else "down" if ret < -threshold else "flat"


def end_index(base: int, horizon: int) -> int:
    """標籤結束日（第 N 個交易日）在交易日序列裡的位置。"""
    return base + horizon


def observed_by(end_day: str | None, fit_day: str) -> bool:
    """標籤在 fit_day 收盤時是否已完整觀測（結束日的收盤價已經出來）。"""
    return end_day is not None and end_day <= fit_day


def local_time(iso: str | None) -> tuple[str, str, str]:
    """ISO 時間 → 台北時間的（日期, HH:MM, 品質）。

    品質：ok＝有時區；naive＝沒有時區，當成台北時間；date_only＝只有日期（時間為空字串）；missing＝沒有值。
    其他時區（例如 Z／UTC）會換算成台北時間，跨日也會跟著換。
    """
    iso = (iso or "").strip()
    if not iso:
        return "", "", "missing"
    if len(iso) < 16:
        return iso[:10], "", "date_only"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10], iso[11:16], "naive"
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), "naive"
    dt = dt.astimezone(TAIPEI)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), "ok"


def entry_index(days: list[str], day: str, time: str, rule: str = ENTRY_RULE) -> int | None:
    """報酬起點是第幾個交易日的收盤。None＝那天的收盤還沒出來（等待）；−1＝早於價格資料。

    days：由舊到新的交易日。day、time：資訊可取得的台北日期與 HH:MM（time 可空白）。
    rule：close（預設，可成交）、legacy（舊規則，只供比較）、given（day 本身就是起點交易日，給已經算好起點的樣本用）。
    """
    if rule not in ENTRY_RULES:
        raise ValueError(f"進場規則只能是 {'／'.join(ENTRY_RULES)}")
    if not days or day > days[-1]:
        return None
    if rule == "legacy":
        after = (time or "08:00") >= OPEN_TIME
        i = bisect.bisect_left(days, day) - 1   # 最後一個早於新聞日期的交易日
        if after and i + 1 < len(days) and days[i + 1] == day:
            i += 1
        return i
    if day < days[0]:
        return -1
    i = bisect.bisect_left(days, day)           # 第一個不早於這天的交易日
    if rule == "given":
        return i if days[i] == day else -1
    if days[i] == day and not (time and time <= ENTRY_CUTOFF):
        i += 1                                  # 交易日 13:00 以後或沒有時間 → 下一個交易日
    return i if i < len(days) else None


def available_at(*isos: str | None) -> tuple[str, str]:
    """幾個時間裡最晚的那個（台北日期, HH:MM）；不能解析的值（空白、"now" 之類）略過。都不能用時回傳 ("", "")。

    網站收錄的新聞：available_at(發布時間, 抓取時間[, 預測建立時間])＝系統第一次能用這則新聞的時間。
    """
    best: tuple[str, str] | None = None
    for iso in isos:
        d, t, q = local_time(iso)
        if q == "missing" or len(d) != 10 or not (d[:4].isdigit() and d[5:7].isdigit() and d[8:].isdigit()):
            continue
        if best is None or (d, t or "00:00") > (best[0], best[1] or "00:00"):
            best = (d, t)
    return best or ("", "")
