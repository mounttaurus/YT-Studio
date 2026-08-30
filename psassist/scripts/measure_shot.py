"""抜いたマスクからキャラの実際のショットサイズを測り、slot.shot と突き合わせる.

背景の画角合わせは `slot.shot`（LLMが付けたラベル）を前提にしているが、
そのラベル自体がずれている疑いが出た（panel_line_084: メタは bust だが
実際はスカート丈まで写るニーショット）。土台が狂っていれば画角合わせも狂う。

測り方: **頭身**で判定する。
  1. マスク上端から下へ走査し、幅が極小になる行＝首を探す
  2. 頭の高さ = 首 - 上端
  3. 見えている高さ ÷ 頭の高さ = 頭身
アニメ絵は概ね6.5〜7頭身なので、頭身から写っている範囲が逆算できる。
"""

from __future__ import annotations

import argparse
import collections
import glob
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np
from PIL import Image

# 頭身 → ショット。実測して調整すること（初期値は一般的なカメラワークの目安）。
SHOT_BY_HEADS = [
    (1.6, "face_closeup"),
    (2.8, "bust"),
    (4.0, "waist_up"),
    (5.6, "knee"),  # 語彙には無いが実在する。waist_up と wide の中間
    (99.0, "wide"),
]


def head_height(mask: np.ndarray) -> dict | None:
    """頭の高さを測る。

    ⚠️ 単純に「上から幅の谷を探す」と**頭のてっぺん**を拾う（そこは幅がほぼ0で、
       下へ行くほど太くなるため常に最小になる）。必ず**頭のピークより下**を探すこと。
       手順: 頭頂 → 頭の最大幅の行 → そこから下で最小幅の行（＝首）。
    """
    rows = np.where(mask.any(1))[0]
    if len(rows) < 40:
        return None
    top, bottom = int(rows[0]), int(rows[-1])
    widths = mask[top : bottom + 1].sum(1).astype(float)
    n = len(widths)
    if widths.max() <= 0 or n < 40:
        return None

    # 頭のピーク（上から35%以内で最大幅）
    head_zone = max(8, int(n * 0.35))
    head_peak = int(np.argmax(widths[:head_zone]))
    if head_peak < 3:
        return None

    # 首: ピークより下で最小幅になる行（肩で増加へ転じる手前）
    lo, hi = head_peak + 2, max(head_peak + 6, int(n * 0.65))
    if hi <= lo:
        return None
    seg = widths[lo:hi]
    neck = lo + int(np.argmin(seg))
    if widths[neck] >= widths[head_peak] * 0.95:
        return None  # くびれが無い＝顔アップで首まで写っていない等

    return {
        "top": top,
        "bottom": bottom,
        "neck": top + neck,
        "head_h": neck,  # 上端から首まで
        "head_w": float(widths[:head_zone].max()),
        "head_cropped": bool(mask[0, :].sum() > mask.shape[1] * 0.02),
    }


def classify(heads: float) -> str:
    for limit, name in SHOT_BY_HEADS:
        if heads <= limit:
            return name
    return "wide"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutout", required=True)
    ap.add_argument("--aroll", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    with open(args.aroll, encoding="utf-8") as fh:
        ar = {p["line_id"]: p for p in json.load(fh)["panels"]}

    rows, unknown = [], 0
    for f in sorted(glob.glob(os.path.join(args.cutout, "panel_line_*.png"))):
        lid = os.path.basename(f)[len("panel_") : -len(".png")]
        m = np.asarray(Image.open(f).convert("RGBA"))[:, :, 3] > 128
        h = head_height(m)
        if h is None:
            unknown += 1
            continue
        visible = h["bottom"] - h["top"] + 1
        heads = visible / max(1, h["head_h"])
        rows.append(
            {
                "line_id": lid,
                "meta_shot": (ar.get(lid, {}).get("slot") or {}).get("shot"),
                "heads": round(heads, 2),
                "measured": classify(heads),
                "head_h": h["head_h"],
                "head_w": round(h["head_w"]),
                "visible_px": visible,
                "head_cropped": h["head_cropped"],
            }
        )

    print("測定 %d 枚 / 首を検出できず %d 枚\n" % (len(rows), unknown))
    print("■ メタの shot 別に見た実測頭身")
    by = collections.defaultdict(list)
    for r in rows:
        by[r["meta_shot"]].append(r["heads"])
    for k, v in sorted(by.items(), key=lambda x: -len(x[1])):
        v = sorted(v)
        print("   %-14s N=%3d  頭身 中央%.1f  [%.1f - %.1f]" % (k, len(v), v[len(v) // 2], v[0], v[-1]))

    print("\n■ メタ vs 実測")
    agree = sum(1 for r in rows if r["meta_shot"] == r["measured"])
    print("   一致 %d / %d (%.0f%%)" % (agree, len(rows), 100 * agree / max(1, len(rows))))
    cm = collections.Counter((r["meta_shot"], r["measured"]) for r in rows)
    for (a, b), n in cm.most_common(10):
        print("   %-14s → %-14s %3d枚 %s" % (a, b, n, "OK" if a == b else ""))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({r["line_id"]: r for r in rows}, fh, ensure_ascii=False, indent=1)
        print("\n→ %s" % args.out)


if __name__ == "__main__":
    main()
