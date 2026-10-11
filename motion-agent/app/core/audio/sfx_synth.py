"""効果音の手続き合成の部品集（Docs/MOTION_UNIFIED_PLAN.md §9-2・A0）。

numpy だけで 48kHz・16bit・モノラルの WAV を作る。外部の音源・ライブラリ・ネットワークは使わない（M3・M10）。

    whoosh   帯域を動かした雑音＋山なりの音量        語の切り替え・繋ぎ
    riser    音程と帯域の上昇。打点で頂点（終わり）  ③の終わり→④
    impact   低い一撃＋短い雑音＋共鳴＋余韻          ①の飛び込み・⑤
    stamp    鈍い打撃＋紙の擦れ                      判子
    tick     短いクリック                            解読の文字送り
    glitch   ビットを落とした断片                    HUD・乱れ
    braam    低音の和音を歪ませた持続                タイトル
    suck     逆再生のシンバル風                      無音へ吸い込む直前

全て (length, pitch, gain, seed) を取る:
    length  秒。WAV の長さそのもの（sample 数は round(length * 48000)）
    pitch   Hz。部品ごとの意味は PARTS の `pitch` 欄（whoosh は山の頂での帯域の中心、impact は終わりの低音…）
    gain    0〜1。**WAV のピーク**（フルスケール比）。音量の釣り合いはここと、置く側の volume で取る
    seed    整数。雑音・断片の並びの種。同じ引数なら同じ WAV（M2）

乱数は numpy の乱数を使わず、整数のハッシュ（splitmix64）で作る。numpy の版が変わっても雑音の並びは変わらない。
同じ環境（同じコンテナ）で同じ引数なら WAV はバイト一致する。別の CPU・別の numpy の版では、三角関数の
最後の桁の差で 16bit の量子化が1段ずれることがありうる（なので固定のハッシュ値をテストに書かない）。

`VERSION` は合成の式を変えた時に上げる。置いた WAV を作り直す目印。
"""
from __future__ import annotations

import inspect
import io
import wave
from pathlib import Path
from typing import Callable

import numpy as np

SR = 48000
VERSION = 1

MIN_LENGTH = 0.01
MAX_LENGTH = 30.0
MIN_PITCH = 20.0
MAX_PITCH = 16000.0

_MASK64 = 0xFFFFFFFFFFFFFFFF


# ── 土台: 雑音・フィルタ・仕上げ ──

def _mix64(z: np.ndarray) -> np.ndarray:
    """splitmix64 の仕上げ。uint64 の配列を混ぜる（桁あふれは折り返す）。"""
    with np.errstate(over="ignore"):
        z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        return z ^ (z >> np.uint64(31))


def _hash64(x: int) -> int:
    """splitmix64 の仕上げ（Python の整数）。"""
    x &= _MASK64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _MASK64
    return x ^ (x >> 31)


def noise(n: int, seed: int, stream: int = 0) -> np.ndarray:
    """[-1, 1) の白色雑音。seed と stream（部品の中の用途の番号）で決まる。
    鍵を先にハッシュするので、seed や stream が違えば並びは無関係になる（ずらしただけの同じ並びにならない）。"""
    key = _hash64(_hash64(int(seed)) ^ ((int(stream) + 1) * 0xD1B54A32D192ED03))
    with np.errstate(over="ignore"):
        z = _mix64(np.arange(n, dtype=np.uint64) * np.uint64(0x9E3779B97F4A7C15) + np.uint64(key))
    u = (z >> np.uint64(11)).astype(np.float64) / float(1 << 53)   # [0, 1)
    return u * 2.0 - 1.0


def _unit(n: int, seed: int, stream: int) -> np.ndarray:
    """[0, 1) の乱数 n 個（断片の長さ・音程の選び方に使う）。"""
    return (noise(n, seed, stream) + 1.0) * 0.5


def _count(length: float) -> int:
    return max(1, int(round(float(length) * SR)))


def _times(n: int) -> np.ndarray:
    return np.arange(n, dtype=np.float64) / SR


def _phase(freq_hz: np.ndarray) -> np.ndarray:
    """瞬間周波数（Hz）の並び → 位相（rad）。"""
    return 2.0 * np.pi * np.cumsum(freq_hz) / SR


def _ramp(t: np.ndarray, sec: float) -> np.ndarray:
    """0→1 へ滑らかに立ち上がる（半周期の正弦）。"""
    return np.sin(0.5 * np.pi * np.clip(t / max(sec, 1e-6), 0.0, 1.0))


