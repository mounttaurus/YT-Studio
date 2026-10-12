"""打点の並び → 1本の音の道（WAV）・無音の mp4 との結合（ffmpeg）。エンジンに依存しない。
設計: Docs/MOTION_UNIFIED_PLAN.md §9-1（Remotion の型は ffmpeg でミックスして無音の mp4 と合わせる）・
Docs/OPENING_MOTION_PLAN.md M2（ナレーション・BGM・ダッキング・ラウドネスは M2 で足す。ここは効果音だけ）。

打点の形（cue）:
    {"kind": "whoosh", "at": 1.25, "length": 0.5, "pitch": 1400, "gain": 0.6, "seed": 3, "volume": 1.0}
    kind は sfx_synth.PARTS の部品。`at` は秒で、部品の基準点（sfx_synth.anchor）がここに来る
    （頭が打点＝既定。riser・suck は**終わり**が打点＝打点から逆算して置く）。
    volume は置く側の倍率（既定 1）。timing の効果音（{"frame", "kind"}）は `cues_from_timing` で変える。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

from app.core import config
from app.core.audio import sfx_synth as synth

# 同じ種類の打点がこの秒より近ければ後ろを間引く（OPENING_MOTION_PLAN §14-3「③で密になりすぎる」への当座の規則。
# M2 で音の組み立てと一緒に見直す）
DEFAULT_MIN_GAP_SEC = 0.1
# 下に敷く BGM（予告編キット）の音量。キットのピークは 0.9 なので、効果音（0.5〜0.9）の下に収まる
DEFAULT_BED_GAIN = 0.55
# AAC に通した後の真のピークの上限。超えたら下げて入れ直す（mux_cues）。最終のラウドネスは M2 が決める
TRUE_PEAK_LIMIT_DBFS = -1.0
# 出力の上限（足し合わせてこれを超えたら全体を一律に下げる）。AAC に通すと真のピークが 0.4dB ほど超えるので、
# 0.98（実測で +0.4 dBTP）ではなく -1.4 dBFS まで。最終のラウドネスは M2 が決める
CEILING = 0.85

# 暫定の対応表: Remotion の型の効果音の kind（timing.py の sfx）→ 部品と引数。A2 の予告編キットで置き換える
CUE_PARTS: dict[str, tuple[str, dict]] = {
    "impact": ("impact", {"length": 1.4, "pitch": 58.0, "gain": 0.9}),
    "whoosh": ("whoosh", {"length": 0.5, "pitch": 1400.0, "gain": 0.6}),
    "hit": ("stamp", {"length": 0.14, "pitch": 140.0, "gain": 0.5}),
    "flash": ("suck", {"length": 0.8, "pitch": 3000.0, "gain": 0.7}),
    "riser": ("riser", {"length": 1.4, "pitch": 140.0, "gain": 0.75}),
}


# AAC の前に通す先読みのリミッター（山の間の「見えない山」＝標本の間の頂を抑える）。level=0 で音量を持ち上げない。
# 0.8＝-1.9dBFS。AAC が戻す分（約1dB）の余白を残す。最終のラウドネスは M2 が決める
LIMITER = "alimiter=limit=0.8:attack=5:release=60:level=0"


class MixError(RuntimeError):
    pass


# ── 打点 ──

def cues_from_timing(sfx: list[dict], fps: int, seed: int = 0) -> list[dict]:
    """timing の効果音 [{"frame", "kind"}] → 打点。同じ種類の2つ目以降は種をずらす（同じ音の連打にしない）。"""
    out: list[dict] = []
    count: dict[str, int] = {}
    for c in sorted(sfx, key=lambda x: (x["frame"], x["kind"])):
        part = CUE_PARTS.get(c["kind"])
        if part is None:
            continue
        kind, params = part
        i = count.get(c["kind"], 0)
        count[c["kind"]] = i + 1
        out.append({"kind": kind, "at": round(c["frame"] / fps, 6), **params, "seed": int(seed) + i, "source": c["kind"]})
    return out


def thin_cues(cues: list[dict], min_gap_sec: float = DEFAULT_MIN_GAP_SEC) -> tuple[list[dict], list[dict]]:
    """同じ部品の打点が min_gap_sec より近ければ後ろを間引く。返り値: (残す, 落とす)。"""
    kept: list[dict] = []
    dropped: list[dict] = []
    last: dict[str, float] = {}
    for c in sorted(cues, key=lambda x: x["at"]):
        k = c["kind"]
        if min_gap_sec > 0 and k in last and c["at"] - last[k] < min_gap_sec - 1e-9:
            dropped.append(c)
            continue
        last[k] = c["at"]
        kept.append(c)
    return kept, dropped


def cue_params(cue: dict) -> dict:
    return synth.normalize_params(cue["kind"], **{k: cue[k] for k in ("length", "pitch", "gain", "seed") if k in cue})


def cue_start(cue: dict, params: dict | None = None) -> float:
    """WAV の頭の時刻（秒）。終わりが打点の部品は at − length。"""
    p = params or cue_params(cue)
    at = float(cue["at"])
    return at - p["length"] if synth.anchor(cue["kind"]) == "end" else at


# ── 音の道 ──

def soft_limit(x: np.ndarray, ceiling: float, knee: float = 0.7) -> np.ndarray:
    """柔らかい上限。knee×ceiling までは触らず、それを超えた山だけを ceiling へ滑らかに丸める
    （全体を一律に下げると、BGM を敷いた時に音楽全体が小さくなる）。"""
    k = ceiling * knee
    a = np.abs(x)
    over = a > k
    if not np.any(over):
        return x
    y = x.copy()
    y[over] = np.sign(x[over]) * (k + (ceiling - k) * np.tanh((a[over] - k) / (ceiling - k)))
    return y


def render_track(cues: list[dict], total_sec: float, bed: np.ndarray | None = None,
                 bed_gain: float = 1.0) -> tuple[np.ndarray, dict]:
    """打点を全部置いた 48kHz・モノラルの音の道。返り値: (波形, 報告)。
    bed は下に敷く音（BGM＝予告編キット）。道より長ければ切り、短ければ後ろは無音。
    道の外へはみ出す部分は切る。足し合わせて CEILING を超えたら全体を一律に下げる。"""
    n = max(1, int(round(float(total_sec) * synth.SR)))
    out = np.zeros(n)
    if bed is not None:
        m = min(n, len(bed))
        out[:m] += float(bed_gain) * np.asarray(bed[:m], dtype=np.float64)
    cache: dict[tuple, np.ndarray] = {}
    placed: list[dict] = []
    for c in cues:
        p = cue_params(c)
        key = (c["kind"], p["length"], p["pitch"], p["gain"], p["seed"])
        x = cache.get(key)
        if x is None:
            x = cache[key] = synth.synth(c["kind"], **p)
        start = cue_start(c, p)
        i0 = int(round(start * synth.SR))
        a, b = max(0, i0), min(n, i0 + len(x))
        if b > a:
            out[a:b] += float(c.get("volume", 1.0)) * x[a - i0:b - i0]
        placed.append({"kind": c["kind"], "at": round(float(c["at"]), 4), "start": round(start, 4),
                       "length": p["length"], "clipped": bool(i0 < 0 or i0 + len(x) > n)})
    peak_in = float(np.max(np.abs(out))) if n else 0.0
    out = soft_limit(out, CEILING)
    peak = float(np.max(np.abs(out))) if n else 0.0
    # scaled: 山の頂が上限で何割に丸められたか（1.0＝触っていない）
    scale = round(peak / peak_in, 4) if peak_in > CEILING else 1.0
    return out, {"cues": placed, "peak": round(peak, 4), "scaled": scale}


# ── ffmpeg ──

def _ffmpeg(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise MixError("ffmpeg が見つからない")
    cp = subprocess.run([exe, "-y", "-hide_banner", *args], capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=timeout)
    if cp.returncode != 0:
        raise MixError(f"ffmpeg が失敗した: {(cp.stderr or '')[-800:]}")
    return cp


def probe_duration(path: Path) -> float:
    exe = shutil.which("ffprobe")
    if exe is None:
        raise MixError("ffprobe が見つからない")
    cp = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                        capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        return float(json.loads(cp.stdout)["format"]["duration"])
    except (ValueError, KeyError) as e:
        raise MixError(f"長さを読めない: {path}") from e


def has_audio(path: Path) -> bool:
    exe = shutil.which("ffprobe")
    if exe is None:
        raise MixError("ffprobe が見つからない")
    cp = subprocess.run([exe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name",
                         "-of", "csv=p=0", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    return bool(cp.stdout.strip())


_LUFS_RE = re.compile(r"\bI:\s+(-?\d+(?:\.\d+)?)\s+LUFS")
_PEAK_RE = re.compile(r"\bPeak:\s+(-?\d+(?:\.\d+)?)\s+dBFS")


def loudness(path: Path) -> dict:
    """統合ラウドネス（LUFS）と真のピーク（dBFS）。ebur128 の最後の要約を読む。"""
    cp = _ffmpeg(["-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
    lufs = _LUFS_RE.findall(cp.stderr)
    peak = _PEAK_RE.findall(cp.stderr)
    return {"integrated_lufs": float(lufs[-1]) if lufs else None, "true_peak_dbfs": float(peak[-1]) if peak else None}


def mux(video: Path, wav: Path, out: Path, audio_bitrate: str = "192k") -> Path:
    """映像はそのまま（-c:v copy）、音の道を AAC・48kHz・ステレオで足す。長さは映像に合わせる。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(["-i", str(video), "-i", str(wav), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
             "-af", LIMITER, "-c:a", "aac", "-b:a", audio_bitrate, "-ar", str(synth.SR), "-ac", "2", "-shortest",
             "-fflags", "+bitexact", "-flags:a", "+bitexact", "-movflags", "+faststart", str(out)])
    return out


