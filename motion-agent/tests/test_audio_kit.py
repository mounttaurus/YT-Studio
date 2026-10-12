"""予告編キット（app/core/audio/kit.py・A2・MOTION_UNIFIED_PLAN §9-3）と BGM の結合（mix.mux_job の bgm）。"""
import json
import shutil

import numpy as np
import pytest

from app.core import config, timing
from app.core.audio import kit, mix, sfx_synth as S

SR = S.SR
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="ffmpeg が無い環境")


@pytest.fixture(scope="module")
def amap():
    return kit.demo_audiomap(120.0, 30)


@pytest.fixture(scope="module")
def rendered(amap):
    return {m: kit.render(amap, m, fps=30, seed=0) for m in kit.MOODS}


def _rms(x, a, b):
    seg = x[int(a * SR):int(b * SR)]
    return float(np.sqrt(np.mean(seg ** 2)))


def _span(amap, name):
    p = next(p for p in amap["energy_phases"] if p["name"] == name)
    return p["start"], p["end"]


def test_three_moods():
    assert sorted(kit.MOODS) == ["dread", "drive", "hush"]
    assert [m["mood"] for m in kit.moods()] == ["dread", "drive", "hush"]


@pytest.mark.parametrize("mood", sorted(kit.MOODS))
def test_length_peak_and_silence_drop(amap, rendered, mood):
    x, rep = rendered[mood]
    assert len(x) == round(amap["audio"]["duration_sec"] * SR)
    assert float(np.max(np.abs(x))) == pytest.approx(0.9, abs=1e-6)       # ピーク 0.9（置く側の音量で下げる）
    assert np.all(np.isfinite(x))
    b4 = _span(amap, "b4_out")
    # ④は無音の落とし: 頭から 20ms 以降は完全な無音（効果音の suck・riser はこの層の外）
    assert float(np.max(np.abs(x[int((b4[0] + 0.02) * SR):int(b4[1] * SR)]))) == 0.0
    assert rep["mood"] == mood and rep["stop_sec"] == pytest.approx(b4[0])


@pytest.mark.parametrize("mood", sorted(kit.MOODS))
def test_arc_builds_from_first_to_third_and_title_hits(amap, rendered, mood):
    """①＜②＜③と音量が上がり、⑤は低い一撃で大きく始まる（計画 §9-3）。"""
    x, _ = rendered[mood]
    r1, r2, r3, r5 = (_rms(x, *_span(amap, n)) for n in ("b1_first", "b2_flow", "b3_montage", "b5_title"))
    assert r1 < r2 < r3
    assert r5 > r1
    t5 = _span(amap, "b5_title")[0]
    assert _rms(x, t5, t5 + 0.15) > 1.5 * _rms(x, _span(amap, "b3_montage")[0], _span(amap, "b3_montage")[1])


@pytest.mark.parametrize("mood", sorted(kit.MOODS))
def test_starts_from_near_silence(amap, rendered, mood):
    """①は無音から立ち上がる（頭の 0.2 秒は小さい）。"""
    x, _ = rendered[mood]
    assert _rms(x, 0.0, 0.2) < 0.25 * _rms(x, *_span(amap, "b2_flow"))


@pytest.mark.parametrize("mood", sorted(kit.MOODS))
def test_same_inputs_give_identical_bytes_and_seed_changes_it(amap, rendered, mood):
    x, _ = rendered[mood]
    again, _ = kit.render(amap, mood, fps=30, seed=0)
    assert S.wav_bytes(x) == S.wav_bytes(again)
    other, _ = kit.render(amap, mood, fps=30, seed=1)
    assert S.wav_bytes(x) != S.wav_bytes(other)


def test_moods_differ_from_each_other(rendered):
    wavs = {m: S.wav_bytes(rendered[m][0]) for m in kit.MOODS}
    assert len(set(wavs.values())) == 3
    # 声の構成が違う（drive はキックとアルペジオ・dread と hush はタムの脈）
    v = {m: rendered[m][1]["voices"] for m in kit.MOODS}
    assert "kick" in v["drive"] and "arp" in v["drive"] and "kick" not in v["dread"] and "kick" not in v["hush"]
    assert "tom" in v["dread"] and "tom" in v["hush"]


def test_hits_land_on_frame_boundaries(amap):
    """打点はフレームに丸める（効果音と同じフレームで鳴る）: 120 BPM・30fps の拍（0.5 秒）は元々フレーム上。
    半端な BPM でも、キックの立ち上がりはフレームの境目に来る。"""
    am = kit.demo_audiomap(127.0, 30)
    x, _ = kit.render(am, "drive", fps=30, seed=0)
    b2 = _span(am, "b2_flow")
    seg = x[int(b2[0] * SR):int((b2[0] + 2.0) * SR)]
    # 立ち上がりの位置（絶対値が閾値を超える最初の点）を拾い、秒→フレームの端数を見る
    thr = 0.2 * float(np.max(np.abs(seg)))
    onsets = []
    prev = False
    for i, v in enumerate(np.abs(seg) > thr):
        if v and not prev:
            onsets.append(i)
        prev = bool(v)
    assert onsets
    first = (b2[0] * SR + onsets[0]) / SR * 30
    assert abs(first - round(first)) < 0.35      # フレームの境目の近く（立ち上がりの検出誤差の分を許す）


