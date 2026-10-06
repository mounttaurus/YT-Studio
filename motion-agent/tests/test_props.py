import copy

from app.core.props import build_props, default_still_frames, validate_input


def _fields(errors):
    return {e["field"] for e in errors}


def test_sample_is_valid(kt_meta, kt_input):
    assert validate_input(kt_meta, kt_input) == []


def test_counts_and_lengths(kt_meta, kt_input):
    bad = copy.deepcopy(kt_input)
    bad["b1_first"] = [{"text": "あ", "kind": "word"}] * 4
    bad["b2_flow"] = ["とても長いキーワードの例です"] + bad["b2_flow"][1:]
    bad["b3_montage"] = bad["b3_montage"][:2]
    f = _fields(validate_input(kt_meta, bad))
    assert {"b1_first", "b2_flow[0]", "b3_montage"} <= f


def test_kind_choice_and_variant(kt_meta, kt_input):
    bad = copy.deepcopy(kt_input)
    bad["b1_first"][0]["kind"] = "person"
    bad["b4_out"] = "red"
    bad["variant"] = {"palette": "pink", "unknown": 1}
    f = _fields(validate_input(kt_meta, bad))
    assert {"b1_first[0].kind", "b4_out", "variant.palette", "variant.unknown"} <= f


def test_src_must_be_relative_inside_shared(kt_meta, kt_input, tmp_path):
    bad = copy.deepcopy(kt_input)
    bad["b3_montage"][0]["src"] = "../secret.png"
    bad["b3_montage"][1]["src"] = "/etc/passwd"
    bad["b3_montage"][2]["src"] = "projects/none/missing.png"
    f = _fields(validate_input(kt_meta, bad, shared_dir=tmp_path))
    assert {"b3_montage[0].src", "b3_montage[1].src", "b3_montage[2].src"} <= f


def test_existing_shared_file_is_accepted(kt_meta, kt_input, tmp_path):
    (tmp_path / "motion").mkdir()
    (tmp_path / "motion" / "a.png").write_bytes(b"x")
    ok = copy.deepcopy(kt_input)
    ok["b3_montage"][0]["src"] = "motion/a.png"
    assert validate_input(kt_meta, ok, shared_dir=tmp_path) == []


def test_total_seconds_limit(kt_meta, kt_input):
    bad = copy.deepcopy(kt_input)
    bad["beat_sec"] = {"b1_first": 4.0, "b2_flow": 9.0, "b3_montage": 7.0}
    assert "beat_sec" in _fields(validate_input(kt_meta, bad))


def test_title_needs_plate_or_text(kt_meta, kt_input):
    bad = copy.deepcopy(kt_input)
    bad["b5_title"] = {"plate": None, "main": "", "sub": ""}
    assert "b5_title" in _fields(validate_input(kt_meta, bad))


def test_props_fill_variant_defaults(kt_meta, kt_input):
    inp = copy.deepcopy(kt_input)
    inp.pop("variant")
    p = build_props(kt_meta, inp, "http://x/files/")
    assert p["variant"] == {"palette": "crimson", "montage_motion": "fly", "montage_tone": "mono", "grain": True}
    assert p["asset_base"] == "http://x/files/"
    assert p["template"] == {"id": "kinetic_teaser", "version": 1}


def test_default_stills_inside_their_beats(kt_meta, kt_input):
    t = build_props(kt_meta, kt_input, "")["timing"]
    frames = default_still_frames(t)
    names = [f["name"] for f in frames]
    assert len(names) == len(set(names))
    for f in frames:
        beat = "_".join(f["name"].split("_")[:2])
        r = t["beats"][beat]
        assert r["from"] <= f["frame"] < r["to"], f
