from app.core.timing import BEATS, beat_seconds, build_timing


def _timing(meta, b2=4, b3=6, **sec):
    return build_timing(meta, beat_seconds(meta, sec), b2, b3)


def test_beats_are_contiguous_and_cover_total(kt_meta):
    t = _timing(kt_meta)
    pos = 0
    for b in BEATS:
        r = t["beats"][b]
        assert r["from"] == pos and r["to"] > r["from"]
        pos = r["to"]
    assert t["total_frames"] == pos


def test_cumulative_rounding_does_not_drift(kt_meta):
    # 端数の秒が5つ重なっても、合計は「秒の合計×fps」の丸めと一致する
    sec = {"b1_first": 2.33, "b2_flow": 5.17, "b3_montage": 4.71, "b4_out": 0.77, "b5_title": 3.31}
    t = build_timing(kt_meta, sec, 4, 6)
    assert t["total_frames"] == round(sum(sec.values()) * kt_meta["fps"])


def test_switches_start_at_b2_and_increase(kt_meta):
    t = _timing(kt_meta, b2=5)
    sw = t["b2_switches"]
    assert len(sw) == 5 and sw[0] == t["beats"]["b2_flow"]["from"]
    assert all(a < b for a, b in zip(sw, sw[1:]))
    assert sw[-1] < t["beats"]["b2_flow"]["to"]


def test_switches_from_narration_seconds(kt_meta):
    sec = beat_seconds(kt_meta)
    t = build_timing(kt_meta, sec, 3, 6, switch_sec=[0.0, 1.5, 3.2])
    b2 = t["beats"]["b2_flow"]["from"]
    assert t["b2_switches"] == [b2, b2 + 45, b2 + 96]


def test_cuts_cover_b3_and_accelerate(kt_meta):
    t = _timing(kt_meta)
    cuts = t["b3_cuts"]
    r = t["beats"]["b3_montage"]
    assert cuts[0]["from"] == r["from"] and cuts[-1]["to"] == r["to"]
    assert all(a["to"] == b["from"] for a, b in zip(cuts, cuts[1:]))
    lens = [c["to"] - c["from"] for c in cuts]
    # だんだん速く: 長さは増えない（端数は先頭に配るので最後が伸びない）
    assert all(a >= b for a, b in zip(lens, lens[1:])), lens
    min_f = round(kt_meta["beats"]["b3_montage"]["cuts"]["min_sec"] * kt_meta["fps"])
    assert min(lens) >= min_f


def test_cuts_cycle_materials(kt_meta):
    t = _timing(kt_meta, b3=4)
    assert [c["material"] for c in t["b3_cuts"]][:6] == [0, 1, 2, 3, 0, 1]


def test_very_short_montage_is_one_cut(kt_meta):
    t = build_timing(kt_meta, {**beat_seconds(kt_meta), "b3_montage": 0.1}, 4, 6)
    assert len(t["b3_cuts"]) == 1


def test_sfx_cues_follow_meta(kt_meta):
    t = _timing(kt_meta, b2=4)
    kinds = [s["kind"] for s in t["sfx"]]
    assert kinds.count("whoosh") == 4
    assert kinds.count("hit") == len(t["b3_cuts"])
    assert {"frame": t["beats"]["b4_out"]["from"], "kind": "flash"} in t["sfx"]
    assert [s["frame"] for s in t["sfx"]] == sorted(s["frame"] for s in t["sfx"])
