import math

from PIL import Image, ImageDraw

from app.core import points as pts
from app.core.lab import load_fonts


def _in_unit(points):
    return all(-1.05 <= p[0] <= 1.05 and -1.05 <= p[1] <= 1.05 for p in points)


def test_sphere_has_n_points_on_the_radius():
    p = pts.sphere_points(500, radius=0.8)
    assert len(p) == 500
    assert all(abs(math.sqrt(x * x + y * y + z * z) - 0.8) < 0.01 for x, y, z in p)


def test_glyph_points_are_deterministic_and_normalized():
    f = load_fonts()["fonts"]["gothic_black"]
    a = pts.glyph_points("脳", f["file"], 800, seed=3, index=f.get("index", 0))
    b = pts.glyph_points("脳", f["file"], 800, seed=3, index=f.get("index", 0))
    assert a == b and len(a) == 800 and _in_unit(a)


def test_every_font_in_the_bundle_draws_japanese(tmp_path):
    # 同梱の全書体で日本語が描ける（字形が空なら点が取れずに落ちる）。
    # ユーザーの書体（shared/motion/fonts/）は英字だけの書体も多く、置いた物で結果が変わるので見ない
    for fid, f in load_fonts()["fonts"].items():
        if f.get("user"):
            continue
        p = pts.glyph_points("脳と洗脳", f["file"], 50, index=f.get("index", 0))
        assert len(p) == 50, fid


def test_image_points_dark_and_alpha(tmp_path):
    img = Image.new("RGB", (200, 100), "white")
    ImageDraw.Draw(img).ellipse((20, 10, 80, 90), fill="black")
    img.save(tmp_path / "a.png")
    p = pts.image_points(tmp_path / "a.png", 300, mode="dark")
    assert len(p) == 300 and _in_unit(p)
    rgba = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(rgba).rectangle((10, 10, 40, 90), fill=(255, 0, 0, 255))
    rgba.save(tmp_path / "b.png")
    assert len(pts.image_points(tmp_path / "b.png", 100, mode="alpha")) == 100


def test_order_for_morph_is_by_angle():
    o = pts.order_for_morph(pts.scatter_points(300, seed=2))
    angles = [round(math.atan2(p[1], p[0]), 3) for p in o]
    assert angles == sorted(angles)
