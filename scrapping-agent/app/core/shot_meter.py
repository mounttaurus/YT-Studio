"""切り抜きマスクから**実際の画角（頭身）**を測る。

⚠️ **`psassist/scripts/measure_shot.py` と同じ計算でなければならない。** あちらは
その話数で生成した絵（`a_roll/panel_*.png`）を測って背景の画角合わせに使い、こちらは
**在庫**（`panel_library/cutouts/*.png`）を測ってカメラプランに使う ── 入力が違うだけで
同じ物差しを使う。値がずれると「背景は寄りに合わせたのにキャラは引き」が起きる。
`fingerprint.py` を二重に持っているのと同じ約束（コメントで縛る）。

## なぜラベルを信じないか

`shot` ラベル（LLMが付けた希望／生成時に指定した値）は実物とずれる。実測の前例:
`face_closeup` と指定した絵の実物がバストアップ寄りだった（[[slot-shot-label-unreliable]]・
[[face-extreme-shot-added]]）。寄り引きを組み立てるなら**実物の頭身**で並べる。
"""
from __future__ import annotations

import numpy as np
from PIL import Image

# 頭身 → ショット。psassist/scripts/measure_shot.py の SHOT_BY_HEADS と同一にすること。
SHOT_BY_HEADS = [
    (1.6, "face_closeup"),
    (2.8, "bust"),
    (4.0, "waist_up"),
    (5.6, "knee"),   # 語彙には無いが実在する。waist_up と wide の中間
    (99.0, "wide"),
]

# 寄り→引きの順。カメラプランが「一段変える」を計算するのに使う。
# ⚠️ `profile` は画角ではなく向きなので**ここには入れない**（orientation が扱う）。
SCALE_ORDER = ["face_closeup", "bust", "waist_up", "knee", "wide"]


def head_height(mask: np.ndarray) -> dict | None:
    """頭の高さを測る（psassist/scripts/measure_shot.py の同名関数と同一）。

    ⚠️ 単純に「上から幅の谷を探す」と**頭のてっぺん**を拾う（そこは幅がほぼ0で、
       下へ行くほど太くなるため常に最小になる）。必ず**頭のピークより下**を探すこと。
       手順: 頭頂 → 頭の最大幅の行 → そこから下で最小幅の行（＝首）。
    """
    rows = np.where(mask.any(1))[0]
    if len(rows) < 40:
        return None
    top, bottom = int(rows[0]), int(rows[-1])
    widths = mask[top: bottom + 1].sum(1).astype(float)
    n = len(widths)
    if widths.max() <= 0 or n < 40:
        return None

    head_zone = max(8, int(n * 0.35))
    head_peak = int(np.argmax(widths[:head_zone]))
    if head_peak < 3:
        return None

    lo, hi = head_peak + 2, max(head_peak + 6, int(n * 0.65))
    if hi <= lo:
        return None
    seg = widths[lo:hi]
    neck = lo + int(np.argmin(seg))
    if widths[neck] >= widths[head_peak] * 0.95:
        return None  # くびれが無い＝顔アップで首まで写っていない等

    return {"top": top, "bottom": bottom, "neck": top + neck, "head_h": neck,
            "head_w": float(widths[:head_zone].max()),
            "head_cropped": bool(mask[0, :].sum() > mask.shape[1] * 0.02)}


def classify(heads: float) -> str:
    for limit, name in SHOT_BY_HEADS:
        if heads <= limit:
            return name
    return "wide"


def measure(rgba: Image.Image) -> dict:
    """透過PNGから {"shot", "heads"} を返す。測れなければ両方 None。

    ⚠️ **測れない絵がある**のは異常ではない（顔アップで首が写っていない等）。
    その時は None を返し、呼び出し側はラベルへフォールバックすること。
    """
    mask = np.asarray(rgba.convert("RGBA"))[:, :, 3] > 128
    h = head_height(mask)
    if h is None:
        return {"shot": None, "heads": None}
    visible = h["bottom"] - h["top"] + 1
    heads = visible / max(1, h["head_h"])
    return {"shot": classify(heads), "heads": round(float(heads), 2)}


def scale_index(shot: str | None) -> int | None:
    """寄り(0)→引き(4) の段。未知は None。"""
    if shot in SCALE_ORDER:
        return SCALE_ORDER.index(shot)
    return None


def effective_shot(entry: dict) -> str | None:
    """その entry の**実物の画角**。実測を優先し、無ければラベルへ落とす。

    ⚠️ ラベルは実物と 87.5% ずれた実績がある（`used_slot` の件）。
    実測がある限りそちらを使うこと。
    """
    measured = (entry.get("measured") or {}).get("shot")
    return measured or entry.get("shot")
