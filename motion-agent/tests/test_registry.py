from app.core import registry
from app.core.fonts import load_fonts, resolve_ref


def test_aliases_resolve_to_canonical_ids():
    assert registry.canonical("decode") == "B3_decode"
    assert registry.canonical("portal") == "A1_portal"
    assert registry.canonical("K3") == "K3_montage"
    assert registry.canonical("B3_decode") == "B3_decode"
    assert registry.canonical("unknown") == "unknown"  # 検査が指摘する
    assert registry.gimmick("decode")["cid"] == "B3"
    assert registry.enter(None)["name"] == "切り替え"


def test_every_alias_points_to_something_real():
    reg = registry.load()
    for alias, target in reg["aliases"].items():
        assert target in reg["gimmicks"] or target in reg["enters"], alias


def test_gimmick_entries_are_complete():
    for gid, g in registry.load()["gimmicks"].items():
        assert g["min_sec"] < g["max_sec"], gid
        assert g["effect"] and g["name"], gid


def test_font_ref_by_id_and_by_role_tag():
    fonts = load_fonts()
    assert resolve_ref(fonts, "gothic_black") == "gothic_black"
    tagged = resolve_ref(fonts, "role:不穏")
    assert tagged in fonts["fonts"] and "不穏" in fonts["fonts"][tagged]["tags"]
    assert resolve_ref(fonts, "role:不穏") == tagged  # 決定的
    assert resolve_ref(fonts, "role:存在しないタグ") is None
    assert resolve_ref(fonts, "no_such_font") is None
    assert resolve_ref(fonts, None) is None


def test_every_bundled_font_has_tags_from_the_vocabulary():
    import json
    from app.core.fonts import BUNDLED_FILE
    d = json.loads(BUNDLED_FILE.read_text(encoding="utf-8"))
    for fid, f in d["fonts"].items():
        assert f["tags"] and set(f["tags"]) <= set(d["tags_vocabulary"]), fid
