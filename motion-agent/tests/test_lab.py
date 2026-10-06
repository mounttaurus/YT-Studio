import copy

import pytest

from app.core import builders, lab
from app.core.templates import get_meta, load_sample_input


@pytest.fixture
def meta():
    return get_meta("gimmick_lab", 1)


@pytest.fixture
def inp():
    return load_sample_input("gimmick_lab", 1, "mkultra")


def _fields(errs):
    return {e["field"] for e in errs}


def test_builder_is_chosen_by_meta(meta, kt_meta):
    assert builders.for_meta(meta) is lab
    assert builders.for_meta(kt_meta).__name__.endswith("props")


def test_sample_is_valid(meta, inp):
    assert lab.validate_input(meta, inp) == []


def test_portal_overlaps_the_previous_shot(meta, inp):
    p = lab.build_props(meta, inp, "")
    s1, s2, s3 = p["shots"]
    assert (s1["from"], s1["to"]) == (0, 102)
    assert s2["from"] == 102 and s2["to"] == 192
    # 3つ目は 1.4 秒（42 フレーム）重なって始まる
    assert s3["from"] == 150 and s3["enter"]["from"] == 150 and s3["enter"]["to"] == 192
    assert s3["enter"]["x"] == 960 and s3["enter"]["y"] == round(0.86 * 1080)
    assert p["total_frames"] == s3["to"] == 330


def test_particle_stages_have_points_and_times(meta, inp):
    s3 = lab.build_props(meta, inp, "")["shots"][2]["params"]
    assert [st["shape"] for st in s3["stages"]] == ["scatter", "glyph", "glyph", "sphere"]
    assert all(len(st["points"]) == s3["n"] for st in s3["stages"])
    ats = [st["at"] for st in s3["stages"]]
    assert ats[0] == 0 and ats == sorted(ats) and ats[-1] < 180
    assert s3["stages"][3]["spin"] and not s3["stages"][1]["spin"]


def test_fonts_are_resolved_to_families(meta, inp):
    p = lab.build_props(meta, inp, "")
    fc = p["shots"][1]["params"]["fonts"]
    assert all(f["family"] and f["id"] for f in fc)
    assert p["shots"][0]["params"]["stamp"]["text"] == "極秘"


def test_validation_messages(meta, inp):
    bad = copy.deepcopy(inp)
    bad["shots"][0]["enter"] = {"type": "portal", "sec": 1, "x": 0.5, "y": 0.5}
    bad["shots"][1]["gimmick"] = "explode"
    bad["shots"][2]["enter"]["sec"] = 9
    bad["shots"][2]["params"]["stages"][1]["font"] = "comic_sans"
    bad["shots"][2]["params"]["stages"].append({"shape": "image", "src": "../etc/x.png"})
    f = _fields(lab.validate_input(meta, bad))
    assert {"shots[0].enter", "shots[1].gimmick", "shots[2].enter.sec",
            "shots[2].params.stages[1].font", "shots[2].params.stages[4].src"} <= f


def test_total_seconds_limit(meta, inp):
    bad = copy.deepcopy(inp)
    bad["shots"][0]["sec"] = 8   # 8 + 8 + 10 − 重なり 1.4 = 24.6 秒 > 20 秒
    bad["shots"][1]["sec"] = 8
    bad["shots"][2]["sec"] = 10
    assert "shots" in _fields(lab.validate_input(meta, bad))


def test_still_frames_fall_inside_shots(meta, inp):
    p = lab.build_props(meta, inp, "")
    frames = lab.still_frames(p)
    assert 3 <= len(frames) <= 9
    assert all(0 <= f["frame"] < p["total_frames"] for f in frames)
    assert any(f["name"].endswith("_portal") for f in frames)
