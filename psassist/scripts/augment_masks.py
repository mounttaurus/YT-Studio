"""mask_stats.json に列占有プロファイルを足す（Photoshop 不要・約30秒）.

plan_builder が「バブルを避けるためにキャラを何px動かすか」を計算するのに使う。
プロファイルは 128 分割の粗いものだが、移動量の算出には十分（1bin ≒ 10.75px）。

これを持たせることで plan_builder は JSON→JSON の純粋関数のままでいられる
（画像 IO をプラン生成に持ち込まない）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np
from PIL import Image

COLS = 64
ROWS = 16


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", required=True)
    ap.add_argument("--cutout", required=True)
    args = ap.parse_args()

    with open(args.stats, encoding="utf-8") as fh:
        stats = json.load(fh)

    done = 0
    for line_id, m in stats.items():
        if m.get("error") or m.get("empty"):
            continue
        f = os.path.join(args.cutout, "panel_%s.png" % line_id)
        if not os.path.exists(f):
            continue
        a = np.asarray(Image.open(f).convert("RGBA"))[:, :, 3] > 128
        h, w = a.shape
        # ★2D の粗い占有グリッド。バブルは上から1/3にあるので、全身の列profileで
        #   判定すると肩幅・胴体を拾って過剰に「重なっている」と誤判定する。
        #   帯（y範囲）を指定して問い合わせられるよう2次元で持つ。
        xe = np.linspace(0, w, COLS + 1).astype(int)
        ye = np.linspace(0, h, ROWS + 1).astype(int)
        grid = []
        for r in range(ROWS):
            band = a[ye[r] : ye[r + 1], :]
            grid.append([round(float(band[:, xe[c] : xe[c + 1]].mean()), 3) for c in range(COLS)])
        m["grid"] = grid
        m["grid_shape"] = [ROWS, COLS]
        m.pop("col_profile", None)  # 1次元版は誤判定のもとなので捨てる

        # ★キャラに乗った照明の向き（正=右が明るい）。背景の光源と逆だと
        #   違和感が出るため、背景候補の選定に使う。キャラ画素だけで測る。
        rgb = np.asarray(Image.open(f).convert("RGB")).astype(float)
        lum = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
        xs = np.where(a.any(0))[0]
        third = max(1, (xs.max() - xs.min()) // 3)
        lsl = slice(xs.min(), xs.min() + third)
        rsl = slice(xs.max() - third, xs.max())
        lm, rm = a[:, lsl], a[:, rsl]
        if lm.sum() > 50 and rm.sum() > 50:
            left, right = lum[:, lsl][lm].mean(), lum[:, rsl][rm].mean()
            m["light_dx"] = round(float((right - left) / max(1.0, (left + right) / 2)), 3)
        else:
            m["light_dx"] = None

        # ★キャンバスのどの辺にキャラが接しているか＝生成時にそこで切れている。
        #   縮小するとその切断面が画面内へ移動して露出する（頭頂部が平らに欠ける）。
        #   接地している下辺は基点なので動かず問題にならない。
        h_, w_ = a.shape
        m["edges"] = {
            "top": bool(a[0, :].sum() > w_ * 0.02),
            "bottom": bool(a[-1, :].sum() > w_ * 0.02),
            "left": bool(a[:, 0].sum() > h_ * 0.02),
            "right": bool(a[:, -1].sum() > h_ * 0.02),
        }
        done += 1

    with open(args.stats, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, indent=1)
    print("列プロファイルを付与: %d 件 → %s" % (done, args.stats))


if __name__ == "__main__":
    main()