def _stft_filter(x: np.ndarray, resp: Callable[[np.ndarray, np.ndarray], np.ndarray],
                 n_fft: int = 2048, hop: int = 512) -> np.ndarray:
    """時間で変わる周波数応答を掛ける（STFT・重ね合わせ）。
    resp(frame_center_sec, freqs_hz) → (frames, bins) の倍率。"""
    n = len(x)
    pad = n_fft
    xp = np.concatenate([np.zeros(pad), x, np.zeros(pad + n_fft)])
    n_frames = (len(xp) - n_fft) // hop + 1
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    win = np.hanning(n_fft + 1)[:-1]
    spec = np.fft.rfft(xp[idx] * win, axis=1)
    centers = (hop * np.arange(n_frames) + n_fft / 2.0 - pad) / SR
    freqs = np.fft.rfftfreq(n_fft, 1.0 / SR)
    spec *= resp(centers, freqs)
    frames = np.fft.irfft(spec, n=n_fft, axis=1)
    y = np.zeros(len(xp))
    for i in range(n_frames):
        y[i * hop:i * hop + n_fft] += frames[i]
    y /= (n_fft / hop) / 2.0   # Hann・重なり率で和が一定になる分
    return y[pad:pad + n]


def _band(fc: Callable[[np.ndarray], np.ndarray], sigma_oct: float):
    """中心周波数 fc(t)・幅 sigma_oct（オクターブ）の山型の帯域。"""
    def resp(tc: np.ndarray, f: np.ndarray) -> np.ndarray:
        c = np.maximum(fc(tc), 20.0)[:, None]
        r = np.log2(np.maximum(f, 1.0)[None, :] / c) / sigma_oct
        return np.exp(-0.5 * r * r)
    return resp


def _lowpass(fc: float, order: int = 2):
    def resp(tc: np.ndarray, f: np.ndarray) -> np.ndarray:
        return np.broadcast_to(1.0 / np.sqrt(1.0 + (f / fc) ** (2 * order)), (len(tc), len(f)))
    return resp


def _highpass(fc: float, order: int = 2):
    def resp(tc: np.ndarray, f: np.ndarray) -> np.ndarray:
        r = 1.0 / np.sqrt(1.0 + (fc / np.maximum(f, 1.0)) ** (2 * order))
        return np.broadcast_to(r, (len(tc), len(f)))
    return resp


