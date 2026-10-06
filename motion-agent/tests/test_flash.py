import subprocess

import numpy as np

from app.core import flash

W, H = 96, 54


def _frames(fn, n):
    for i in range(n):
        yield fn(i)


def _solid(v):
    return np.full((H, W, 3), v, dtype=np.uint8)


def test_hard_5hz_strobe_fails():
    # 30fps・3フレームごとに白黒 → 毎秒5回の明滅
    res = flash.analyze_frames(_frames(lambda i: _solid(255 if (i // 3) % 2 else 0), 60), 30)
    assert res["ok"] is False and res["max_per_sec"] >= 4


def test_2hz_passes():
    res = flash.analyze_frames(_frames(lambda i: _solid(255 if (i // 8) % 2 else 0), 90), 30)
    assert res["ok"] is True and res["max_per_sec"] <= 3


def test_partial_screen_flash_is_not_counted():
    def fn(i):
        f = _solid(0)
        if (i // 3) % 2:
            f[:, : W // 5] = 255  # 画面の 20% だけ明滅
        return f
    res = flash.analyze_frames(_frames(fn, 60), 30)
    assert res["ok"] is True and res["max_per_sec"] == 0


def test_a_single_fade_to_white_is_one_flash_at_most():
    res = flash.analyze_frames(_frames(lambda i: _solid(min(255, i * 40)), 30), 30)
    assert res["ok"] is True and res["max_per_sec"] <= 1


def test_slow_dim_changes_are_ignored():
    res = flash.analyze_frames(_frames(lambda i: _solid(100 + (i % 20)), 60), 30)
    assert res["max_per_sec"] == 0


def test_runs_and_counting_on_events():
    # 0.1秒おきに交互 → 1秒に 5 回明るくなる
    ev = [(i * 3, 1 if i % 2 == 0 else -1) for i in range(20)]
    c = flash.count_flashes(flash.runs_from_events(ev), 30)
    assert c["max_per_sec"] >= 5
    ev = [(i * 15, 1 if i % 2 == 0 else -1) for i in range(8)]  # 0.5秒おき → 1秒に1回
    assert flash.count_flashes(flash.runs_from_events(ev), 30)["max_per_sec"] <= 1


def test_luminance_helpers():
    assert flash.hex_luminance("#ffffff") > 0.99 and flash.hex_luminance("#000000") == 0
    assert flash.is_transition(0.0, 1.0) and not flash.is_transition(0.5, 0.55)
    assert not flash.is_transition(0.85, 1.0)  # 暗い側が 0.8 以上


def test_video_end_to_end_strobe_vs_calm(tmp_path):
    def make(name, period):
        raw = b"".join(_solid(255 if (i // period) % 2 else 0).tobytes() for i in range(60))
        out = tmp_path / name
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                        "-r", "30", "-i", "-", "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(out)],
                       input=raw, check=True)
        return str(out)
    assert flash.analyze_video(make("strobe.mp4", 3))["ok"] is False
    assert flash.analyze_video(make("calm.mp4", 15))["ok"] is True
    assert flash.analyze_video(str(tmp_path / "none.mp4"))["ok"] is None
