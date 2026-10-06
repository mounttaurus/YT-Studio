"""文字の実寸の測定（安全域の検査用）。Pillow で書体ファイルを使って測る＝描画前に分かる。"""
from __future__ import annotations

from functools import lru_cache

from PIL import ImageFont

SAFE_W = 1728  # 画面幅 1920 の 90%（型 v1 の約束と同じ）


@lru_cache(maxsize=256)
def _font(file: str, index: int, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(file, size, index=index)


def text_width(font: dict, text: str, size: int, spacing_em: float = 0.0) -> float:
    """CSS の letter-spacing（em）込みの1行の幅（px）。書体が読めなければ全角幅で見積もる。"""
    try:
        w = _font(font["file"], int(font.get("index", 0)), int(size)).getlength(text)
    except (OSError, KeyError):
        w = len(text) * size
    return w + spacing_em * size * len(text)
