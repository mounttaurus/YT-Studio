"""ユーザーの書体（shared/motion/fonts/）の自動の取り込み。fc-query が要るのでコンテナの中で走らせる。"""
import json
import shutil

from app.core import fonts

SAMPLE = "/usr/share/fonts/truetype/vlgothic/VL-Gothic-Regular.ttf"  # 同梱の書体を「ユーザーの書体」に見立てる


def test_discovers_family_weight_and_japanese(tmp_path):
    shutil.copy(SAMPLE, tmp_path / "MyFont Regular.ttf")
    found = fonts.discover_user_fonts(tmp_path)
    assert list(found) == ["user_myfont-regular"]
    f = found["user_myfont-regular"]
    assert f["family"] == "VL Gothic" and f["weight"] == 400 and f["ja"] and f["user"]
    assert f["file"].endswith("MyFont Regular.ttf")


def test_ignores_non_font_files_and_missing_folder(tmp_path):
    (tmp_path / "readme.txt").write_text("x")
    assert fonts.discover_user_fonts(tmp_path) == {}
    assert fonts.discover_user_fonts(tmp_path / "none") == {}


def test_overrides_rename_and_role(tmp_path):
    shutil.copy(SAMPLE, tmp_path / "myfont.ttf")
    (tmp_path / "fonts.json").write_text(json.dumps(
        {"fonts": {"user_myfont": {"id": "title_gothic", "role": "タイトル"}}}), encoding="utf-8")
    merged = fonts.load_fonts(user_dir=tmp_path)["fonts"]
    assert "title_gothic" in merged and merged["title_gothic"]["role"] == "タイトル"
    assert "user_myfont" not in merged
    assert "gothic_black" in merged, "同梱の書体も残る"


def test_user_font_cannot_overwrite_a_bundled_id(tmp_path):
    shutil.copy(SAMPLE, tmp_path / "x.ttf")
    (tmp_path / "fonts.json").write_text(json.dumps({"fonts": {"user_x": {"id": "gothic_black"}}}), encoding="utf-8")
    merged = fonts.load_fonts(user_dir=tmp_path)["fonts"]
    assert merged["gothic_black"]["family"] == "Noto Sans CJK JP"


def test_css_weight_mapping():
    assert [fonts._css_weight(w) for w in (0, 50, 80, 100, 200, 210)] == [100, 300, 400, 500, 700, 900]
