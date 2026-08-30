"""切り抜きPNGから視覚指紋の候補を複数まとめて計算する.

`CHARACTER_CUTOUT_PLAN.md` §7-1 の宿題1。**どれが人間の「使い回しに見える」と
相関するかは未確定**なので、安い候補を全部並べて測り、後段(`score_pairs.py`)で
ユーザー判定と突き合わせて採否を決める。光源判定(`analyze_side.py`)・
ショット判定(`measure_shot.py`)と同じ手順。

依存は numpy と Pillow だけ（ホストに torch / scipy は無い）。

候補となる信号:
  dhash      隣接画素の明暗差ハッシュ。構図の大枠
  phash      DCT低周波ハッシュ。dhashより頑健とされる
  shape_rel  bboxに正規化したアルファ占有(16x16)。**位置に依らないシルエット**
  shape_abs  画面全体のアルファ占有(16x32)。**配置が同じか**を見る
  color      マスク内のRGBヒストグラム(4x4x4)。衣装・照明・色調
  head       頭部だけを切り出したグレースケール(16x16)。**視聴者が一番見る所**
  light_dx   左右の輝度差(mask_statsから引き継ぎ)。単独では弱いが合成に使う

⚠️ 距離は全て 0..1 に正規化して返す。信号ごとにスケールが違うと比較にならない。
"""

from __future__ import annotations

import argparse
import glob

import json
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ⚠️ measure_shot は import 時に sys.stdout を差し替える。元の stdout を掴んでおかないと
#    GC で閉じられ、以後の print が「I/O operation on closed file」で落ちる。
_keep_stdout = sys.stdout
from measure_shot import head_height  # 首の検出は1つの本籍から使う

sys.stdout.reconfigure(encoding="utf-8")

ALPHA_TH = 128
GRAY_BG = 128  # 透過部を埋める中間グレー。ここを白/黒にすると輪郭が強調され過ぎる


# --------------------------------------------------------------------------- 基本部品


def _dct_matrix(n: int) -> np.ndarray:
    """DCT-II の変換行列。scipy が無いので自前で作る（n=32 なら一瞬）。"""
    k = np.arange(n).reshape(-1, 1)
    i = np.arange(n).reshape(1, -1)
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n))
    m[0] *= np.sqrt(0.5)
    return m * np.sqrt(2.0 / n)


_DCT32 = _dct_matrix(32)


def _bits_to_hex(bits: np.ndarray) -> str:
    return np.packbits(bits.astype(np.uint8).ravel()).tobytes().hex()


def _hex_to_bits(h: str) -> np.ndarray:
    return np.unpackbits(np.frombuffer(bytes.fromhex(h), dtype=np.uint8))


def _u8_to_hex(a: np.ndarray) -> str:
    return a.astype(np.uint8).tobytes().hex()


def _hex_to_u8(h: str) -> np.ndarray:
    return np.frombuffer(bytes.fromhex(h), dtype=np.uint8).astype(np.float32)


def _resize(a: np.ndarray, w: int, h: int, mode: str = "L") -> np.ndarray:
    """numpy配列を縮小する。BOX＝面積平均なので占有率がそのまま出る。"""
    return np.asarray(Image.fromarray(a.astype(np.uint8), mode).resize((w, h), Image.BOX))


# --------------------------------------------------------------------------- 指紋の計算