def mux_cues(video: Path, cues: list[dict], out: Path, track_wav: Path | None = None,
             min_gap_sec: float = DEFAULT_MIN_GAP_SEC, bed: np.ndarray | None = None,
             bed_gain: float = DEFAULT_BED_GAIN) -> dict:
    """打点（と下に敷く BGM）を音の道にして映像へ足す。
    返り値: {file, track, duration, cues, dropped, peak, scaled, loudness}。"""
    total = probe_duration(video)
    kept, dropped = thin_cues(cues, min_gap_sec)
    track, report = render_track(kept, total, bed=bed, bed_gain=bed_gain)
    wav = track_wav or out.with_suffix(".wav")
    wav.parent.mkdir(parents=True, exist_ok=True)
    base = track
    wav.write_bytes(synth.wav_bytes(base))
    mux(video, wav, out)
    loud = loudness(out)
    trim_db = 0.0
    for _ in range(3):
        # AAC に通すと波形の山が戻ってくる（密な音ほど大きく、下げても単調には下がらない）。
        # 測って、超えていれば超えた分＋余白だけ下げて入れ直す（最大3回）
        tp = loud.get("true_peak_dbfs")
        if tp is None or tp <= TRUE_PEAK_LIMIT_DBFS:
            break
        trim_db -= (tp - TRUE_PEAK_LIMIT_DBFS) + 0.3
        track = base * (10.0 ** (trim_db / 20.0))
        wav.write_bytes(synth.wav_bytes(track))
        mux(video, wav, out)
        loud = loudness(out)
    return {"file": out, "track": wav, "duration": round(total, 3), "cues": report["cues"],
            "dropped": [{"kind": c["kind"], "at": c["at"]} for c in dropped],
            "peak": round(float(np.max(np.abs(track))), 4), "scaled": report["scaled"],
            "trim_db": round(trim_db, 2), "loudness": loud}


