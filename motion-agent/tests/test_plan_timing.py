import pytest

from app.core import timing as t
from app.core.templates import get_meta


@pytest.fixture
def v1():
    return get_meta("kinetic_teaser", 1)


def _shot(i, beat, gimmick, bars, **kw):
    return {"id": f"s{i}", "beat": beat, "gimmick": gimmick, "bars": bars, "enter": kw.pop("enter", None), **kw}


def _default_shots(v1, bpm=120):
    cfg = v1["beats"]["b3_montage"]["cuts"]
    d = lambda b: t.default_bars(v1, b, bpm)  # noqa: E731
    return [
        _shot(1, "b1_first", "K1_first", d("b1_first")),
        _shot(2, "b2_flow", "K2_flow", d("b2_flow"), n_words=5),
        _shot(3, "b3_montage", "K3_montage", d("b3_montage"), n_materials=6, cut_cfg=cfg),
        _shot(4, "b4_out", "K4_out", d("b4_out")),
        _shot(5, "b5_title", "K5_title", d("b5_title")),
    ]


def test_default_bars_are_v1_seconds_snapped_to_beats(v1):
    assert [t.default_bars(v1, b, 120) for b in t.BEATS] == [1.25, 3.0, 2.5, 0.5, 1.75]
    assert t.default_bars(v1, "b4_out", 200) >= 0.25  # どんな BPM でも最低1拍


def test_default_plan_is_18_seconds_at_120(v1):
    tl = t.plan_timeline(v1, 120, _default_shots(v1))
    assert tl["total_sec"] == 18.0 and tl["total_frames"] == 540
    froms = [s["from"] for s in tl["shots"]]
    assert froms == [0, 75, 255, 405, 435]  # 2.5s・6s・5s・1s（v1 は 0.8s）
    assert tl["shots"][1]["bar_no"] == 2 and tl["shots"][1]["beat_no"] == 2  # 1.25 小節＝2小節目の2拍目


def test_k4_overlays_the_previous_shot(v1):
    tl = t.plan_timeline(v1, 120, _default_shots(v1))
    k3, k4 = tl["shots"][2], tl["shots"][3]
    assert k3["to"] == k4["from"] and k3["hold_to"] == k4["to"]
    assert tl["shots"][0]["hold_to"] == tl["shots"][0]["to"]


def test_positions_are_cumulative_no_drift_at_awkward_bpm(v1):
    # 127 BPM は拍がフレームに割り切れない。ショットごとに丸めると積み重なるが、累積の位置から丸めれば一致する
    shots = [_shot(i, "b1_first", "K1_first", 0.25) for i in range(1, 17)]
    tl = t.plan_timeline(v1, 127, shots)
    assert tl["shots"][-1]["to"] == round(16 * 60 / 127 * 30)
    ends = [s["to"] for s in tl["shots"]]
    assert all(b > a for a, b in zip(ends, ends[1:]))


def test_k2_switches_snap_to_beats_and_stay_distinct(v1):
    assert t.k2_switch_beats(12, 5) == [0, 2, 5, 7, 10]  # 12/5 の等分（0, 2.4, 4.8, 7.2, 9.6）を拍に丸めたもの
    assert t.k2_switch_beats(4, 4) == [0, 1, 2, 3]
    assert t.k2_switch_beats(2, 3) == [0, 0.5, 1.5]  # 語が拍より多ければ半拍（0, 0.67, 1.33 の一番近い半拍）
    with pytest.raises(ValueError):
        t.k2_switch_beats(1, 3)
    tl = t.plan_timeline(v1, 120, _default_shots(v1))
    sw = tl["shots"][1]["switches"]
    assert sw[0] == tl["shots"][1]["from"] and sw == sorted(set(sw)) and len(sw) == 5


def test_k3_cuts_get_faster_and_land_on_the_16th_grid(v1):
    tl = t.plan_timeline(v1, 120, _default_shots(v1))
    k3 = tl["shots"][2]
    cuts = k3["cuts"]
    assert cuts[0]["from"] == k3["from"] and cuts[-1]["to"] == k3["to"]
    assert all(c["to"] - c["from"] >= 2 for c in cuts)
    lens = [c["to"] - c["from"] for c in cuts]
    assert lens[0] > lens[-1] and lens[0] >= 20  # だんだん速く
    f16 = 30 * 60 / 120 / 4
    for c in cuts[1:]:
        k = (c["from"] - k3["from"]) / f16
        assert abs(k - round(k)) < 0.5 / f16 + 1e-6  # 吸着（フレームの丸め込みで1フレーム未満のずれ）


def test_enter_overlaps_the_previous_shot(v1):
    shots = _default_shots(v1)
    shots[1]["enter"] = {"type": "A1_portal", "bars": 0.5, "x": 0.5, "y": 0.5}
    tl = t.plan_timeline(v1, 120, shots)
    s1, s2 = tl["shots"][0], tl["shots"][1]
    assert s2["enter"]["from"] == s1["to"] - 30 and s2["enter"]["to"] == s1["to"]
    assert tl["total_frames"] == 540 - 30


def test_sfx_marks(v1):
    tl = t.plan_timeline(v1, 120, _default_shots(v1))
    kinds = {s["kind"] for s in tl["sfx"]}
    assert {"impact", "whoosh", "hit", "flash", "riser"} == kinds
    assert [s["frame"] for s in tl["sfx"]] == sorted(s["frame"] for s in tl["sfx"])


def test_non_beat_bars_are_rejected():
    with pytest.raises(ValueError):
        t.to_beats(0.3)
    assert t.to_beats(1.25) == 5
