import copy

import pytest

from app.core import builders, plan
from app.core.templates import get_meta, load_sample_input


@pytest.fixture
def meta():
    return get_meta("kinetic_teaser", 2)


@pytest.fixture
def default(meta):
    return load_sample_input("kinetic_teaser", 2, "default")


@pytest.fixture
def mk(meta):
    return load_sample_input("kinetic_teaser", 2, "mkultra")


def _fields(errs):
    return {e["field"] for e in errs}


def _errs(meta, inp):
    return plan.validate_input(meta, inp)


def test_builder_is_plan(meta):
    assert builders.for_meta(meta) is plan


def test_default_plan_is_valid_and_fills_every_beat(meta, default):
    a = plan.analyze(meta, default)
    assert a["errors"] == []
    assert [s["gimmick"] for s in a["shots"]] == ["K1_first", "K2_flow", "K3_montage", "K4_out", "K5_title"]
    assert a["timeline"]["total_sec"] == 18.0


def test_samples_are_valid(meta, mk):
    assert _errs(meta, mk) == []


def test_aliases_are_accepted_and_saved_canonically(meta, mk):
    p = copy.deepcopy(mk)
    p["direction"]["beats"]["b1_first"][0]["gimmick"] = "decode"
    p["direction"]["beats"]["b3_montage"][1]["enter"]["type"] = "portal"
    a = plan.analyze(meta, p)
    assert a["errors"] == []
    assert a["shots"][0]["gimmick"] == "B3_decode"
    assert a["shots"][3]["enter"]["type"] == "A1_portal"


def test_params_are_filled_from_the_brief(meta, default):
    p = copy.deepcopy(default)
    p["direction"] = {"beats": {"b1_first": [{"id": "s1", "gimmick": "B3_decode", "bars": 1.5}]}}
    props = plan.build_props(meta, p, "")
    assert props["shots"][0]["params"]["lines"][0]["text"] == "PROJECT ZERO"
    p["direction"] = {"beats": {"b1_first": [{"id": "s1", "gimmick": "B4_font_cycle", "bars": 1.5}]}}
    errs = _errs(meta, p)
    assert "direction.beats.b1_first[0].params.text" in _fields(errs)  # 12字は6字を超える
    del p["brief"]["b1_first"]
    assert any("brief.b1_first" in e["message"] for e in _errs(meta, p))


def test_default_plan_props_have_v1_content(meta, default):
    props = plan.build_props(meta, default, "")
    k1, k2, k3, k4, k5 = props["shots"]
    assert k1["params"]["items"][0]["text"] == "PROJECT ZERO"
    assert k2["params"]["words"][0] == "極秘計画" and k2["params"]["switches"][0] == 0
    assert k3["hold_to"] == k4["to"] and len(k3["params"]["cuts"]) > 5
    assert k5["params"]["out"] == "black" and k5["params"]["title"]["main"] == "見本チャンネル"
    assert props["total_frames"] == k5["to"]


@pytest.mark.parametrize("mutate,field", [
    (lambda p: p["direction"]["beats"]["b1_first"][0].update(gimmick="explode"), "direction.beats.b1_first[0].gimmick"),
    (lambda p: p["direction"]["beats"]["b1_first"][0].update(gimmick="K4_out"), "direction.beats.b1_first[0].gimmick"),
    (lambda p: p["direction"]["beats"]["b1_first"][0].update(bars=1.3), "direction.beats.b1_first[0].bars"),
    (lambda p: p["direction"]["beats"]["b1_first"][0].update(bars=8), "direction.beats.b1_first[0].bars"),
    (lambda p: p["direction"]["beats"]["b2_flow"][0].update(id="s1"), "direction.beats.b2_flow[0].id"),
    (lambda p: p["direction"]["beats"]["b3_montage"][0].update(enter={"type": "A1_portal", "bars": 0.5, "x": 0.5, "y": 0.5}),
     "direction.beats.b3_montage[0].enter"),  # ③の最初は前のビートの直後だが、portal は前のショットの点が要る→ OK のはず
    (lambda p: p["direction"].update(bpm=300), "direction.bpm"),
    (lambda p: p["direction"]["beats"]["b3_montage"][1]["enter"].update(bars=2.0), "direction.beats.b3_montage[1].enter.bars"),
    (lambda p: p["direction"]["beats"]["b3_montage"][1]["enter"].update(x=2), "direction.beats.b3_montage[1].enter.x"),
    (lambda p: p["direction"]["beats"]["b3_montage"][1]["params"]["stages"][1].update(src="../etc/x.png"),
     "direction.beats.b3_montage[1].params.stages[1].src"),
    (lambda p: p["direction"]["beats"]["b3_montage"][1]["params"]["stages"].append({"shape": "glyph", "text": "あ", "font": "no_font"}),
     "direction.beats.b3_montage[1].params.stages[3].font"),
    (lambda p: p["direction"]["beats"]["b1_first"][0]["params"]["lines"][0].update(font="comic_sans"),
     "direction.beats.b1_first[0].params.lines[0].font"),
    (lambda p: p["direction"]["beats"]["b3_montage"][0]["params"].update(materials=[0, 99]), "direction.beats.b3_montage[0].params.materials"),
    (lambda p: p["brief"].update(b2_flow=["a", "b"]), "brief.b2_flow"),
    (lambda p: p["brief"]["b3_montage"][0].update(src="../x.png"), "brief.b3_montage[0].src"),
    (lambda p: p["brief"].update(unknown=1), "brief.unknown"),
    (lambda p: p["brief"].update(beat_sec={"b1_first": 2}), "brief.beat_sec"),
])
def test_validation_catches(meta, mk, mutate, field):
    p = copy.deepcopy(mk)
    mutate(p)
    errs = _errs(meta, p)
    if field == "direction.beats.b3_montage[0].enter":
        assert errs == [] or field in _fields(errs)
        return
    assert field in _fields(errs), errs