# ── Remotion の描画ジョブへ足す口 ──

def _job_sfx(props: dict) -> tuple[list[dict], int]:
    """描画ジョブの props から効果音の打点（フレーム）と fps。v2 は props.sfx、v1 は props.timing.sfx。"""
    timing = props.get("timing") or {}
    sfx = props.get("sfx") or timing.get("sfx") or []
    fps = int(props.get("fps") or timing.get("fps") or 30)
    return list(sfx), fps


def mux_job(job_id: str, min_gap_sec: float = DEFAULT_MIN_GAP_SEC, seed: int = 0, bgm: str | None = None,
            bgm_gain: float = DEFAULT_BED_GAIN) -> dict:
    """終わった Remotion の描画ジョブ（無音の out.mp4）に、その props の効果音（と BGM）を足して
    out_sfx.mp4（bgm があれば out_sfx_{雰囲気}.mp4）を作る。置く場所は同じジョブのフォルダ
    （sfx_track*.wav・sfx_cues.json・bgm_{雰囲気}.wav も）。映像は触らない。
    bgm は予告編キットの雰囲気（kit.MOODS）。演出プラン（型 v2・bpm あり）の描画だけ使える。"""
    from app.core import jobs  # 循環 import を避ける
    if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
        raise ValueError(f"job_id が正しくない: {job_id!r}")
    job = jobs.get(job_id)
    if job is None:
        raise FileNotFoundError(f"ジョブが無い: {job_id}")
    if job.get("engine") == "hyperframes":
        raise ValueError("HyperFrames の描画は <audio> を使って描画がミックスする（motion.json の sfx）。ここは Remotion の型用")
    if job.get("kind") != "render" or job.get("status") != "done":
        raise ValueError(f"終わった本描画のジョブだけ（kind={job.get('kind')}・status={job.get('status')}）")
    d = config.WORK_DIR / job_id
    video, props_path = d / "out.mp4", d / "props.json"
    if not video.is_file() or not props_path.is_file():
        raise FileNotFoundError(f"out.mp4 か props.json が無い: {job_id}")
    props = json.loads(props_path.read_text(encoding="utf-8"))
    sfx, fps = _job_sfx(props)
    cues = cues_from_timing(sfx, fps, seed=seed)
    bed, bgm_report, suffix = None, None, ""
    if bgm:
        from app.core import timing
        from app.core.audio import kit
        if kit.MOODS.get(bgm) is None:
            raise ValueError(f"bgm は {sorted(kit.MOODS)} のどれか: {bgm!r}")
        if not props.get("bpm") or not props.get("shots"):
            raise ValueError("bgm は演出プラン（型 v2・bpm とショットの並びがある描画）だけ")
        bed, bgm_report = kit.render(timing.audiomap_from_props(props), bgm, fps=fps, seed=seed)
        suffix = f"_{bgm}"
        (d / f"bgm_{bgm}.wav").write_bytes(synth.wav_bytes(bed))
    res = mux_cues(video, cues, d / f"out_sfx{suffix}.mp4", track_wav=d / f"sfx_track{suffix}.wav",
                   min_gap_sec=min_gap_sec, bed=bed, bed_gain=bgm_gain)
    (d / "sfx_cues.json").write_text(json.dumps({"fps": fps, "cues": res["cues"], "dropped": res["dropped"]},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
    return {"job_id": job_id, "file": jobs.rel(res["file"]), "url": f"/files/{jobs.rel(res['file'])}",
            "track": jobs.rel(res["track"]), "duration": res["duration"], "cue_count": len(res["cues"]),
            "dropped_count": len(res["dropped"]), "cues": res["cues"], "dropped": res["dropped"],
            "peak": res["peak"], "scaled": res["scaled"], "trim_db": res["trim_db"], "loudness": res["loudness"],
            "bgm": bgm_report}
