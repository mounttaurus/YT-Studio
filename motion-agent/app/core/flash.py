"""明滅（光過敏）の検査。WCAG 2.3.1「一般閃光」を簡略にした基準:

  ・相対輝度（sRGB→線形→0.2126R+0.7152G+0.0722B）が 0.1 以上変わり、暗い側が 0.8 未満の変化を「遷移」とする
  ・画面の 25% 以上で同時に起きた遷移だけを数える（一部だけの明滅は数えない）
  ・向きが変わるか2フレーム以上空いたら別の「ラン」。1秒の窓の中の、明るくなるランと暗くなるランの
    多い方を「閃光の回数」とし、どの1秒でも 3 回以下であること（赤の閃光の規定は今回は入れない）

2か所で同じ数え方を使う: 概算（演出プランの宣言から・plan_check）と実測（描いた mp4 から・analyze_video）。
"""
from __future__ import annotations

import subprocess
from typing import Iterable

import numpy as np

MAX_PER_SEC = 3
DELTA = 0.1          # 遷移とみなす相対輝度の変化
DARK_LIMIT = 0.8     # 暗い側がこれ未満の変化だけ数える
AREA = 0.25          # 画面のこの割合以上で同時に起きた遷移だけ数える
MERGE_GAP = 2        # 同じ向きがこのフレーム以内に続けば1つのラン
SMALL = (96, 54)     # 実測は縮小して読む（速さのため。25% 判定には十分）


def hex_luminance(color: str) -> float:
    """#rrggbb の相対輝度。"""
    c = color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return float(_rel_lum(np.array([r, g, b])))


def _rel_lum(rgb01: np.ndarray) -> np.ndarray:
    lin = np.where(rgb01 <= 0.03928, rgb01 / 12.92, ((rgb01 + 0.055) / 1.055) ** 2.4)
    return lin[..., 0] * 0.2126 + lin[..., 1] * 0.7152 + lin[..., 2] * 0.0722


def is_transition(y0: float, y1: float) -> bool:
    return abs(y1 - y0) >= DELTA and min(y0, y1) < DARK_LIMIT


def runs_from_events(events: list[tuple[int, int]], gap: int = MERGE_GAP) -> list[tuple[int, int]]:
    """(フレーム, 向き±1) の並びを、同じ向きが gap フレーム以内に続くものを束ねて (開始フレーム, 向き) のランにする。"""
    runs: list[tuple[int, int]] = []
    last_frame, last_sign = None, 0
    for f, s in sorted(events):
        if runs and s == last_sign and last_frame is not None and f - last_frame <= gap:
            last_frame = f
            continue
        runs.append((f, s))
        last_frame, last_sign = f, s
    return runs


def count_flashes(runs: list[tuple[int, int]], fps: float) -> dict:
    """1秒の窓ごとの閃光の回数の最大。{max_per_sec, worst_at_sec}"""
    best, at = 0, 0.0
    for f0, _ in runs:
        up = sum(1 for f, s in runs if f0 <= f < f0 + fps and s > 0)
        down = sum(1 for f, s in runs if f0 <= f < f0 + fps and s < 0)
        n = max(up, down)
        if n > best:
            best, at = n, f0 / fps
    return {"max_per_sec": best, "worst_at_sec": round(at, 3)}


def verdict(runs: list[tuple[int, int]], fps: float) -> dict:
    c = count_flashes(runs, fps)
    return {"ok": c["max_per_sec"] <= MAX_PER_SEC, **c, "limit_per_sec": MAX_PER_SEC,
            "events": [{"sec": round(f / fps, 3), "dir": "brighter" if s > 0 else "darker"} for f, s in runs][:60]}


def analyze_frames(frames: Iterable[np.ndarray], fps: float) -> dict:
    """frames は (H, W, 3) の uint8 の並び。"""
    events: list[tuple[int, int]] = []
    prev = None
    n = 0
    for i, fr in enumerate(frames):
        n = i + 1
        y = _rel_lum(fr.astype(np.float64) / 255.0)
        if prev is not None:
            d = y - prev
            mask = (np.abs(d) >= DELTA) & (np.minimum(y, prev) < DARK_LIMIT)
            if mask.mean() >= AREA:
                up = int((d[mask] > 0).sum())
                down = int((d[mask] < 0).sum())
                events.append((i, 1 if up >= down else -1))
        prev = y
    out = verdict(runs_from_events(events), fps)
    out["frames"] = n
    return out


def analyze_video(path: str, fps: float = 30.0) -> dict:
    """mp4 を縮小して読み、明滅を実測する。ffmpeg が使えなければ {ok: None, error}。"""
    w, h = SMALL
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={w}:{h}:flags=area,format=rgb24",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300, check=True)
    except (subprocess.SubprocessError, OSError) as e:
        return {"ok": None, "error": f"明滅の実測に失敗しました: {e}"}
    buf = np.frombuffer(proc.stdout, dtype=np.uint8)
    size = w * h * 3
    count = len(buf) // size
    frames = (buf[i * size:(i + 1) * size].reshape(h, w, 3) for i in range(count))
    return analyze_frames(frames, fps)
