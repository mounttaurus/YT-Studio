"""打点 → 音の道 → 無音の mp4 への結合（app/core/audio/mix.py・A0・OPENING_MOTION_PLAN M2 の口）。"""
import json
import shutil
import subprocess
import wave

import numpy as np
import pytest

from app.core import config
from app.core.audio import mix, sfx_synth as S

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="ffmpeg が無い環境")


# ── 打点 ──

def test_cues_from_timing_maps_kinds_and_vary_seed():
    sfx = [{"frame": 90, "kind": "whoosh"}, {"frame": 30, "kind": "whoosh"}, {"frame": 0, "kind": "impact"},
           {"frame": 60, "kind": "unknown"}, {"frame": 300, "kind": "riser"}, {"frame": 270, "kind": "flash"}]
    cues = mix.cues_from_timing(sfx, 30, seed=10)
    assert [(c["source"], c["kind"], c["at"]) for c in cues] == [
        ("impact", "impact", 0.0), ("whoosh", "whoosh", 1.0), ("whoosh", "whoosh", 3.0),
        ("flash", "suck", 9.0), ("riser", "riser", 10.0)]
    assert [c["seed"] for c in cues if c["kind"] == "whoosh"] == [10, 11]   # 同じ音の連打にしない


def test_thin_cues_drops_close_same_kind_only():
    cues = [{"kind": "stamp", "at": t} for t in (1.0, 1.04, 1.08, 1.12, 1.30)] + [{"kind": "whoosh", "at": 1.05}]
    kept, dropped = mix.thin_cues(cues, 0.1)
    assert [c["at"] for c in kept if c["kind"] == "stamp"] == [1.0, 1.12, 1.30]
    assert [c["at"] for c in dropped] == [1.04, 1.08]
    assert any(c["kind"] == "whoosh" for c in kept)
    assert mix.thin_cues(cues, 0)[1] == []


def test_cue_start_anchors_riser_and_suck_at_the_end():
    assert mix.cue_start({"kind": "whoosh", "at": 3.0}) == pytest.approx(3.0)
    assert mix.cue_start({"kind": "riser", "at": 3.0, "length": 1.25}) == pytest.approx(1.75)
    assert mix.cue_start({"kind": "suck", "at": 3.0, "length": 0.8}) == pytest.approx(2.2)


# ── 音の道 ──

def test_track_length_and_riser_apex_lands_on_the_hit():
    track, rep = mix.render_track([{"kind": "riser", "at": 2.0, "length": 1.0, "gain": 0.8}], 3.0)
    assert len(track) == 3 * S.SR
    # 頂点（終わり）が打点 2.0 秒。そこから後ろは無音
    assert abs(int(np.argmax(np.abs(track))) - 2 * S.SR) < int(0.02 * S.SR)
    assert np.max(np.abs(track[2 * S.SR + 10:])) == 0.0
    assert rep["cues"][0]["start"] == pytest.approx(1.0) and rep["cues"][0]["clipped"] is False


def test_whoosh_starts_on_the_hit():
    track, _ = mix.render_track([{"kind": "whoosh", "at": 1.0, "length": 0.5}], 2.0)
    assert np.max(np.abs(track[:S.SR - 5])) == 0.0
    assert np.max(np.abs(track[S.SR:int(1.5 * S.SR)])) > 0.1


def test_cue_past_the_end_is_cut_and_marked():
    track, rep = mix.render_track([{"kind": "impact", "at": 1.8, "length": 1.4}, {"kind": "riser", "at": 0.3, "length": 1.0}], 2.0)
    assert len(track) == 2 * S.SR
    assert [c["clipped"] for c in rep["cues"]] == [True, True]


def test_soft_limit_leaves_quiet_parts_alone_and_rounds_peaks():
    x = np.array([0.0, 0.3, -0.5, 0.59, 0.8, -1.2, 3.0])
    y = mix.soft_limit(x, 0.85)
    assert np.array_equal(y[:4], x[:4])                      # knee（0.595）までは触らない
    assert np.all(np.abs(y) <= 0.85) and y[4] < 0.8 and y[5] < -0.7 and y[6] == pytest.approx(0.85, abs=1e-3)
    assert np.all(np.diff(y[[3, 4, 6]]) > 0)                 # 単調（大きい入力は大きいまま）
    assert mix.soft_limit(np.array([0.1, -0.2]), 0.85).tolist() == [0.1, -0.2]


def test_stacked_hits_are_scaled_under_the_ceiling():
    cues = [{"kind": "impact", "at": 0.1, "gain": 0.9, "volume": 1.0, "seed": i} for i in range(4)]
    track, rep = mix.render_track(cues, 1.0)
    assert rep["scaled"] < 1.0
    assert float(np.max(np.abs(track))) <= mix.CEILING + 1e-9


def test_volume_scales_the_cue():
    a, _ = mix.render_track([{"kind": "tick", "at": 0.1, "gain": 0.5}], 0.5)
    b, _ = mix.render_track([{"kind": "tick", "at": 0.1, "gain": 0.5, "volume": 0.5}], 0.5)
    assert np.max(np.abs(b)) == pytest.approx(0.5 * np.max(np.abs(a)))


