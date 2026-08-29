"""切り抜きPNGから視覚指紋 ``dhash+shape_rel/1`` を計算する.

採用の経緯は ``psassist/Docs/CHARACTER_CUTOUT_PLAN.md`` §9。196枚・ユーザー判定80件で
較正し、AUC 0.96 / 閾値 0.073。距離計算は ``cutout_selector.distance`` が持つ（本籍はあちら）。

⚠️ **``psassist/scripts/fingerprint.py`` と1ビットも違ってはいけない。** 既存194枚の指紋は
ホスト側のあの実装で計算されて ``library.json`` に入っている。値がずれると新旧を比較できず、
在庫の重複判定が静かに壊れる。``tests/test_fingerprint.py`` が実データ由来の期待値で固定する。

ホスト側は8信号を計算するが、ここは**採用した2つだけ**を移植した
（``head`` は首の検出＝``measure_shot`` が要るが、使わないので依存ごと不要）。
"""

from __future__ import annotations

import numpy as np
from PIL import Image

VERSION = "dhash+shape_rel/1"
ALPHA_TH = 128
GRAY_BG = 128   # 透過部を埋める中間グレー。白/黒にすると輪郭が強調され過ぎる
_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _resize(a: np.ndarray, w: int, h: int) -> np.ndarray:
    """BOX＝面積平均で縮小する。占有率がそのまま出るのでシルエットに向く。

    ⚠️ ``astype(np.uint8)`` は切り捨て（四捨五入ではない）。ホスト側と同じ挙動にするため
    ここを ``np.round`` に「直して」はいけない。
    """
    return np.asarray(Image.fromarray(a.astype(np.uint8), "L").resize((w, h), Image.BOX))


def _bits_to_hex(bits: np.ndarray) -> str:
    return np.packbits(bits.astype(np.uint8).ravel()).tobytes().hex()


def _u8_to_hex(a: np.ndarray) -> str:
    return a.astype(np.uint8).tobytes().hex()


def compute(img: Image.Image) -> dict:
    """透過PNGから指紋を返す。アルファが空なら ``{"empty": True}``。"""
    arr = np.asarray(img.convert("RGBA"))
    rgb = arr[:, :, :3].astype(np.float32)
    alpha = arr[:, :, 3]
    mask = alpha > ALPHA_TH
    if not mask.any():
        return {"empty": True}

    # 透過部を中間グレーで埋めた合成。輝度系はここから作る
    a = (alpha.astype(np.float32) / 255.0)[:, :, None]
    gray = (rgb * a + GRAY_BG * (1 - a)) @ _LUMA

    # dhash: 9x8 に潰して横方向の明暗差＝構図の大枠
    g9 = _resize(gray, 9, 8).astype(np.float32)
    dhash = _bits_to_hex(g9[:, 1:] > g9[:, :-1])

    # shape_rel: bbox に正規化＝画面のどこに居るかを捨てて形だけ見る。
    # 「寸法を捨てたポーズ・構図の記述子」であることが採用理由（§9-8）
    ys, xs = np.where(mask)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    m255 = (mask * 255).astype(np.uint8)
    shape_rel = _u8_to_hex(_resize(m255[y0 : y1 + 1, x0 : x1 + 1], 16, 16))

    return {
        "empty": False,
        "version": VERSION,
        "dhash": dhash,
        "shape_rel": shape_rel,
        "bbox": [x0, y0, x1, y1],
        "coverage": round(float(mask.mean()), 4),
    }


def for_entry(img: Image.Image) -> dict:
    """``library.json`` の ``fingerprint`` フィールドに入れる形（既存194件と同じキー）。"""
    fp = compute(img)
    if fp.get("empty"):
        return {"version": VERSION, "dhash": None, "shape_rel": None}
    return {"version": VERSION, "dhash": fp["dhash"], "shape_rel": fp["shape_rel"]}
