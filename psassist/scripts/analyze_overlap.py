"""バブルとキャラの重なりを実測し、避けるのに必要な移動量を計算する.

★AI も画像理解も要らない。196枚の透過PNG（Select Subject 済み）があるので
  「バブル矩形 ∩ キャラのアルファ」は純粋な画素演算で厳密に出る。
  必要な移動量も、バブルと反対方向へ何px動かせば重なりが消えるかを
  数えるだけで求まる。
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np
from PIL import Image

# バブル内側の「絶対に隠れてはいけない」領域は、しっぽを除いた本体部分。
# 重なりがこの割合を超えたらキャラを動かす。
OVERLAP_TOL = 0.02


def load_alpha(path: str) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGBA"))[:, :, 3] > 128


def required_shift(mask: np.ndarray, rect: list[int], side: str) -> tuple[int, float]:
    """バブル矩形からキャラを追い出すのに必要な水平移動量(px)と現在の重なり率。

    side="left"（バブルが左）なら キャラを右へ、"right" なら左へ動かす。
    """
    h, w = mask.shape
    x0, y0, x1, y1 = [max(0, rect[0]), max(0, rect[1]), min(w, rect[2]), min(h, rect[3])]
    if x1 <= x0 or y1 <= y0:
        return 0, 0.0
    band = mask[y0:y1, :]  # バブルの高さ帯だけ見る
    area = (x1 - x0) * (y1 - y0)
    cur = int(band[:, x0:x1].sum())
    if cur / area <= OVERLAP_TOL:
        return 0, cur / area

    cols = band.any(0)
    if side == "left":
        # バブルは左端側。キャラの左端が x1 より右に来るまで右へ動かす
        occupied = np.where(cols)[0]
        need = int(x1 - occupied.min()) if len(occupied) else 0
        return max(0, need), cur / area
    occupied = np.where(cols)[0]
    need = int(occupied.max() - x0) if len(occupied) else 0
    return -max(0, need), cur / area


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--cutout", required=True)
    args = ap.parse_args()

    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)

    rows = []
    for p in plan["panels"]:
        f = os.path.join(args.cutout, p["character"]["image"])
        if not os.path.exists(f):
            continue
        mask = load_alpha(f)
        shift, ratio = required_shift(mask, p["bubble"]["rect"], p["bubble"]["side"])
        h, w = mask.shape
        xs = np.where(mask.any(0))[0]
        # 動かした後、キャラが画面外へ出すぎないか（体の6割は残す）
        cw = xs.max() - xs.min() + 1
        after_out = max(0, (xs.min() + shift) * -1) + max(0, (xs.max() + shift) - (w - 1))
        rows.append(
            {
                "line_id": p["line_id"],
                "side": p["bubble"]["side"],
                "overlap": ratio,
                "shift": shift,
                "char_w": int(cw),
                "lost_ratio": after_out / cw if cw else 0.0,
            }
        )

    n = len(rows)
    hit = [r for r in rows if r["overlap"] > OVERLAP_TOL]
    print("パネル %d 枚\n" % n)
    print("■ バブル本体にキャラが重なっている: %d 枚 (%.0f%%)" % (len(hit), 100 * len(hit) / n))
    buckets = collections.Counter()
    for r in rows:
        o = r["overlap"]
        buckets["0% (重なりなし)" if o <= 0.02 else
                "〜10%" if o <= 0.10 else
                "10〜25%" if o <= 0.25 else
                "25〜50%" if o <= 0.50 else "50%超"] += 1
    for k in ("0% (重なりなし)", "〜10%", "10〜25%", "25〜50%", "50%超"):
        if buckets[k]:
            print("   %-16s %3d枚  %s" % (k, buckets[k], "#" * round(40 * buckets[k] / n)))

    if hit:
        sh = sorted(abs(r["shift"]) for r in hit)
        print("\n■ 重なりを解消するのに必要な移動量")
        print("   中央 %dpx  /  90%%点 %dpx  /  最大 %dpx"
              % (sh[len(sh) // 2], sh[int(len(sh) * 0.9)], sh[-1]))
        ok = [r for r in hit if r["lost_ratio"] < 0.15]
        print("   移動だけで解消でき、キャラの見切れも15%%未満: %d/%d 枚 (%.0f%%)"
              % (len(ok), len(hit), 100 * len(ok) / len(hit)))
        bad = [r for r in hit if r["lost_ratio"] >= 0.15]
        if bad:
            print("   移動だと見切れが大きい（縮小が要る）: %d枚  %s"
                  % (len(bad), ", ".join(r["line_id"] for r in bad[:8])))

    print("\n■ 重なりが大きい上位10枚")
    for r in sorted(rows, key=lambda x: -x["overlap"])[:10]:
        print("   %-10s side=%-5s 重なり%5.1f%%  移動%+5dpx  見切れ%4.1f%%"
              % (r["line_id"], r["side"], 100 * r["overlap"], r["shift"], 100 * r["lost_ratio"]))


if __name__ == "__main__":
    main()
