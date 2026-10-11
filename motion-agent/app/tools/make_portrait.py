"""見本用の「架空の人物の古い肖像写真」（自作・実在の人物ではない）を描く。

HyperFrames の見本「人物の初出」（hyperframes/samples/person_intro）が使う。顔のない胸から上の人影に、
古い写真の質感（セピア・粒子・周辺減光・傷）を付ける。乱数は種で固定する＝同じ種なら同じ画像（M2）。

    python -m app.tools.make_portrait [出力先] [種]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 1200, 1500


def _figure() -> Image.Image:
    img = Image.new("L", (W, H), 0)
    # 背景: 写真館の幕のような、左上が明るい縦のグラデーション
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    bg = 206 - 0.04 * yy - 0.02 * xx + 16 * np.exp(-(((xx - 380) / 520) ** 2 + ((yy - 420) / 640) ** 2))
    img = Image.fromarray(np.clip(bg, 0, 255).astype(np.uint8), "L")
    d = ImageDraw.Draw(img)
    # 上着（肩）
    d.polygon([(110, H), (160, 1180), (330, 1040), (520, 990), (680, 990), (870, 1040), (1040, 1180), (1090, H)], fill=46)
    d.ellipse((120, 1040, 470, 1400), fill=46)
    d.ellipse((730, 1040, 1080, 1400), fill=46)
    # 襟と白いシャツ・ネクタイ
    d.polygon([(500, 990), (600, 1240), (700, 990)], fill=214)
    d.polygon([(470, 1000), (560, 1260), (600, 1240), (520, 985)], fill=70)
    d.polygon([(730, 1000), (640, 1260), (600, 1240), (680, 985)], fill=70)
    d.polygon([(578, 1040), (622, 1040), (640, 1300), (600, 1340), (560, 1300)], fill=30)
    # 首
    d.rectangle((520, 820, 680, 1010), fill=88)
    # 頭（顔は描かない＝匿名の人物）。左から光が当たる陰影は下で付ける
    d.ellipse((400, 330, 800, 860), fill=118)
    d.ellipse((385, 560, 430, 680), fill=108)   # 耳
    d.ellipse((770, 560, 815, 680), fill=96)
    # 髪（撫でつけた短髪）
    d.chord((392, 296, 808, 720), 180, 360, fill=34)
    d.polygon([(400, 500), (420, 430), (470, 520)], fill=38)
    return img


def draw(seed: int = 1963) -> Image.Image:
    rng = np.random.default_rng(seed)
    base = np.asarray(_figure().filter(ImageFilter.GaussianBlur(3.2)), dtype=np.float32)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    # 左からの光（右側を少し落とす）と周辺減光
    light = 1.0 - 0.18 * (xx / W)
    r = np.sqrt(((xx - W * 0.48) / (W * 0.62)) ** 2 + ((yy - H * 0.45) / (H * 0.62)) ** 2)
    vignette = np.clip(1.15 - 0.55 * r ** 2, 0.25, 1.0)
    img = base * light * vignette
    # フィルムの粒子
    img += rng.normal(0.0, 9.0, size=img.shape)
    # 縦の細い傷を数本
    for _ in range(7):
        x = int(rng.integers(40, W - 40))
        y0, y1 = sorted(int(v) for v in rng.integers(0, H, size=2))
        img[y0:y1, x:x + 2] += float(rng.choice([-38.0, 46.0]))
    gray = np.clip(img, 0, 255)
    # セピアに色付け
    rgb = np.stack([gray * 1.07 + 10, gray * 0.95 + 4, gray * 0.78], axis=-1)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("portrait.png")
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1963
    out.parent.mkdir(parents=True, exist_ok=True)
    draw(seed).save(out, optimize=True)
    print(out)


if __name__ == "__main__":
    main()
