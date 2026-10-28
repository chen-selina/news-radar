"""五個新聞來源，每個來源一個模組，都提供 list_items(limit) → list[ListItem]。

要開關某個來源，改 config/crawl.json 的 enabled。
"""
from __future__ import annotations

from types import ModuleType

from . import bw, cw, google, udn, yahoo

FETCHERS: dict[str, ModuleType] = {"yahoo": yahoo, "google": google, "udn": udn, "bw": bw, "cw": cw}
