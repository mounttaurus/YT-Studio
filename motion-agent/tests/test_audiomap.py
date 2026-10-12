"""拍の地図（app/core/timing.py の audiomap_from_grid・A1・MOTION_UNIFIED_PLAN §9-5）。
形は HyperFrames の analyze-beatgrid.py の audiomap.json（version 2）に合わせる。"""
import copy
import json

import pytest

from app.core import timing as t
from app.core.templates import get_meta

# analyze-beatgrid.py（music-to-video）の audiomap.json の最上位のキー。grid の地図はこれに `source` を足したもの
ANALYZER_KEYS = ["version", "phraseBars", "summary", "audio", "tempo", "grid", "energy_phases", "key_moments",
                 "hard_stops", "rolls", "silences", "stats", "events", "phrases"]
EVENT_KEYS = {"t", "bar", "beat_in_bar", "step16", "grid", "drum", "energy", "feel", "special"}


@pytest.fixture
def v1():
    return get_meta("kinetic_teaser", 1)


def _shot(i, beat, gimmick, bars, **kw):
    return {"id": f"s{i}", "beat": beat, "gimmick": gimmick, "bars": bars, "enter": kw.pop("enter", None), **kw}


def _timeline(v1, bpm=120):
    cfg = v1["beats"]["b3_montage"]["cuts"]
    d = lambda b: t.default_bars(v1, b, bpm)  # noqa: E731
    shots = [
        _shot(1, "b1_first", "K1_first", d("b1_first")),
        _shot(2, "b2_flow", "K2_flow", d("b2_flow"), n_words=5),
        _shot(3, "b3_montage", "K3_montage", d("b3_montage"), n_materials=6, cut_cfg=cfg),
        _shot(4, "b4_out", "K4_out", d("b4_out")),
        _shot(5, "b5_title", "K5_title", d("b5_title")),
    ]
    return t.plan_timeline(v1, bpm, shots)


def test_plan_timeline_keeps_its_old_keys_and_adds_the_map(v1):
    tl = _timeline(v1)
    for k in ("fps", "bpm", "bar_sec", "total_frames", "total_sec", "shots", "beats", "sfx"):
        assert k in tl
    assert tl["audiomap"]["source"] == "grid"


def test_map_has_the_analyzer_shape(v1):
    am = _timeline(v1)["audiomap"]
    assert [k for k in am if k != "source"] == ANALYZER_KEYS
    assert am["version"] == 2 and am["phraseBars"] == 4
    assert set(am["audio"]) == {"path", "duration_sec", "sr"} and am["audio"]["path"] is None
    assert set(am["tempo"]) == {"bpm", "beats_per_bar", "downbeat_phase", "n_beats", "n_bars"}
    assert set(am["grid"]) == {"beats_sec", "downbeats_sec"}
    assert am["events"] and all(EVENT_KEYS <= set(e) for e in am["events"])
    json.dumps(am)   # JSON にできる


def test_grid_is_the_metronome(v1):
    tl = _timeline(v1, 120)
    am = tl["audiomap"]
    total_sec = tl["total_frames"] / tl["fps"]
    assert am["audio"]["duration_sec"] == pytest.approx(total_sec)
    b = am["grid"]["beats_sec"]
    assert b[0] == 0.0 and all(y - x == pytest.approx(0.5, abs=1e-3) for x, y in zip(b, b[1:]))
    assert len(b) == int(-(-total_sec // 0.5))                          # 全部の拍（尺を超えない）
    assert am["grid"]["downbeats_sec"] == b[::4]
    assert am["tempo"]["bpm"] == 120.0 and am["tempo"]["n_beats"] == len(b) and am["tempo"]["n_bars"] == len(b[::4])
    assert am["phrases"][0] == {"index": 0, "start": 0.0, "end": am["grid"]["downbeats_sec"][4], "bars": 4}
    assert am["phrases"][-1]["end"] == pytest.approx(total_sec, abs=1e-3)


def test_phases_follow_the_five_beats_and_b4_is_the_void(v1):
    tl = _timeline(v1)
    am = tl["audiomap"]
    assert [p["name"] for p in am["energy_phases"]] == list(t.BEATS)
    starts = [p["start"] for p in am["energy_phases"]]
    assert starts == sorted(starts)
    b4 = tl["beats"]["b4_out"]
    ph4 = next(p for p in am["energy_phases"] if p["name"] == "b4_out")
    assert ph4["level"] == "VOID" and ph4["start"] == pytest.approx(b4["from"] / tl["fps"])
    assert am["silences"] == [{"start": ph4["start"], "end": ph4["end"]}]
    # ③→④ は大きな落ち（ハードストップ）・②→③は山
    assert [h["t"] for h in am["hard_stops"]] == [ph4["start"]]
    kinds = {m["t"]: m["kind"] for m in am["key_moments"]}
    assert kinds[ph4["start"]] == "DROP"


def test_events_come_from_sfx_cues_with_metrical_position(v1):
    tl = _timeline(v1, 120)
    am = tl["audiomap"]
    assert len(am["events"]) == len(tl["sfx"])
    first = am["events"][0]                                             # ①の頭の impact
    assert (first["t"], first["bar"], first["beat_in_bar"], first["step16"], first["grid"], first["drum"]) == (
        0.0, 0, 1, 0, "strong", "kick")
    risers = [e for e in am["events"] if e["cue"] == "riser"]
    assert risers and risers[0]["special"] == "riser"
    flashes = [e for e in am["events"] if e["cue"] == "flash"]
    assert flashes and flashes[0]["special"] == "hard_stop"
    # 拍の頭に乗る打点は強い位置・小節内の拍は 1〜4・16分の位置は 0〜15
    for e in am["events"]:
        assert 1 <= e["beat_in_bar"] <= 4 and 0 <= e["step16"] <= 15
        assert e["grid"] in ("strong", "weak", "syncopated", "off-grid")


def test_montage_cuts_make_one_accelerating_roll(v1):
    am = _timeline(v1)["audiomap"]
    assert len(am["rolls"]) == 1
    r = am["rolls"][0]
    assert r["kind"] == "accel-roll" and r["hits"] >= 4 and r["drum"] == "snare"
    ph3 = next(p for p in am["energy_phases"] if p["name"] == "b3_montage")
    assert ph3["start"] <= r["start"] and r["end"] <= ph3["end"] and ph3["density"] == "dense"


def test_map_is_deterministic_and_follows_bpm(v1):
    assert _timeline(v1, 120)["audiomap"] == _timeline(v1, 120)["audiomap"]
    a, b = _timeline(v1, 120)["audiomap"], _timeline(v1, 100)["audiomap"]
    assert a["tempo"]["bpm"] == 120.0 and b["tempo"]["bpm"] == 100.0
    assert b["grid"]["beats_sec"][1] == pytest.approx(0.6)


def test_map_from_props_equals_map_from_timeline(v1):
    """描画ジョブの props（bpm・fps・total_frames・shots・sfx）からも同じ地図になる。"""
    tl = _timeline(v1)
    props = {"fps": tl["fps"], "bpm": tl["bpm"], "total_frames": tl["total_frames"],
             "shots": [{"beat": s["beat"], "from": s["from"], "to": s["to"]} for s in tl["shots"]], "sfx": tl["sfx"]}
    assert t.audiomap_from_props(copy.deepcopy(props)) == tl["audiomap"]