def test_unknown_mood_and_missing_structure(amap):
    with pytest.raises(kit.KitError):
        kit.render(amap, "bright", fps=30)
    bad = {**amap, "energy_phases": [p for p in amap["energy_phases"] if p["name"] != "b1_first"]}
    with pytest.raises(kit.KitError):
        kit.render(bad, "dread", fps=30)


def test_works_without_optional_beats(amap):
    """③④⑤が無い短い構成でも落ちない。"""
    short = dict(amap, energy_phases=[p for p in amap["energy_phases"] if p["name"] in ("b1_first", "b2_flow")])
    x, rep = kit.render(short, "hush", fps=30)
    assert len(x) > 0 and "final" not in rep["voices"]


def test_audiomap_from_props_drives_the_kit(amap):
    """描画ジョブの props から作った地図と、plan_timeline の地図が同じなら、BGM も同じ。"""
    shots = [{"beat": p["name"], "from": round(p["start"] * 30), "to": round(p["end"] * 30)} for p in amap["energy_phases"]]
    props = {"fps": 30, "bpm": 120.0, "total_frames": round(amap["audio"]["duration_sec"] * 30), "shots": shots,
             "sfx": []}
    a2 = timing.audiomap_from_props(props)
    x1, _ = kit.render(a2, "dread", fps=30, seed=3)
    x2, _ = kit.render(timing.audiomap_from_props(props), "dread", fps=30, seed=3)
    assert S.wav_bytes(x1) == S.wav_bytes(x2)


# ── 結合 ──

def _silent_mp4(path, sec):
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=160x90:rate=30:duration={sec}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def test_track_with_bed_is_limited_and_longer_beds_are_cut():
    bed = np.full(3 * SR, 0.5)
    track, rep = mix.render_track([{"kind": "impact", "at": 0.1, "gain": 0.9}], 2.0, bed=bed, bed_gain=1.0)
    assert len(track) == 2 * SR
    assert float(np.max(np.abs(track))) <= mix.CEILING + 1e-9 and rep["scaled"] < 1.0   # 敷き物＋一撃は上限で一律に下がる
    short, _ = mix.render_track([], 2.0, bed=np.full(SR, 0.3), bed_gain=0.5)
    assert float(np.max(np.abs(short[:SR]))) == pytest.approx(0.15) and float(np.max(np.abs(short[SR:]))) == 0.0


@needs_ffmpeg
def test_mux_job_with_bgm(tmp_path, monkeypatch, amap):
    shared = tmp_path / "shared"
    work = shared / "motion" / "_work"
    monkeypatch.setattr(config, "SHARED_DIR", shared)
    monkeypatch.setattr(config, "WORK_DIR", work)
    jid = "20261012-000000-abc123"
    d = work / jid
    d.mkdir(parents=True)
    _silent_mp4(d / "out.mp4", 17.5)
    shots = [{"beat": p["name"], "from": round(p["start"] * 30), "to": round(p["end"] * 30)} for p in amap["energy_phases"]]
    props = {"fps": 30, "bpm": 120.0, "total_frames": 525, "shots": shots,
             "sfx": [{"frame": 0, "kind": "impact"}, {"frame": 90, "kind": "whoosh"}, {"frame": 390, "kind": "flash"},
                     {"frame": 420, "kind": "riser"}]}
    (d / "props.json").write_text(json.dumps(props), encoding="utf-8")
    (d / "job.json").write_text(json.dumps({"job_id": jid, "kind": "render", "status": "done"}), encoding="utf-8")

    plain = mix.mux_job(jid)
    assert plain["bgm"] is None and plain["file"].endswith("out_sfx.mp4")
    res = mix.mux_job(jid, bgm="dread")
    assert res["file"].endswith("out_sfx_dread.mp4") and res["bgm"]["mood"] == "dread"
    assert (d / "out_sfx_dread.mp4").is_file() and (d / "bgm_dread.wav").is_file() and (d / "sfx_track_dread.wav").is_file()
    assert (d / "out_sfx.mp4").is_file()                                 # 効果音だけの版も残る
    assert mix.has_audio(d / "out_sfx_dread.mp4")
    assert res["loudness"]["true_peak_dbfs"] <= mix.TRUE_PEAK_LIMIT_DBFS + 0.05    # AAC の後でも -1dBTP 以内
    # BGM つきの方が音が多い（時間平均の大きさ＝RMS が上がる。統合ラウドネス LUFS はゲートで落ちた区間を数えないので比べない）
    def rms(path):
        raw = np.frombuffer(path.read_bytes()[44:], dtype="<i2").astype(np.float64) / 32768.0
        return float(np.sqrt(np.mean(raw ** 2)))
    assert rms(d / "sfx_track_dread.wav") > 1.3 * rms(d / "sfx_track.wav")
    # 同じ入力なら音の道はバイト一致
    first = (d / "sfx_track_dread.wav").read_bytes()
    mix.mux_job(jid, bgm="dread")
    assert (d / "sfx_track_dread.wav").read_bytes() == first

    with pytest.raises(ValueError):
        mix.mux_job(jid, bgm="bright")
    (d / "props.json").write_text(json.dumps({"timing": {"fps": 30, "sfx": []}}), encoding="utf-8")   # v1（bpm なし）
    with pytest.raises(ValueError):
        mix.mux_job(jid, bgm="dread")
