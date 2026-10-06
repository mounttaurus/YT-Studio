"""点群を作る（C1 点群の変形・B10 粒子が文字になる・将来のシルエット）。

ブラウザで毎フレーム計算しない。ここで作って props で渡す＝速く、決定的（同じ入力なら同じ点）。
座標は中心が原点、長い辺が [-1, 1] に収まる正規化座標。z は奥行き（文字・画像は薄い厚み）。

- glyph_points: 文字（日本語の書体を含む）の塗りから点を取る
- image_points: 画像のシルエット（暗い部分 or 不透明部分）から点を取る＝脳の図・人物の影など
- sphere_points / scatter_points: 球・散らばり
- order_for_morph: 形と形を対応づける並べ方（角度順）。同じ番号の点どうしが動いて変形する
"""
from __future__ import annotations

import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

Point = list[float]  # [x, y, z]


def _sample_mask(mask: np.ndarray, n: int, seed: int, depth: float) -> list[Point]:
    """二値の面（True が中身）から n 点を取り、正規化する。点が足りなければ重複して取る。"""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        raise ValueError("形が空です（文字が描けなかった・画像が真っ白など）")
    rng = random.Random(seed)
    idx = list(range(len(xs)))
    picked = rng.sample(idx, n) if len(idx) >= n else [rng.choice(idx) for _ in range(n)]
    h, w = mask.shape
    scale = 2.0 / max(w, h)
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    out = []
    for i in picked:
        x = (xs[i] + rng.random() - 0.5 - cx) * scale
        y = (ys[i] + rng.random() - 0.5 - cy) * scale
        out.append([round(x, 4), round(y, 4), round((rng.random() - 0.5) * depth, 4)])
    return out


def glyph_points(text: str, font_file: str, n: int, seed: int = 1, index: int = 0,
                 size: int = 360, depth: float = 0.08) -> list[Point]:
    """文字の塗りから点を取る。font_file は書体ファイル（.ttc は index で書体を選ぶ）。"""
    font = ImageFont.truetype(font_file, size=size, index=index)
    left, top, right, bottom = font.getbbox(text)
    pad = size // 10
    img = Image.new("L", (right - left + pad * 2, bottom - top + pad * 2), 0)
    ImageDraw.Draw(img).text((pad - left, pad - top), text, font=font, fill=255)
    return _sample_mask(np.asarray(img) > 127, n, seed, depth)


def image_points(path: str | Path, n: int, seed: int = 1, mode: str = "dark",
                 threshold: int = 128, max_side: int = 800, depth: float = 0.08) -> list[Point]:
    """画像のシルエットから点を取る。mode: dark＝暗い部分（白地の線画・影絵）／alpha＝不透明部分（切り抜き）。"""
    img = Image.open(path)
    img.thumbnail((max_side, max_side))
    if mode == "alpha":
        mask = np.asarray(img.convert("RGBA"))[:, :, 3] > threshold
    elif mode == "dark":
        mask = np.asarray(img.convert("L")) < threshold
    else:
        raise ValueError(f"mode は dark か alpha です（{mode}）")
    return _sample_mask(mask, n, seed, depth)


def sphere_points(n: int, radius: float = 0.85) -> list[Point]:
    """球面上に均等に（フィボナッチ格子）。"""
    golden = math.pi * (3 - math.sqrt(5))
    out = []
    for i in range(n):
        y = 1 - 2 * (i + 0.5) / n
        r = math.sqrt(1 - y * y)
        t = golden * i
        out.append([round(math.cos(t) * r * radius, 4), round(y * radius, 4), round(math.sin(t) * r * radius, 4)])
    return out


def scatter_points(n: int, seed: int = 1, spread: tuple[float, float, float] = (1.9, 1.1, 1.0)) -> list[Point]:
    """画面いっぱいに散らばった点（はじまり・終わりの形）。"""
    rng = random.Random(seed)
    return [[round((rng.random() * 2 - 1) * s, 4) for s in spread] for _ in range(n)]


def order_for_morph(points: list[Point]) -> list[Point]:
    """角度順（同じ角度なら中心からの距離順）に並べる。どの形もこの順に並べておけば、
    同じ番号の点どうしが近い方向にいるので、変形が渦を巻くように滑らかになる。"""
    return sorted(points, key=lambda p: (round(math.atan2(p[1], p[0]), 3), p[0] * p[0] + p[1] * p[1]))