def test_enter_needs_a_previous_shot_and_not_after_k4(meta, mk):
    p = copy.deepcopy(mk)
    p["direction"]["beats"]["b1_first"][0]["enter"] = {"type": "A3_light", "bars": 0.25, "x": 0.5, "y": 0.5}
    assert "direction.beats.b1_first[0].enter" in _fields(_errs(meta, p))
    p = copy.deepcopy(mk)
    p["direction"]["beats"]["b5_title"] = [{"id": "t", "gimmick": "K5_title", "bars": 1.75, "enter": {"type": "A3_light", "bars": 0.25, "x": 0.5, "y": 0.5}}]
    assert "direction.beats.b5_title[0].enter" in _fields(_errs(meta, p))


def test_total_over_20_seconds_is_an_error(meta, mk):
    p = copy.deepcopy(mk)
    p["direction"]["beats"]["b1_first"][0]["bars"] = 2.5  # 5秒
    p["direction"]["beats"]["b2_flow"][0]["bars"] = 4.5   # 9秒
    errs = _errs(meta, p)
    assert any("上限" in e["message"] for e in errs)


def test_safe_area_overflow_is_caught(meta, mk):
    p = copy.deepcopy(mk)
    p["direction"]["beats"]["b1_first"][0]["params"]["lines"][0].update(text="MK-ULTRA PROJECT 1953 MIND", size=140)
    errs = _errs(meta, p)
    assert any("安全域" in e["message"] for e in errs)


def test_source_is_required_outside_static(meta, mk, tmp_path):
    p = copy.deepcopy(mk)
    (tmp_path / "x.png").write_bytes(b"x")
    for i in range(3):
        p["brief"]["b3_montage"][i]["src"] = "x.png"
    errs = plan.validate_input(meta, p, tmp_path)
    assert {"brief.b3_montage[0].source", "brief.b3_montage[2].source"} <= _fields(errs)
    for i in range(3):
        p["brief"]["b3_montage"][i]["source"] = {"kind": "cc_by", "author": "A", "url": "https://x", "license": "CC BY 4.0"}
    assert not [e for e in plan.validate_input(meta, p, tmp_path) if "source" in e["field"]]
    p["brief"]["b3_montage"][0]["source"] = {"kind": "cc_by", "author": "A"}
    assert "brief.b3_montage[0].source.url" in _fields(plan.validate_input(meta, p, tmp_path))
    p["brief"]["b3_montage"][0]["source"] = {"kind": "found_online"}
    assert "brief.b3_montage[0].source" in _fields(plan.validate_input(meta, p, tmp_path))


def test_flash_estimate_rejects_a_strobing_k2(meta, mk):
    p = copy.deepcopy(mk)
    p["brief"]["b2_flow"] = ["一", "二", "三", "四", "五", "六"]
    p["brief"]["variant"]["palette"] = "noir"  # 黄・黒・白が交互＝明るさの差が大きい
    p["direction"]["beats"]["b2_flow"][0].update(bars=1)  # 6語を2秒＝毎秒3語
    p["direction"]["beats"]["b2_flow"][0]["gimmick"] = "K2_flow"
    errs = _errs(meta, p)
    assert any("明滅" in e["message"] for e in errs), errs
    p["direction"]["beats"]["b2_flow"][0].update(bars=3)  # 6語を6秒＝毎秒1語
    assert not any("明滅" in e["message"] for e in _errs(meta, p))


