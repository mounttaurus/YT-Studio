"""backgrounds.json の全エントリに光源メタ（light_dx / light_mean）を補完する.

新規登録分（register_backgrounds.py）は最初から持っているが、既存エントリには
無いため後追いで計算する。Photoshop 不要・冪等（既にあるものは飛ばす）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np
from PIL import Image


def light_metrics(path: str) -> tuple[float | None, float | None]:
    """左右3分割の輝度差（正=右が明るい）と平均輝度。透明画素は除外する。"""
    im = Image.open(path)
    a = np.asarray(im.convert("RGBA")).astype(float)
    alpha = a[:, :, 3] > 128
    if alpha.mean() < 0.05:
        return None, None
    lum = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    t = max(1, lum.shape[1] // 3)

    def band(sl):
        m = alpha[:, sl]
        return float(lum[:, sl][m].mean()) if m.sum() > 50 else None

    left, right = band(slice(0, t)), band(slice(-t, None))
    mean = round(float(lum[alpha].mean()), 1)
    if left is None or right is None:
        return None, mean
    return round(float((right - left) / max(1.0, (left + right) / 2)), 3), mean


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True)
    ap.add_argument("--force", action="store_true", help="既にある値も測り直す")
    args = ap.parse_args()

    index_path = os.path.join(args.archive, "backgrounds.json")
    with open(index_path, encoding="utf-8") as fh:
        index = json.load(fh)

    done = skipped = missing = 0
    directional = 0
    for b in index["backgrounds"]:
        if not args.force and b.get("light_dx") is not None:
            skipped += 1
            continue
        path = os.path.join(args.archive, b["image"].replace("/", os.sep))
        if not os.path.exists(path):
            missing += 1
            continue
        dx, mean = light_metrics(path)
        b["light_dx"] = dx
        b["light_mean"] = mean
        done += 1
        if dx is not None and abs(dx) >= 0.08:
            directional += 1

    shutil.copyfile(index_path, index_path + ".bak")
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)

    print("補完 %d 件 / 既存のまま %d 件 / 実体なし %d 件" % (done, skipped, missing))
    print("うち明確な方向性(|dx|>=0.08)を持つもの: %d 件" % directional)
    have = [b for b in index["backgrounds"] if b.get("light_dx") is not None]
    strong = [b for b in have if abs(b["light_dx"]) >= 0.08]
    print("索引全体: %d 件中 %d 件が光源メタあり、%d 件が方向性あり"
          % (len(index["backgrounds"]), len(have), len(strong)))


if __name__ == "__main__":
    main()
