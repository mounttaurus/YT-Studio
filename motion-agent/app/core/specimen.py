"""書体の見本帳（1行1書体の画像）。Pillow で描く＝Remotion を使わず1秒ほど。

書体が数百あると名前では選べないので、Claude とユーザーが画像で見て選び、気に入った書体に用途タグを付ける
（shared/motion/fonts/fonts.json の上書き）。演出プランの書体は id か `role:<タグ>` で指定する。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.core import fonts as fonts_mod

WIDTH, ROW = 1600, 104
MAX_ROWS = 60


def select(fonts: dict, ids: list[str] | None = None, tag: str | None = None, ja_only: bool = True,
           user_only: bool = False, query: str | None = None) -> list[str]:
    table = fonts["fonts"]
    if ids:
        out = [fonts_mod.resolve_ref(fonts, i) for i in ids]
        return [i for i in dict.fromkeys(out) if i]
    keys = [k for k, f in table.items()
            if (not ja_only or f.get("ja", True)) and (not user_only or f.get("user"))
            and (not tag or tag in (f.get("tags") or []))
            and (not query or query.lower() in (k + " " + f["family"]).lower())]
    return sorted(keys, key=lambda k: (bool(table[k].get("user")), k))


def _font(path: str, index: int, size: int):
    try:
        return ImageFont.truetype(path, size, index=index)
    except (OSError, ValueError):
        return None


def render(fonts: dict, text: str, out_dir: Path, ids: list[str] | None = None, tag: str | None = None,
           ja_only: bool = True, user_only: bool = False, query: str | None = None, limit: int = 24, offset: int = 0) -> dict:
    """見本帳を out_dir に JPEG で書く。返り値: {file, ids, total, offset, shown}（file は out_dir 内のファイル名）。"""
    table = fonts["fonts"]
    all_ids = select(fonts, ids, tag, ja_only, user_only, query)
    limit = max(1, min(int(limit), MAX_ROWS))
    shown = all_ids[max(0, offset):max(0, offset) + limit]
    label_src = table.get("gothic") or next(iter(table.values()))
    label_font = _font(label_src["file"], label_src.get("index", 0), 22)
    img = Image.new("RGB", (WIDTH, ROW * max(1, len(shown))), "#101012")
    d = ImageDraw.Draw(img)
    for r, fid in enumerate(shown):
        f = table[fid]
        y = r * ROW
        if r % 2:
            d.rectangle((0, y, WIDTH, y + ROW), fill="#17171a")
        tags = " ".join(f"#{t}" for t in (f.get("tags") or []))
        meta = f"{fid}  ·  {f['family']} w{f.get('weight', 400)}  {tags}"
        d.text((24, y + 8), meta, font=label_font, fill="#8d8d99")
        fnt = _font(f["file"], f.get("index", 0), 50)
        if fnt is None:
            d.text((24, y + 40), "(読めません)", font=label_font, fill="#aa5555")
            continue
        d.text((24, y + 38), text, font=fnt, fill="#ece9e0")
    out_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(("|".join(shown) + text).encode("utf-8")).hexdigest()[:10]
    out = out_dir / f"specimen_{key}.jpg"
    img.save(out, quality=82)
    return {"file": out.name, "ids": shown, "total": len(all_ids), "offset": max(0, offset), "shown": len(shown)}