def test_k2_with_too_many_words_for_the_beats(meta, mk):
    p = copy.deepcopy(mk)
    p["brief"]["b2_flow"] = ["一", "二", "三", "四", "五", "六"]
    p["direction"]["beats"]["b2_flow"][0].update(bars=0.5)  # 1秒に6語（半拍の枠は4）
    errs = _errs(meta, p)
    assert _fields(errs)  # 秒の範囲 or 語が多すぎる


def test_light_transition_counts_two_flashes_toward_the_limit(meta, mk):
    p = copy.deepcopy(mk)
    p["direction"]["beats"]["b3_montage"][2]["enter"] = {"type": "A3_light", "bars": 0.25, "x": 0.5, "y": 0.5}
    a = plan.analyze(meta, p)
    assert a["errors"] == []
    assert a["flash"]["max_per_sec"] >= 1


def test_morph_pairs(meta, mk):
    p = copy.deepcopy(mk)
    # decode → C1 を morph で（文字は MK-ULTRA = 8字 → 6字を超えるので指摘）
    p["direction"]["beats"]["b3_montage"] = [
        {"id": "s3", "gimmick": "K3_montage", "bars": 1.5, "params": {"materials": [0, 1, 2]}},
        {"id": "s4", "gimmick": "C1_particle_morph", "bars": 2, "enter": {"type": "morph", "bars": 0.5},
         "params": {"stages": [{"shape": "sphere"}, {"shape": "scatter"}]}},
    ]
    errs = _errs(meta, p)
    assert any("morph" in e["message"] for e in errs)  # K3 と C1 は morph できない
    p["direction"]["beats"]["b3_montage"] = [
        {"id": "s3", "gimmick": "B4_font_cycle", "bars": 1.5, "params": {"text": "洗脳", "fonts": ["gothic_black", "brush"]}},
        {"id": "s4", "gimmick": "C1_particle_morph", "bars": 3, "enter": {"type": "morph", "bars": 0.5},
         "params": {"stages": [{"shape": "sphere"}, {"shape": "scatter"}]}},
    ]
    a = plan.analyze(meta, p)
    assert a["errors"] == [], a["errors"]
    props = plan.build_props(meta, p, "")
    c1 = props["shots"][4]["params"]  # s1, s2, s3, s4 の並び（b3 は 3番目から）
    c1 = [s for s in props["shots"] if s["id"] == "s4"][0]["params"]
    assert c1["stages"][0]["shape"] == "glyph" and c1["stages"][0]["at"] == 0
    assert c1["stages"][1]["at"] >= 15  # 重なりが終わってから次の形へ
    assert [st["at"] for st in c1["stages"]] == sorted(st["at"] for st in c1["stages"])


def test_morph_c1_to_text_appends_the_glyph(meta, mk):
    p = copy.deepcopy(mk)
    p["direction"]["beats"]["b3_montage"] = [
        {"id": "s3", "gimmick": "C1_particle_morph", "bars": 3,
         "params": {"stages": [{"shape": "scatter"}, {"shape": "sphere"}]}},
        {"id": "s4", "gimmick": "B4_font_cycle", "bars": 1.5, "enter": {"type": "morph", "bars": 0.5},
         "params": {"text": "洗脳", "fonts": ["gothic_black", "brush"]}},
    ]
    a = plan.analyze(meta, p)
    assert a["errors"] == [], a["errors"]
    props = plan.build_props(meta, p, "")
    c1 = [s for s in props["shots"] if s["id"] == "s3"][0]
    last = c1["params"]["stages"][-1]
    assert last["shape"] == "glyph" and last["at"] < c1["to"] - c1["from"]
    assert len(last["points"]) == c1["params"]["n"]


def test_still_frames_are_inside_and_at_most_12(meta, mk):
    props = plan.build_props(meta, mk, "")
    frames = plan.still_frames(props)
    assert 5 <= len(frames) <= 12
    assert all(0 <= f["frame"] < props["total_frames"] for f in frames)
    assert any(f["name"].endswith("_enter") for f in frames)
    assert len({f["name"] for f in frames}) == len(frames)


def test_font_roles_resolve_and_are_reported(meta, mk):
    p = copy.deepcopy(mk)
    p["direction"]["beats"]["b1_first"] = [
        {"id": "s1", "gimmick": "B4_font_cycle", "bars": 1.5, "params": {"text": "洗脳", "fonts": ["role:不穏", "role:毛筆", "gothic_black"]}}]
    a = plan.analyze(meta, p)
    assert a["errors"] == []
    assert set(a["resolved_fonts"]) == {"role:不穏", "role:毛筆"}
    p["direction"]["beats"]["b1_first"][0]["params"]["fonts"] = ["role:存在しない", "gothic_black"]
    assert "direction.beats.b1_first[0].params.fonts" in _fields(_errs(meta, p))
