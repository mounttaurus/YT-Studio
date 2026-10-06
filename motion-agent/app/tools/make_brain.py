"""見本用の脳のシルエット（自作・権利の問題なし）を Remotion の public/samples/brain.png に描く。

C1_particle_morph の `shape: image`（暗い部分から点群を取る）の見本。白地に黒い脳（左右の半球・小脳・脳幹）
と、白い溝。実在の脳の図（解剖図など）は、出典の分かるものをユーザーが用意する。

    python -m app.tools.make_brain [出力先]
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from app.core.config import REMOTION_DIR

W, H = 1000, 800


def draw() -> Image.Image:
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    # 左右の半球（大きな楕円を少し重ねる）。上が丸く、下がやや平ら
    d.ellipse((90, 90, 520, 560), fill=0)
    d.ellipse((480, 90, 910, 560), fill=0)
    d.ellipse((200, 40, 800, 470), fill=0)
    # 側頭葉のふくらみ
    d.ellipse((130, 360, 520, 640), fill=0)
    d.ellipse((480, 360, 870, 640), fill=0)
    # 小脳と脳幹
    d.ellipse((330, 560, 500, 710), fill=0)
    d.ellipse((500, 560, 670, 710), fill=0)
    d.polygon([(440, 520), (560, 520), (535, 770), (465, 770)], fill=0)
    # 溝: 中央の縦の溝と、半球ごとの曲がった溝（白）
    rng = random.Random(11)
    d.line([(500, 70), (505, 200), (495, 330), (505, 470)], fill=255, width=9)
    for cx, sign in ((300, -1), (700, 1)):
        for k in range(7):
            y0 = 130 + k * 60 + rng.randint(-10, 10)
            pts = []
            for t in range(0, 11):
                x = cx + sign * (t - 5) * 26
                y = y0 + 22 * math.sin(t * 0.9 + k) + rng.randint(-4, 4)
                pts.append((x, y))
            d.line(pts, fill=255, width=7, joint="curve")
    # 側頭葉と前頭葉を分ける溝
    d.arc((120, 280, 520, 560), 200, 330, fill=255, width=8)
    d.arc((480, 280, 880, 560), 210, 340, fill=255, width=8)
    return img.filter(ImageFilter.GaussianBlur(1.2)).convert("RGB")


def main(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    draw().save(out)
    print(out)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else REMOTION_DIR / "public" / "samples" / "brain.png")
