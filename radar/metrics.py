"""評估指標的唯一來源。validate／scoring／train 都用 report() 算分，分母與基準因此一致。

輸入是每筆樣本的「預測」（up／down／flat，flat＝沒有發訊號）與「實際」（radar/targets.py 的方向；
None＝不能評分，另給原因）。輸出同時包含兩種常見的看法，名稱分開，不要混用：

- hit_rate（命中率，沿用舊定義）：只看有發訊號（預測不是 flat）的樣本；實際 flat 留在分母、算沒命中。
  旁邊的 always_up／always_down 基準用「同一批有發訊號的樣本」全部猜漲／全部猜跌。
- 三分類（down／flat／up）：所有有標籤的樣本都在分母，預測 flat 也是一種預測。
  報 accuracy、balanced accuracy、macro F1、每類 precision／recall、混淆矩陣，
  以及同一批樣本上「全猜 up／down／flat／多數類」的基準。

coverage＝有發訊號的筆數 ÷ 有標籤的筆數。excluded 依原因列出沒有標籤的筆數。
"""
from __future__ import annotations

from collections import Counter

from .targets import CLASSES


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _confusion(pairs: list[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """confusion[實際][預測]。"""
    conf = {a: {p: 0 for p in CLASSES} for a in CLASSES}
    for p, a in pairs:
        conf[a][p] += 1
    return conf


def _core(pairs: list[tuple[str, str]]) -> dict:
    """pairs：(預測, 實際)，實際一定有值。"""
    n = len(pairs)
    conf = _confusion(pairs)
    per_class, recalls, f1s = {}, [], []
    for c in CLASSES:
        support = sum(conf[c].values())
        predicted = sum(conf[a][c] for a in CLASSES)
        prec, rec = _ratio(conf[c][c], predicted), _ratio(conf[c][c], support)
        f1 = (2 * prec * rec / (prec + rec) if prec and rec else 0.0) if support else None
        per_class[c] = {"support": support, "predicted": predicted, "precision": prec, "recall": rec, "f1": f1}
        if support:
            recalls.append(rec)
            f1s.append(f1)
    sig = [(p, a) for p, a in pairs if p != "flat"]
    return {
        "accuracy": _ratio(sum(p == a for p, a in pairs), n),
        "balanced_accuracy": _mean(recalls),
        "macro_f1": _mean(f1s),
        "hit_rate": _ratio(sum(p == a for p, a in sig), len(sig)),
        "hits": sum(p == a for p, a in sig),
        "n_signal": len(sig),
        "per_class": per_class,
        "confusion": conf,
    }


def report(preds: list[str], acts: list[str | None], reasons: list[str | None] | None = None) -> dict:
    """preds、acts 一一對應；acts 為 None 的樣本不評分，原因取 reasons（沒給就記 no_label）。"""
    if len(preds) != len(acts) or (reasons is not None and len(reasons) != len(acts)):
        raise ValueError("preds、acts、reasons 的長度要一樣")
    bad = {p for p in preds if p not in CLASSES} | {a for a in acts if a is not None and a not in CLASSES}
    if bad:
        raise ValueError(f"不認得的方向：{bad}")
    excluded: Counter = Counter()
    pairs = []
    for i, (p, a) in enumerate(zip(preds, acts)):
        if a is None:
            excluded[(reasons[i] if reasons else None) or "no_label"] += 1
        else:
            pairs.append((p, a))
    n = len(pairs)
    core = _core(pairs)
    share = {c: _ratio(sum(a == c for _, a in pairs), n) for c in CLASSES}
    majority = max(CLASSES, key=lambda c: (share[c] or 0, c == "flat")) if n else None
    sig_acts = [a for p, a in pairs if p != "flat"]

    def const(c: str) -> dict:
        b = _core([(c, a) for _, a in pairs])
        return {k: b[k] for k in ("accuracy", "balanced_accuracy", "macro_f1")}

    baselines = {f"always_{c}": const(c) for c in CLASSES}
    baselines["always_up"]["hit_rate_on_signals"] = _ratio(sig_acts.count("up"), len(sig_acts))
    baselines["always_down"]["hit_rate_on_signals"] = _ratio(sig_acts.count("down"), len(sig_acts))
    baselines["majority"] = {"class": majority, **(const(majority) if majority else {})}
    return {
        "n_total": len(preds), "n_labeled": n, "n_signal": core["n_signal"],
        "coverage": _ratio(core["n_signal"], n),
        "excluded": dict(excluded),
        "actual_share": share,
        **{k: core[k] for k in ("hit_rate", "hits", "accuracy", "balanced_accuracy", "macro_f1", "per_class", "confusion")},
        "baselines": baselines,
    }


def brief(m: dict) -> str:
    """一行文字摘要（指令列印出用）。"""
    pc = lambda x: "—" if x is None else f"{x * 100:.0f}%"
    s = m["actual_share"]
    b = m["baselines"]
    maj = {"up": "漲", "down": "跌", "flat": "持平", None: "—"}[b["majority"]["class"]]
    return (f"有標籤 {m['n_labeled']}／發訊號 {m['n_signal']}（coverage {pc(m['coverage'])}）"
            f"　實際 漲 {pc(s['up'])}・持平 {pc(s['flat'])}・跌 {pc(s['down'])}"
            f"　命中率 {pc(m['hit_rate'])}（同批全猜漲 {pc(b['always_up']['hit_rate_on_signals'])}、"
            f"全猜跌 {pc(b['always_down']['hit_rate_on_signals'])}）"
            f"　三分類 balanced acc {pc(m['balanced_accuracy'])}、macro F1 {pc(m['macro_f1'])}"
            f"（全猜{maj} "
            f"{pc(b['majority'].get('balanced_accuracy'))}、{pc(b['majority'].get('macro_f1'))}）")
