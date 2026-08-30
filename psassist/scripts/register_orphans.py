"""images/ に直接置かれた未登録の背景を検出して索引に登録する.

★これが外部調達素材を追加する正規の手順（2026-08-24 変更）。
  旧手順（作業用PSDにレイヤーを足して報告）は**やめた**。理由:
  PSD は「ユーザーが便宜のため自由にリネームしてよい作業パレット」であって、
  レイヤー名を資産のIDとして信用してはいけない。実際、既存の生成物
  `psy_gradient_001` をPSD内でリネームしたものを新規素材と誤認して
  `bg_gradation-paint_001` として二重登録する事故が起きた。

★重複チェックを必ず通す。 既存の全エントリと画素で照合し、同一のものは登録しない。
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np
from PIL import Image

PREFIX_CATEGORY = {
    "loc": "location",
    "psy": "psych",
    "com": "comic",
    "eff": "effect",
    "bg": "backdrop",
}
NAME_RE = re.compile(
    r"^(loc)_[a-z0-9-]+_[a-z0-9-]+_[a-z0-9-]+_\d{3}$|^(psy|com|eff|bg)_[a-z0-9-]+_\d{3}$"
)
DUP_THRESHOLD = 3.0  # 32x18 に縮小した RGBA の平均画素差


def fingerprint(path: str) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGBA").resize((32, 18))).astype(int)


def metrics(path: str) -> dict:
    a = np.asarray(Image.open(path).convert("RGBA")).astype(float)
    alpha = a[:, :, 3] > 128
    cov = float(alpha.mean())
    out = {"coverage": round(cov, 3), "light_dx": None, "light_mean": None}
    if cov < 0.05:
        return out
    lum = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    t = max(1, lum.shape[1] // 3)

    def band(sl):
        m = alpha[:, sl]
        return float(lum[:, sl][m].mean()) if m.sum() > 50 else None

    left, right = band(slice(0, t)), band(slice(-t, None))
    out["light_mean"] = round(float(lum[alpha].mean()), 1)
    if left is not None and right is not None:
        out["light_dx"] = round(float((right - left) / max(1.0, (left + right) / 2)), 3)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True)
    ap.add_argument("--apply", action="store_true", help="実際に索引へ書き込む")
    args = ap.parse_args()

    index_path = os.path.join(args.archive, "backgrounds.json")
    images_dir = os.path.join(args.archive, "images")
    with open(index_path, encoding="utf-8") as fh:
        index = json.load(fh)
    known = {os.path.basename(b["image"]): b for b in index["backgrounds"]}

    # 既存の指紋（重複検出用）
    known_fp = {}
    for b in index["backgrounds"]:
        p = os.path.join(args.archive, b["image"].replace("/", os.sep))
        if os.path.exists(p):
            known_fp[b["bg_id"]] = fingerprint(p)

    orphans = [p for p in sorted(glob.glob(os.path.join(images_dir, "*.png")))
               if os.path.basename(p) not in known]
    if not orphans:
        print("未登録の画像はありません（索引 %d 件）" % len(index["backgrounds"]))
        return

    print("未登録の画像 %d 件\n" % len(orphans))
    to_add, skipped = [], []
    for p in orphans:
        name = os.path.splitext(os.path.basename(p))[0]
        fp = fingerprint(p)

        # ★重複チェック。これを通さないと同じ絵が別IDで二重登録される
        dup = None
        for bid, kfp in known_fp.items():
            if kfp.shape == fp.shape and float(np.abs(kfp - fp).mean()) < DUP_THRESHOLD:
                dup = bid
                break
        if dup:
            skipped.append((name, "既存 %s と同一の絵" % dup))
            continue

        if not NAME_RE.match(name):
            skipped.append((name, "命名が規約外（§4 参照）。直してから再実行"))
            continue
        prefix = name.split("_")[0]
        category = PREFIX_CATEGORY.get(prefix)
        if category is None:
            skipped.append((name, "接頭辞 %s が未知" % prefix))
            continue

        parts = name.split("_")
        m = metrics(p)
        to_add.append(
            {
                "bg_id": name,
                "category": category,
                "spot": parts[1] if category == "location" else None,
                "light": parts[2] if category == "location" else None,
                "camera": None,
                "framing": parts[3].replace("-", "_") if category == "location" else None,
                "form": parts[1] if category in ("psych", "backdrop") else None,
                "effect": parts[1] if category in ("comic", "effect") else None,
                "mood": [],
                "aspect": "16:9",
                "image": "images/%s.png" % name,
                "style": "kamishibai_bg" if category == "location" else "kamishibai_fx",
                "prompt": None,
                "provider": "external",
                "source_url": None,
                "license": None,
                "is_keyframe": False,
                "source_ref": None,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "note": "images/ 直置きから登録",
                "times_used": 0,
                "overlay": category == "effect",
                **m,
            }
        )
        known_fp[name] = fp

    print("  登録できる %d 件:" % len(to_add))
    for e in to_add:
        print("     %-34s %-9s 光源%-7s 不透明%3.0f%%"
              % (e["bg_id"], e["category"],
                 ("%+.3f" % e["light_dx"]) if e["light_dx"] is not None else "-",
                 100 * e["coverage"]))
    if skipped:
        print("\n  見送り %d 件:" % len(skipped))
        for n, why in skipped:
            print("     %-34s %s" % (n, why))

    if not args.apply:
        print("\n（--apply を付けると索引に書き込みます）")
        return
    if to_add:
        shutil.copyfile(index_path, index_path + ".bak")
        index["backgrounds"].extend(to_add)
        index["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(index_path, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=2)
        print("\n索引に %d 件追加（計 %d 件）" % (len(to_add), len(index["backgrounds"])))


if __name__ == "__main__":
    main()