def test_track_is_deterministic_byte_for_byte():
    cues = mix.cues_from_timing([{"frame": 0, "kind": "impact"}, {"frame": 30, "kind": "whoosh"},
                                 {"frame": 60, "kind": "hit"}, {"frame": 80, "kind": "flash"}, {"frame": 120, "kind": "riser"}], 30)
    a, _ = mix.render_track(cues, 6.0)
    b, _ = mix.render_track(cues, 6.0)
    assert S.wav_bytes(a) == S.wav_bytes(b)
    cues2 = mix.cues_from_timing([{"frame": 0, "kind": "impact"}, {"frame": 30, "kind": "whoosh"}], 30, seed=99)
    c, _ = mix.render_track(cues2, 6.0)
    d, _ = mix.render_track(mix.cues_from_timing([{"frame": 0, "kind": "impact"}, {"frame": 30, "kind": "whoosh"}], 30, seed=0), 6.0)
    assert S.wav_bytes(c) != S.wav_bytes(d)


# ── ffmpeg ──

def _silent_mp4(path, sec=2.0):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=160x90:rate=30:duration={sec}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _streams(path):
    cp = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,sample_rate,channels",
                         "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(cp.stdout)["streams"]


@needs_ffmpeg
def test_mux_cues_adds_audio_and_keeps_video(tmp_path):
    video = _silent_mp4(tmp_path / "in.mp4")
    assert mix.has_audio(video) is False
    cues = [{"kind": "impact", "at": 0.1, "length": 0.8, "gain": 0.8}, {"kind": "riser", "at": 1.5, "length": 1.0}]
    res = mix.mux_cues(video, cues, tmp_path / "out.mp4")
    assert mix.has_audio(res["file"]) is True
    kinds = {s["codec_type"]: s for s in _streams(res["file"])}
    assert kinds["video"]["codec_name"] == "h264"            # 映像はそのまま（再エンコードしない）
    assert kinds["audio"]["codec_name"] == "aac" and kinds["audio"]["sample_rate"] == "48000" and kinds["audio"]["channels"] == 2
    assert res["duration"] == pytest.approx(2.0, abs=0.1)
    assert abs(mix.probe_duration(res["file"]) - 2.0) < 0.1
    assert res["loudness"]["integrated_lufs"] is not None and res["loudness"]["true_peak_dbfs"] < 0.0
    # 音の道（WAV）は 48kHz・モノラル・映像と同じ長さ
    w = wave.open(str(res["track"]))
    assert w.getframerate() == 48000 and w.getnchannels() == 1 and w.getnframes() == pytest.approx(2.0 * 48000, abs=2400)


@needs_ffmpeg
def test_mux_job_adds_sfx_to_a_finished_remotion_job(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    work = shared / "motion" / "_work"
    monkeypatch.setattr(config, "SHARED_DIR", shared)
    monkeypatch.setattr(config, "WORK_DIR", work)
    jid = "20261011-000000-abc123"
    d = work / jid
    d.mkdir(parents=True)
    _silent_mp4(d / "out.mp4", 2.0)
    (d / "job.json").write_text(json.dumps({"job_id": jid, "kind": "render", "status": "done"}), encoding="utf-8")
    props = {"fps": 30, "sfx": [{"frame": 0, "kind": "impact"}, {"frame": 15, "kind": "whoosh"}, {"frame": 18, "kind": "hit"},
                                {"frame": 20, "kind": "hit"}, {"frame": 59, "kind": "riser"}]}
    (d / "props.json").write_text(json.dumps(props), encoding="utf-8")

    res = mix.mux_job(jid)
    assert res["file"] == f"motion/_work/{jid}/out_sfx.mp4" and res["url"] == f"/files/{res['file']}"
    assert (d / "out_sfx.mp4").is_file() and (d / "sfx_cues.json").is_file()
    assert mix.has_audio(d / "out_sfx.mp4")
    assert res["cue_count"] == 4 and res["dropped_count"] == 1          # 近い2つの hit は1つに間引かれる
    assert (d / "out.mp4").is_file() and mix.has_audio(d / "out.mp4") is False   # 元の無音の mp4 は残す
    # 同じ入力なら音の道はバイト一致
    first = (d / "sfx_track.wav").read_bytes()
    mix.mux_job(jid)
    assert (d / "sfx_track.wav").read_bytes() == first

    # v1 の props（timing の中に sfx）も読む
    (d / "props.json").write_text(json.dumps({"timing": {"fps": 30, "sfx": [{"frame": 0, "kind": "impact"}]}}), encoding="utf-8")
    assert mix.mux_job(jid)["cue_count"] == 1


def test_mux_job_rejects_wrong_jobs(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    work = shared / "motion" / "_work"
    monkeypatch.setattr(config, "SHARED_DIR", shared)
    monkeypatch.setattr(config, "WORK_DIR", work)
    for jid, job in (("j_hf", {"kind": "render", "status": "done", "engine": "hyperframes"}),
                     ("j_stills", {"kind": "stills", "status": "done"}),
                     ("j_run", {"kind": "render", "status": "running"})):
        (work / jid).mkdir(parents=True)
        (work / jid / "job.json").write_text(json.dumps({"job_id": jid, **job}), encoding="utf-8")
        with pytest.raises(ValueError):
            mix.mux_job(jid)
    with pytest.raises(FileNotFoundError):
        mix.mux_job("nope")
    (work / "j_files").mkdir()
    (work / "j_files" / "job.json").write_text(json.dumps({"job_id": "j_files", "kind": "render", "status": "done"}), encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        mix.mux_job("j_files")
    for bad in ("../x", "a/b", ""):
        with pytest.raises(ValueError):
            mix.mux_job(bad)