def fingerprint(path: str, stats: dict | None) -> dict:
    im = Image.open(path).convert("RGBA")
    arr = np.asarray(im)
    rgb = arr[:, :, :3].astype(np.float32)
    alpha = arr[:, :, 3]
    mask = alpha > ALPHA_TH
    if not mask.any():
        return {"empty": True}

    # 透過部を中間グレーで埋めた合成。ここから輝度系を全部作る
    a = (alpha.astype(np.float32) / 255.0)[:, :, None]
    flat = rgb * a + GRAY_BG * (1 - a)
    gray = flat @ np.array([0.299, 0.587, 0.114], dtype=np.float32)

    # dhash: 9x8 に潰して横方向の明暗差
    g9 = _resize(gray, 9, 8).astype(np.float32)
    dhash = _bits_to_hex(g9[:, 1:] > g9[:, :-1])

    # phash: 32x32 の DCT 低周波 8x8（DC を除いた中央値で2値化）
    g32 = _resize(gray, 32, 32).astype(np.float32)
    dct = _DCT32 @ g32 @ _DCT32.T
    low = dct[:8, :8].copy()
    med = np.median(np.delete(low.ravel(), 0))
    phash = _bits_to_hex(low > med)

    ys, xs = np.where(mask)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())

    # shape_rel: bbox に正規化＝画面のどこに居るかを捨てて形だけ見る
    m255 = (mask * 255).astype(np.uint8)
    shape_rel = _u8_to_hex(_resize(m255[y0 : y1 + 1, x0 : x1 + 1], 16, 16))
    # shape_abs: 画面全体のまま＝配置が同じかどうかも含む
    shape_abs = _u8_to_hex(_resize(m255, 32, 16))

    # color: マスク内だけの RGB ヒストグラム 4x4x4
    q = (rgb[mask] / 64).astype(np.int32).clip(0, 3)
    idx = q[:, 0] * 16 + q[:, 1] * 4 + q[:, 2]
    hist = np.bincount(idx, minlength=64).astype(np.float32)
    hist /= max(1.0, hist.sum())

    # head: 頭部だけを切り出す。首が取れない絵は bbox 上部で代替する
    hh = head_height(mask)
    hx = (stats or {}).get("head_bbox_x")
    hx0, hx1 = (int(hx[0]), int(hx[1])) if hx else (x0, x1)
    if hh is not None:
        hy0, hy1 = int(hh["top"]), int(hh["neck"])
        head_src = "neck"
    else:
        hy0, hy1 = y0, y0 + max(8, int((y1 - y0 + 1) * 0.28))
        head_src = "fallback"
    hy1 = max(hy0 + 8, min(hy1, gray.shape[0] - 1))
    hx1 = max(hx0 + 8, min(hx1, gray.shape[1] - 1))
    head = _u8_to_hex(_resize(gray[hy0 : hy1 + 1, hx0 : hx1 + 1], 16, 16))

    return {
        "empty": False,
        "dhash": dhash,
        "phash": phash,
        "shape_rel": shape_rel,
        "shape_abs": shape_abs,
        "color": [round(float(v), 5) for v in hist],
        "head": head,
        "head_src": head_src,
        "head_box": [hx0, hy0, hx1, hy1],
        "bbox": [x0, y0, x1, y1],
        "coverage": round(float(mask.mean()), 4),
        "light_dx": (stats or {}).get("light_dx"),
    }


# --------------------------------------------------------------------------- 距離


def _ham(a: str, b: str) -> float:
    return float((_hex_to_bits(a) != _hex_to_bits(b)).mean())


def _u8_l1(a: str, b: str) -> float:
    x, y = _hex_to_u8(a), _hex_to_u8(b)
    return float(np.abs(x - y).mean() / 255.0)


def distances(p: dict, q: dict) -> dict[str, float]:
    """0=同一, 1=最大。信号ごとにスケールを揃えてある。"""
    d = {
        "dhash": _ham(p["dhash"], q["dhash"]),
        "phash": _ham(p["phash"], q["phash"]),
        "shape_rel": _u8_l1(p["shape_rel"], q["shape_rel"]),
        "shape_abs": _u8_l1(p["shape_abs"], q["shape_abs"]),
        "color": float(np.abs(np.array(p["color"]) - np.array(q["color"])).sum() / 2.0),
        "head": _u8_l1(p["head"], q["head"]),
    }
    a, b = p.get("light_dx"), q.get("light_dx")
    d["light"] = float(min(1.0, abs(a - b) / 2.0)) if a is not None and b is not None else 0.0
    # 合成の当て馬。採否は score_pairs.py の結果で決める（今は等重み）
    d["combo"] = float(np.mean([d["phash"], d["shape_rel"], d["color"], d["head"]]))
    return d


SIGNALS = ["dhash", "phash", "shape_rel", "shape_abs", "color", "head", "light", "combo"]


# --------------------------------------------------------------------------- CLI


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutout", required=True)
    ap.add_argument("--stats", default=None, help="mask_stats.json（light_dx / head_bbox_x を借りる）")
    ap.add_argument("--aroll", default=None, help="aroll.json（char_id / slot_key を借りる）")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    stats = {}
    if args.stats and os.path.exists(args.stats):
        with open(args.stats, encoding="utf-8") as fh:
            stats = json.load(fh)
    meta = {}
    if args.aroll and os.path.exists(args.aroll):
        with open(args.aroll, encoding="utf-8") as fh:
            for p in json.load(fh)["panels"]:
                meta[p["line_id"]] = {
                    "characters": p.get("characters") or [],
                    "slot_key": p.get("slot_key"),
                    "slot": p.get("slot") or {},
                    "speaker_name": p.get("speaker_name"),
                }

    out, skipped, fallback = {}, [], 0
    files = sorted(glob.glob(os.path.join(args.cutout, "panel_line_*.png")))
    for i, f in enumerate(files, 1):
        lid = os.path.basename(f)[len("panel_") : -len(".png")]
        fp = fingerprint(f, stats.get(lid))
        if fp.get("empty"):
            skipped.append(lid)
            continue
        if fp["head_src"] == "fallback":
            fallback += 1
        fp.update(meta.get(lid, {}))
        out[lid] = fp
        if i % 40 == 0:
            print("  ... %d/%d" % (i, len(files)))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"signals": SIGNALS, "items": out}, fh, ensure_ascii=False)

    print("指紋 %d 枚（空 %d / 頭部は代替枠 %d 枚）" % (len(out), len(skipped), fallback))
    print("→ %s" % args.out)


if __name__ == "__main__":
    main()
