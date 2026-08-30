"""backgrounds-psy2.psd の未登録レイヤーを images/*.png へ書き出して索引に登録する.

Photoshop 不要（psd-tools でレンダリング）。既存エントリには触れない。
命名は Docs/BACKGROUND_ARCHIVE.md §4 の規約に正規化する:
    {prefix}_{form}_{nnn}   小文字・語の区切りはハイフン・連番必須

新カテゴリ:
    eff_ = effect   集中線・フォーカスライン。キャラの**後ろ・背景の前**に置く
    bg_  = backdrop ハーフトーン/グラデーション。部屋背景の代替・場面転換用
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import numpy as np
from PIL import Image
from psd_tools import PSDImage

# レイヤー名 → 正規化した bg_id / カテゴリ / 説明
MAPPING: dict[str, tuple[str, str, str]] = {
    "eff_Focus_strong": ("eff_focus-strong_001", "effect", "集中線・強"),
    "eff_Focus_middle": ("eff_focus-middle_001", "effect", "集中線・中"),
    "eff_Focus_long": ("eff_focus-long_001", "effect", "集中線・長"),
    "eff_Focus_tin": ("eff_focus-thin_001", "effect", "集中線・細"),
    "eff_Focus_bold": ("eff_focus-bold_001", "effect", "集中線・太"),
    "psy_Swirl": ("psy_swirl_001", "psych", "渦"),
    "psy_Swirl&Sun": ("psy_swirl-sun_001", "psych", "渦＋光芒"),
    "com_Swirl&Harftone": ("com_swirl-halftone_001", "comic", "渦＋網点"),
    "com_Swirl_Yellow": ("com_swirl-yellow_001", "comic", "渦・黄"),
    "BG_Grunge_Yellow": ("bg_grunge-yellow_001", "backdrop", "グランジ・黄"),
    "BG_Grunge_Orange": ("bg_grunge-orange_001", "backdrop", "グランジ・橙"),
    # ⚠️ BG_Gradation-Paint は登録しない。ユーザーが作業用PSD内で psy_gradient_001
    #    （こちらが生成物の正本）を便宜的にリネームしたものだった。実体は同一
    #    （RGB平均差 1.5）。**PSDのレイヤー名を資産のIDとして信用してはいけない**
    #    という教訓（2026-08-24）。以後 §外部調達フローは images/ 直置きへ変更。
    "BG_Gradation-Blur": ("bg_gradation-blur_001", "backdrop", "グラデーション・ぼかし"),
    "BG_HarfTone": ("bg_halftone_001", "backdrop", "ハーフトーン"),
}


def render(layer, width: int, height: int) -> Image.Image | None:
    """非表示レイヤーを 1376x768 のキャンバスへ描き出す。

    ⚠️ composite() は**非表示レイヤーに対しアルファ0を返す**（force=True でも）。
       実データは numpy() にしか無いので、そちらを取ってキャンバスへ手で置く。
    ⚠️ アルファは捨てない。eff_ は「暗い線＋透明背景」で、キャラの後ろ・背景の
       前に重ねて使う＝透過が無いと背景を隠してしまう。
    """
    n = layer.numpy()  # (h, w, C) float 0-1、レイヤー自身の bbox サイズ
    if n is None or n.size == 0:
        return None
    if n.shape[2] == 3:  # アルファが無ければ不透明として扱う
        n = np.dstack([n, np.ones(n.shape[:2])])
    canvas = np.zeros((height, width, 4), dtype=float)

    x0, y0 = layer.bbox[0], layer.bbox[1]
    sx0, sy0 = max(0, -x0), max(0, -y0)
    dx0, dy0 = max(0, x0), max(0, y0)
    w = min(n.shape[1] - sx0, width - dx0)
    h = min(n.shape[0] - sy0, height - dy0)
    if w <= 0 or h <= 0:
        return None
    canvas[dy0 : dy0 + h, dx0 : dx0 + w] = n[sy0 : sy0 + h, sx0 : sx0 + w]
    return Image.fromarray((np.clip(canvas, 0, 1) * 255).astype(np.uint8), "RGBA")


def light_metrics(img: Image.Image) -> tuple[float | None, float | None, float]:
    """左右の輝度差（正=右が明るい）・平均輝度・不透明率。

    透明画素を混ぜると意味が壊れるので、アルファのある画素だけで測る。
    """
    a = np.asarray(img.convert("RGBA")).astype(float)
    alpha = a[:, :, 3] > 128
    coverage = float(alpha.mean())
    if coverage < 0.05:
        return None, None, round(coverage, 3)
    lum = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    t = max(1, lum.shape[1] // 3)

    def band(sl) -> float | None:
        m = alpha[:, sl]
        return float(lum[:, sl][m].mean()) if m.sum() > 50 else None

    left, right = band(slice(0, t)), band(slice(-t, None))
    if left is None or right is None:
        return None, round(float(lum[alpha].mean()), 1), round(coverage, 3)
    denom = max(1.0, (left + right) / 2)
    return (
        round(float((right - left) / denom), 3),
        round(float(lum[alpha].mean()), 1),
        round(coverage, 3),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--psd", required=True)
    ap.add_argument("--archive", required=True, help="shared/backgrounds ディレクトリ")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    index_path = os.path.join(args.archive, "backgrounds.json")
    images_dir = os.path.join(args.archive, "images")
    with open(index_path, encoding="utf-8") as fh:
        index = json.load(fh)
    known = {b["bg_id"] for b in index["backgrounds"]}

    psd = PSDImage.open(args.psd)
    added, skipped = [], []
    for layer in psd:
        # ⚠️ レイヤー名に末尾の空白が混じっていることがある（3件実在した）
        name = layer.name.strip()
        entry = MAPPING.get(name)
        if entry is None:
            skipped.append((name, "対応表に無い（既存 or 未定義）"))
            continue
        bg_id, category, note = entry
        if bg_id in known:
            skipped.append((name, "登録済み"))
            continue

        img = render(layer, psd.width, psd.height)
        if img is None:
            skipped.append((name, "レンダリング失敗"))
            continue
        light_dx, light_mean, coverage = light_metrics(img)

        out = os.path.join(images_dir, "%s.png" % bg_id)
        if not args.dry_run:
            img.save(out, "PNG")
        index["backgrounds"].append(
            {
                "bg_id": bg_id,
                "category": category,
                "spot": None,
                "light": None,
                "camera": None,
                "framing": None,
                "form": bg_id.split("_")[1] if category in ("psych", "backdrop") else None,
                "effect": bg_id.split("_")[1] if category in ("comic", "effect") else None,
                "mood": [],
                "aspect": "16:9",
                "image": "images/%s.png" % bg_id,
                "style": "kamishibai_fx",
                "prompt": None,
                "provider": "vecteezy",
                "source_url": None,
                "license": None,
                "is_keyframe": False,
                "source_ref": None,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "note": note + "（backgrounds-psy2.psd から登録／外部調達）",
                "times_used": 0,
                # 光源メタ: キャラの陰影と背景の明暗方向が逆だと違和感が出るため、
                # 背景候補の並べ替えに使う（正=右が明るい）
                "light_dx": light_dx,
                "light_mean": light_mean,
                # 不透明率。低いものは「重ねて使う」素材＝キャラの後ろ・背景の前
                "coverage": coverage,
                "overlay": category == "effect",
            }
        )
        added.append((name, bg_id, light_dx, light_mean, coverage))

    print("追加 %d 件 / スキップ %d 件%s\n" % (len(added), len(skipped), "（dry-run）" if args.dry_run else ""))
    print("  %-22s %-26s %7s %6s %7s" % ("レイヤー名", "bg_id", "光源", "輝度", "不透明率"))
    for name, bg_id, dx, mean, cov in added:
        print("  %-22s %-26s %7s %6s %6.0f%%"
              % (name, bg_id, "%+.3f" % dx if dx is not None else "-",
                 "%.0f" % mean if mean is not None else "-", 100 * cov))
    if skipped:
        print("\n  スキップ:")
        for name, why in skipped:
            print("    %-34s %s" % (name, why))

    if args.dry_run:
        return
    shutil.copyfile(index_path, index_path + ".bak")
    index["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    print("\n索引を更新: %s（旧版は .bak に退避）" % index_path)
    print("合計 %d 件" % len(index["backgrounds"]))


if __name__ == "__main__":
    main()