def _finish(x: np.ndarray, gain: float, fade_in: float = 0.0005, fade_out: float = 0.002) -> np.ndarray:
    """直流を除き、頭と終わりを0へ絞り、ピークを gain に揃える。"""
    x = np.asarray(x, dtype=np.float64).copy()
    x -= x.mean()
    n = len(x)
    fi = min(n // 2, max(1, int(fade_in * SR)))
    fo = min(n // 2, max(1, int(fade_out * SR)))
    x[:fi] *= np.linspace(0.0, 1.0, fi, endpoint=False)
    x[-fo:] *= np.linspace(1.0, 0.0, fo)
    peak = float(np.max(np.abs(x)))
    if peak > 0.0:
        x *= min(max(float(gain), 0.0), 1.0) / peak
    return x


# ── 部品 ──

def whoosh(length: float = 0.6, pitch: float = 1400.0, gain: float = 0.8, seed: int = 0) -> np.ndarray:
    """帯域が下から上へ動く雑音。音量は山なり（頂は4割あたり）。pitch は頂での帯域の中心。"""
    n = _count(length)
    t = _times(n)
    u = t / (n / SR)
    x = noise(n, seed, 0)
    top = _stft_filter(x, _band(lambda tc: pitch * 2.0 ** ((np.clip(tc / (n / SR), 0, 1) - 0.4) * 3.5), 0.9))
    body = _stft_filter(noise(n, seed, 1), _band(lambda tc: pitch * 0.25 * 2.0 ** ((np.clip(tc / (n / SR), 0, 1) - 0.4) * 2.0), 0.7))
    env = (u ** 1.3) * ((1.0 - u) ** 2.0)
    env /= env.max() if env.max() > 0 else 1.0
    return _finish((top + 0.45 * body) * env, gain, fade_in=0.003, fade_out=0.004)


def riser(length: float = 1.5, pitch: float = 140.0, gain: float = 0.8, seed: int = 0) -> np.ndarray:
    """音程と帯域が上がり続け、**WAV の終わり＝打点で頂点**（音量も最大）。
    打点から逆算して置く: 開始 = 打点 − length。pitch は始めの音程（終わりでその8倍）。"""
    n = _count(length)
    t = _times(n)
    u = t / (n / SR)
    f0 = pitch * 2.0 ** (3.0 * u ** 1.4)
    tone = np.zeros(n)
    for cents, amp in ((-14.0, 0.7), (0.0, 1.0), (11.0, 0.7)):
        ph = _phase(f0 * 2.0 ** (cents / 1200.0))
        for k in (1, 2, 3, 4):
            w = np.clip((18000.0 - k * f0) / 3000.0, 0.0, 1.0)   # ナイキストへ近づく倍音は絞る
            tone += amp * w * np.sin(k * ph) / k
    hiss = _stft_filter(noise(n, seed, 0), _band(lambda tc: 350.0 * 2.0 ** (5.0 * np.clip(tc / (n / SR), 0, 1)), 1.1))
    mix = 0.6 * tone / max(1e-9, float(np.std(tone))) + 0.45 * hiss / max(1e-9, float(np.std(hiss)))
    # 瞬間の山を揃えてから音量の曲線を掛ける: 曲線が最大の終わりに、確実に頂点が来る
    limited = np.tanh(1.5 * mix / max(1e-9, float(np.std(mix))))
    env = (0.03 + 0.97 * u ** 2.2) * (1.0 + 0.6 * u ** 10)
    return _finish(limited * env, gain, fade_in=0.01, fade_out=0.0015)


def impact(length: float = 1.4, pitch: float = 58.0, gain: float = 0.9, seed: int = 0) -> np.ndarray:
    """低い一撃。pitch は落ち着く低音。頭の一瞬は高い所から落ちる（ドンと鳴る）。"""
    n = _count(length)
    t = _times(n)
    scale = max(length, 0.2) / 1.4
    body = np.sin(_phase(pitch * (1.0 + 3.0 * np.exp(-t / 0.04)))) * np.exp(-t / (0.26 * scale + 0.05))
    crack = _stft_filter(noise(n, seed, 0), _band(lambda tc: np.full_like(tc, 2200.0), 1.2)) * np.exp(-t / 0.018)
    thump = _stft_filter(noise(n, seed, 1), _lowpass(150.0, 2)) * np.exp(-t / 0.11)
    modes = np.zeros(n)
    for i, (ratio, amp, tau) in enumerate(((2.4, 0.30, 0.20), (3.9, 0.20, 0.14), (6.3, 0.11, 0.09))):
        ph0 = float(_unit(1, seed, 10 + i)[0]) * 2.0 * np.pi
        modes += amp * np.sin(2.0 * np.pi * pitch * ratio * t + ph0) * np.exp(-t / (tau * scale + 0.02))
    tail = _stft_filter(noise(n, seed, 2), _lowpass(220.0, 2)) * np.exp(-t / (0.34 * scale + 0.05))
    s = lambda a: a / max(1e-9, float(np.max(np.abs(a))))   # noqa: E731 — 成分ごとにピークを揃えてから混ぜる
    x = 1.0 * body + 0.45 * s(crack) + 0.35 * s(thump) + modes + 0.28 * s(tail)
    return _finish(np.tanh(1.7 * x), gain)


def stamp(length: float = 0.4, pitch: float = 110.0, gain: float = 0.85, seed: int = 0) -> np.ndarray:
    """判子の「ポン」。鈍い低音の打撃に、紙が擦れる高域の雑音が少し遅れて乗る。"""
    n = _count(length)
    t = _times(n)
    thud = np.sin(_phase(pitch * (1.0 + 1.3 * np.exp(-t / 0.018)))) * np.exp(-t / 0.075)
    knock = 0.4 * np.sin(2.0 * np.pi * pitch * 2.3 * t) * np.exp(-t / 0.028)
    scrape = _stft_filter(noise(n, seed, 0), _band(lambda tc: np.full_like(tc, 3200.0), 1.0))
    delay = 0.012
    sc_env = np.where(t >= delay, np.exp(-(t - delay) / 0.07), 0.0) * _ramp(t - delay, 0.004)
    scrape = scrape / max(1e-9, float(np.max(np.abs(scrape)))) * sc_env * 0.5
    click = _stft_filter(noise(n, seed, 1), _highpass(1500.0, 2)) * np.exp(-t / 0.0025)
    click = click / max(1e-9, float(np.max(np.abs(click)))) * 0.35
    return _finish(np.tanh(1.3 * (thud + knock + scrape + click)), gain, fade_in=0.0003, fade_out=0.004)


def tick(length: float = 0.05, pitch: float = 2400.0, gain: float = 0.5, seed: int = 0) -> np.ndarray:
    """短いクリック。減衰する正弦波に、頭の1ミリ秒の高域の雑音。"""
    n = _count(length)
    t = _times(n)
    ring = np.sin(2.0 * np.pi * pitch * t) * np.exp(-t / max(length / 5.0, 0.002))
    click = np.diff(noise(n + 1, seed, 0)) * np.exp(-t / 0.0007)
    return _finish(ring + 0.6 * click / 2.0, gain, fade_in=0.0002, fade_out=min(0.004, length / 3.0))


def glitch(length: float = 0.3, pitch: float = 900.0, gain: float = 0.6, seed: int = 0) -> np.ndarray:
    """ビットを落とした断片。短い区間ごとに音程が飛び、一部は同じ断片を繰り返し、一部は無音。"""
    n = _count(length)
    t = _times(n)
    out = np.zeros(n)
    steps = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)
    r = _unit(2048, seed, 0)
    i, pos = 0, 0
    while pos < n and i + 3 < len(r):
        seg = max(int(SR * (0.006 + 0.030 * r[i])), 8)
        end = min(n, pos + seg)
        kind = r[i + 1]
        if kind > 0.28:                       # 28% は無音の隙間
            f = pitch * steps[int(r[i + 2] * len(steps)) % len(steps)]
            if kind > 0.75:                   # 雑音の断片
                chunk = noise(end - pos, seed, 100 + i)
            else:                             # 矩形波の断片
                chunk = np.sign(np.sin(2.0 * np.pi * f * (t[pos:end] - t[pos])) + 1e-9)
            out[pos:end] = chunk * (0.6 + 0.4 * r[i + 3])
        pos, i = end, i + 4
    hold = 4 + int(_unit(1, seed, 1)[0] * 8)           # サンプルの保持（4〜11）
    out = np.repeat(out[::hold], hold)[:n]
    levels = 2.0 ** (3 + int(_unit(1, seed, 2)[0] * 2))  # 3〜4 bit
    out = np.round(out * levels) / levels
    return _finish(out, gain, fade_in=0.0005, fade_out=0.003)


def braam(length: float = 2.5, pitch: float = 55.0, gain: float = 0.7, seed: int = 0) -> np.ndarray:
    """低い和音（根音・5度・オクターブ・1オクターブ下）の鋸歯を歪ませた持続。頭で少しだけ音程が下がる。"""
    n = _count(length)
    t = _times(n)
    sag = 1.0 + 0.035 * np.exp(-t / 0.22)
    spread = (_unit(1, seed, 0)[0] - 0.5) * 6.0          # 種で ±3 セント揺れる
    x = np.zeros(n)
    for ratio, amp in ((0.5, 0.8), (1.0, 1.0), (1.5, 0.7), (2.0, 0.6)):
        for cents in (-9.0 + spread, 8.0 + spread):
            f = pitch * ratio * sag * 2.0 ** (cents / 1200.0)
            ph = _phase(f)
            kmax = int(min(30, max(4, 3200.0 / (pitch * ratio))))
            saw = np.zeros(n)
            for k in range(1, kmax + 1):
                saw += np.sin(k * ph) / k
            x += amp * saw
    x /= max(1e-9, float(np.max(np.abs(x))))
    attack = _ramp(t, 0.05)
    decay = 0.55 + 0.45 * np.exp(-t / 0.7)
    release = np.clip((length - t) / max(0.25 * length, 0.05), 0.0, 1.0) ** 1.5
    x = np.tanh(4.0 * x * attack * decay) * release
    x = _stft_filter(x, _lowpass(2600.0, 2))
    return _finish(x, gain, fade_in=0.004, fade_out=0.01)


def suck(length: float = 1.0, pitch: float = 3000.0, gain: float = 0.8, seed: int = 0) -> np.ndarray:
    """逆再生のシンバル風。ふくらみ続けて**終わりで急に止まる**（無音へ吸い込まれる）。
    打点（無音の始まり）に WAV の終わりを合わせて置く。pitch は金属音の基準。"""
    n = _count(length)
    t = _times(n)
    metal = np.zeros(n)
    for ratio, amp in ((1.0, 1.0), (1.4471, 0.9), (1.617, 0.8), (1.9265, 0.7), (2.5028, 0.6), (2.6637, 0.5)):
        metal += amp * np.sign(np.sin(2.0 * np.pi * pitch * 0.5 * ratio * t) + 1e-9)
    hiss = noise(n, seed, 0)
    cymbal = (0.5 * metal / 4.0 + hiss) * np.exp(-t / max(length / 3.2, 0.05))
    cymbal = _stft_filter(cymbal, _highpass(pitch * 0.45, 2))
    return _finish(cymbal[::-1], gain, fade_in=0.01, fade_out=0.002)


# ── 部品表・入口 ──

PARTS: dict[str, Callable[..., np.ndarray]] = {
    "whoosh": whoosh, "riser": riser, "impact": impact, "stamp": stamp,
    "tick": tick, "glitch": glitch, "braam": braam, "suck": suck,
}

# 置く時の基準点。"start"（既定）は WAV の頭が打点。"end" は WAV の終わりが打点（打点から逆算して置く）
ANCHOR: dict[str, str] = {"riser": "end", "suck": "end"}

PITCH_MEANING: dict[str, str] = {
    "whoosh": "山の頂での帯域の中心（Hz）", "riser": "始めの音程（Hz・終わりで8倍）", "impact": "落ち着く低音（Hz）",
    "stamp": "打撃の低音（Hz）", "tick": "クリックの音程（Hz）", "glitch": "断片の基準の音程（Hz）",
    "braam": "和音の根音（Hz）", "suck": "金属音の基準（Hz）",
}


def anchor(kind: str) -> str:
    return ANCHOR.get(kind, "start")


def defaults(kind: str) -> dict:
    """部品の既定の引数 {length, pitch, gain, seed}。"""
    fn = _part(kind)
    return {name: p.default for name, p in inspect.signature(fn).parameters.items()}


def _part(kind: str) -> Callable[..., np.ndarray]:
    fn = PARTS.get(kind)
    if fn is None:
        raise ValueError(f"効果音の部品は {sorted(PARTS)} のどれか: {kind!r}")
    return fn


def normalize_params(kind: str, **params) -> dict:
    """引数を既定で埋め、範囲を確かめる。ValueError は検査の指摘にそのまま使える。"""
    p = {**defaults(kind), **params}
    unknown = set(params) - {"length", "pitch", "gain", "seed"}
    if unknown:
        raise ValueError(f"{kind}: 知らない引数 {sorted(unknown)}（length・pitch・gain・seed）")
    for key in ("length", "pitch", "gain"):
        if isinstance(p[key], bool) or not isinstance(p[key], (int, float)):
            raise ValueError(f"{kind}: {key} は数: {p[key]!r}")
    if isinstance(p["seed"], bool) or not isinstance(p["seed"], int):
        raise ValueError(f"{kind}: seed は整数: {p['seed']!r}")
    if not (MIN_LENGTH <= p["length"] <= MAX_LENGTH):
        raise ValueError(f"{kind}: length は {MIN_LENGTH}〜{MAX_LENGTH} 秒: {p['length']!r}")
    if not (MIN_PITCH <= p["pitch"] <= MAX_PITCH):
        raise ValueError(f"{kind}: pitch は {MIN_PITCH:g}〜{MAX_PITCH:g} Hz: {p['pitch']!r}")
    if not (0.0 < p["gain"] <= 1.0):
        raise ValueError(f"{kind}: gain は 0 より大きく 1 まで: {p['gain']!r}")
    return {"length": float(p["length"]), "pitch": float(p["pitch"]), "gain": float(p["gain"]), "seed": int(p["seed"])}


def synth(kind: str, **params) -> np.ndarray:
    """部品を作る。返り値は float64・モノラル・48kHz・ピークが gain。"""
    p = normalize_params(kind, **params)
    return _part(kind)(**p)


def to_pcm16(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(np.asarray(x, dtype=np.float64) * 32767.0), -32768, 32767).astype("<i2")


def wav_bytes(x: np.ndarray, sr: int = SR) -> bytes:
    """16bit・モノラルの WAV（ヘッダは固定の44バイト＝同じ中身ならバイト一致）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(to_pcm16(x).tobytes())
    return buf.getvalue()


def make_wav(kind: str, **params) -> bytes:
    return wav_bytes(synth(kind, **params))


def write_wav(path: str | Path, kind: str, **params) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(make_wav(kind, **params))
    return p


def describe() -> list[dict]:
    """部品の一覧（API・見本帳用）: 名前・既定の引数・基準点・pitch の意味。"""
    return [{"kind": k, "defaults": defaults(k), "anchor": anchor(k), "pitch": PITCH_MEANING[k]} for k in PARTS]
