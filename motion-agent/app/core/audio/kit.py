"""予告編キット: 拍の地図から BGM を手続き合成する（A2・Docs/MOTION_UNIFIED_PLAN.md §9-3）。

曲の構造が拍の格子（timing.audiomap_from_grid）から決まるので、音と絵は構造上ずれない。
旋律の質は Lyria や購入曲に劣る。曲が欲しい回は A3（曲を先に決める）を使う。

構造（§9-3・ビートは地図の energy_phases の名前）:
    ① b1_first   無音から低いドローン（パッドと低音）が立ち上がる
    ② b2_flow    拍の脈（低いタム・キック）が加わる
    ③ b3_montage 16分の刻み・上モノ（アルペジオ）が加わり、音量とフィルターが開いていく
    ④ b4_out     **無音の落とし**（④の頭で全てを止める）。効果音の suck がここへ吸い込まれ、riser が⑤へ立ち上がる
    ⑤ b5_title   低い一撃（impact）＋ braam＋余韻
効果音（`mix.CUE_PARTS`）とは別の層: ここは音楽だけを作り、語ごとの whoosh・カットの打撃などは効果音の側が受け持つ。

3つの雰囲気（MOODS）: dread（不穏）・drive（疾走）・hush（静かな緊張）。
乱数は sfx_synth と同じ整数のハッシュ。同じ（地図・雰囲気・種・fps）なら WAV がバイト一致する（M2）。
打点は fps のフレームに丸める（効果音と同じフレームで鳴り、半フレーム未満のずれで揺れない）。
"""
from __future__ import annotations

import numpy as np

from app.core.audio import sfx_synth as S

SR = S.SR
_STEPS_PER_BAR = 16

# 声ごとの音量（足し合わせる前）。最後に全体を一律に収める
_LEVEL = {"kick": 0.9, "tom": 0.75, "clap": 0.45, "hat": 0.22, "ohat": 0.2, "arp": 0.3, "bell": 0.28}

# 雰囲気。patterns[ビート名][声] は16分16個の文字列（'.'＝休み・1〜9＝強さ）。ビートの頭から1小節ずつ繰り返す
MOODS: dict[str, dict] = {
    "dread": {
        "label": "不穏（陰謀論・都市伝説）", "root": 55.0, "chord": [0, 7, 12, 13],       # A1: 根音・5度・8度・短9度
        "bright_hz": 900.0, "sweep": (220.0, 1800.0), "pad": 0.6, "sub": 0.5, "duck": 0.0,
        "kick": 46.0, "tom": 74.0,
        "patterns": {
            "b2_flow": {"tom": "9.......6......."},
            "b3_montage": {"tom": "9...5...7...5.5.", "hat": "....3.......3.3."},
        },
        "bell": {"semis": [0, 1, 6, 13], "octave": 4, "bars": 2, "beats": ["b2_flow", "b3_montage"]},
        "arp": None,
        "final": {"impact": 44.0, "braam": 55.0, "braam_gain": 0.7},
    },
    "drive": {
        "label": "疾走（ドライブ感）", "root": 65.41, "chord": [0, 7, 12, 15],            # C2: パワーコード＋短3度
        "bright_hz": 2600.0, "sweep": (500.0, 5200.0), "pad": 0.4, "sub": 0.55, "duck": 0.55,
        "kick": 52.0, "tom": 90.0,
        "patterns": {
            "b2_flow": {"kick": "9...9...9...9...", "clap": "....7.......7...", "ohat": "..5...5...5...5."},
            "b3_montage": {"kick": "9...9...9...9...", "clap": "....8.......8.5.", "hat": "5363536353635363",
                           "ohat": "..6...6...6...6."},
        },
        "bell": None,
        "arp": {"scale": [0, 3, 5, 7, 10, 12, 10, 7], "octave": 3, "pattern": "7.5.6.5.7.5.6.5.", "beats": ["b3_montage"]},
        "final": {"impact": 50.0, "braam": 65.41, "braam_gain": 0.6},
    },
    "hush": {
        "label": "静かな緊張", "root": 73.42, "chord": [0, 3, 7, 10],                    # D2: 短7の和音
        "bright_hz": 700.0, "sweep": (260.0, 1500.0), "pad": 0.55, "sub": 0.45, "duck": 0.0,
        "kick": 50.0, "tom": 70.0,
        "patterns": {
            "b2_flow": {"tom": "7.....4........."},                                  # 心拍（ドクン…ドッ）
            "b3_montage": {"tom": "8.....5.7.....4.", "hat": "..2...2...2...2."},
        },
        "bell": {"semis": [0, 7, 12, 15], "octave": 5, "bars": 4, "beats": ["b2_flow", "b3_montage"]},
        "arp": None,
        "final": {"impact": 48.0, "braam": 73.42, "braam_gain": 0.45},
    },
}


class KitError(ValueError):
    pass


def moods() -> list[dict]:
    return [{"mood": k, "label": v["label"], "root_hz": v["root"]} for k, v in MOODS.items()]


# ── 声（楽器）。どれも整数のハッシュの雑音と正弦波だけ ──

def _kick(freq: float, seed: int) -> np.ndarray:
    n = S._count(0.34)
    t = S._times(n)
    body = np.sin(S._phase(freq * (1.0 + 3.2 * np.exp(-t / 0.035)))) * np.exp(-t / 0.11)
    click = S._stft_filter(S.noise(n, seed, 0), S._lowpass(3500.0, 2)) * np.exp(-t / 0.004)
    click = click / max(1e-9, float(np.max(np.abs(click)))) * 0.3
    return np.tanh(1.4 * (body + click))


def _tom(freq: float, seed: int) -> np.ndarray:
    n = S._count(0.3)
    t = S._times(n)
    body = np.sin(S._phase(freq * (1.0 + 0.9 * np.exp(-t / 0.05)))) * np.exp(-t / 0.1)
    knock = S._stft_filter(S.noise(n, seed, 1), S._band(lambda tc: np.full_like(tc, 700.0), 0.9)) * np.exp(-t / 0.012)
    knock = knock / max(1e-9, float(np.max(np.abs(knock)))) * 0.25
    return body + knock


def _hat(open_: bool, seed: int) -> np.ndarray:
    n = S._count(0.22 if open_ else 0.05)
    t = S._times(n)
    x = S._stft_filter(S.noise(n, seed, 2), S._highpass(6500.0, 2)) * np.exp(-t / (0.07 if open_ else 0.012))
    return x / max(1e-9, float(np.max(np.abs(x))))


def _clap(seed: int) -> np.ndarray:
    n = S._count(0.2)
    t = S._times(n)
    band = S._stft_filter(S.noise(n, seed, 3), S._band(lambda tc: np.full_like(tc, 1500.0), 0.8))
    env = np.zeros(n)
    for off in (0.0, 0.009, 0.018):
        tt = np.clip(t - off, 0.0, None)
        env += np.where(t >= off, np.exp(-tt / 0.006), 0.0)
    env += 0.5 * np.exp(-np.clip(t - 0.024, 0.0, None) / 0.06) * (t >= 0.024)
    x = band * env + 0.25 * np.sin(2.0 * np.pi * 190.0 * t) * np.exp(-t / 0.04)
    return x / max(1e-9, float(np.max(np.abs(x))))


def _pluck(freq: float, seed: int) -> np.ndarray:
    n = S._count(0.2)
    t = S._times(n)
    x = np.zeros(n)
    for k in range(1, 11):
        if k * freq > 12000.0:
            break
        x += np.sin(2.0 * np.pi * k * freq * t) / k * np.exp(-t / (0.13 / np.sqrt(k)))
    return x / max(1e-9, float(np.max(np.abs(x))))


def _bell(freq: float, seed: int) -> np.ndarray:
    n = S._count(1.6)
    t = S._times(n)
    x = np.zeros(n)
    for ratio, amp, tau in ((1.0, 1.0, 1.1), (2.76, 0.5, 0.7), (5.4, 0.25, 0.45), (8.93, 0.12, 0.25)):
        x += amp * np.sin(2.0 * np.pi * freq * ratio * t) * np.exp(-t / tau)
    x *= S._ramp(t, 0.003)
    return x / max(1e-9, float(np.max(np.abs(x))))


# ── 組み立て ──

def _add(out: np.ndarray, x: np.ndarray, t_sec: float, gain: float = 1.0) -> None:
    i0 = int(round(t_sec * SR))
    a, b = max(0, i0), min(len(out), i0 + len(x))
    if b > a:
        out[a:b] += gain * x[a - i0:b - i0]


def _curve(points: list[tuple[float, float]], n: int) -> np.ndarray:
    xs, ys = zip(*points)
    return np.interp(np.arange(n) / SR, xs, ys)


def _pad(spec: dict, n: int, t_end: float, curve_pts: list, seed: int) -> np.ndarray:
    """持続の和音（鋸歯を少しずらして重ねる）＋低音の正弦波。フィルターが時間とともに開く。"""
    t = np.arange(n) / SR
    ph = S._unit(32, seed, 40)
    x = np.zeros(n)
    i = 0
    for semis in spec["chord"]:
        f = spec["root"] * 2.0 ** (semis / 12.0)
        for cents in (-7.0, 6.0):
            ff = f * 2.0 ** (cents / 1200.0)
            kmax = int(min(10, max(2, spec["bright_hz"] / ff)))
            for k in range(1, kmax + 1):
                x += np.sin(2.0 * np.pi * k * ff * t + 2.0 * np.pi * float(ph[i % 32])) / k
            i += 1
    x /= max(1e-9, float(np.max(np.abs(x))))
    lo, hi = spec["sweep"]
    x = S._stft_filter(x, _sweep_lowpass(lo, hi, t_end))
    sub = np.sin(2.0 * np.pi * spec["root"] * 0.5 * t) + 0.5 * np.sin(2.0 * np.pi * spec["root"] * t)
    sub /= max(1e-9, float(np.max(np.abs(sub))))
    return (spec["pad"] * x + spec["sub"] * sub) * _curve(curve_pts, n)


def _sweep_lowpass(lo: float, hi: float, t_end: float):
    """lo→hi へ指数的に開くローパス（t_end 秒で hi）。"""
    def resp(tc: np.ndarray, f: np.ndarray) -> np.ndarray:
        fc = lo * (hi / lo) ** np.clip(tc / max(t_end, 1e-6), 0.0, 1.0)
        return 1.0 / np.sqrt(1.0 + (f[None, :] / fc[:, None]) ** 4)
    return resp


def render(audiomap: dict, mood: str, fps: int = 30, seed: int = 0) -> tuple[np.ndarray, dict]:
    """拍の地図 → BGM（モノラル・48kHz・ピーク 0.9）。返り値: (波形, 報告)。"""
    spec = MOODS.get(mood)
    if spec is None:
        raise KitError(f"雰囲気は {sorted(MOODS)} のどれか: {mood!r}")
    phases = {p["name"]: p for p in audiomap.get("energy_phases", []) if "name" in p}
    if "b1_first" not in phases:
        raise KitError("拍の地図に b1_first が無い（予告編キットは①〜⑤の構造を前提にする）")
    total = float(audiomap["audio"]["duration_sec"])
    n = max(1, int(round(total * SR)))
    bpm = float(audiomap["tempo"]["bpm"])
    beat = 60.0 / bpm
    step = beat / 4.0
    snap = (lambda x: round(x * fps) / fps) if fps else (lambda x: x)

    b1 = phases["b1_first"]
    b2, b3, b4, b5 = (phases.get(k) for k in ("b2_flow", "b3_montage", "b4_out", "b5_title"))
    stop = b4["start"] if b4 else (b5["start"] if b5 else total)       # ここで音楽を止める（無音の落とし）
    music_end = stop

    out = np.zeros(n)
    counts: dict[str, int] = {}

    # ── パッドと低音（①〜③の通し）: 音量の曲線は ①で立ち上がり ②で保ち ③で開く ──
    n_pad = int(round(music_end * SR))
    pts = [(0.0, 0.0), (min(2 * beat, b1["end"]), 0.45), (b1["end"], 0.5)]
    if b2:
        pts.append((b2["end"], 0.6))
    if b3:
        pts += [(b3["start"], 0.65), (b3["end"] - 0.03, 1.0)]          # ③の終わりまで上がり続け、④の頭で落とす
    pts.append((music_end, 0.0))
    pts.sort(key=lambda p: p[0])
    fixed: list[tuple[float, float]] = []
    for x_, y_ in pts:                                                 # 時刻は単調増加に（np.interp の前提）
        if fixed and x_ <= fixed[-1][0]:
            x_ = fixed[-1][0] + 1e-4
        fixed.append((x_, y_))
    pad = _pad(spec, n_pad, b3["end"] if b3 else music_end, fixed, seed)

    # ── 打楽器・上モノ: 16分の格子の上に置く ──
    cache: dict[tuple, np.ndarray] = {}

    def voice(name: str, key: tuple, make) -> np.ndarray:
        k = (name, *key)
        if k not in cache:
            cache[k] = make()
        return cache[k]

    kick_times: list[float] = []
    n_steps = int(music_end / step + 1e-9)
    for s in range(n_steps):
        t = s * step
        name = next((nm for nm in ("b1_first", "b2_flow", "b3_montage") if phases.get(nm)
                     and phases[nm]["start"] - 1e-9 <= t < phases[nm]["end"] - 1e-9), None)
        if name is None:
            continue
        pat = spec["patterns"].get(name, {})
        pos = s % _STEPS_PER_BAR
        tt = snap(t)
        for vname, string in pat.items():
            ch = string[pos]
            if ch == ".":
                continue
            vel = int(ch) / 9.0
            sd = seed * 7919 + s
            if vname == "kick":
                _add(out, voice("kick", (spec["kick"],), lambda: _kick(spec["kick"], seed)), tt, _LEVEL["kick"] * vel)
                kick_times.append(tt)
            elif vname == "tom":
                _add(out, voice("tom", (spec["tom"],), lambda: _tom(spec["tom"], seed)), tt, _LEVEL["tom"] * vel)
            elif vname == "clap":
                _add(out, voice("clap", (), lambda: _clap(seed)), tt, _LEVEL["clap"] * vel)
            elif vname in ("hat", "ohat"):
                o = vname == "ohat"
                v = voice("hat", (o, s % 3), lambda: _hat(o, seed * 31 + (s % 3)))   # 3通りを使い回す（連打を機械的にしない）
                _add(out, v, tt, _LEVEL[vname] * vel)
            else:
                continue
            counts[vname] = counts.get(vname, 0) + 1
        # アルペジオ（上モノ）
        arp = spec.get("arp")
        if arp and name in arp["beats"] and arp["pattern"][pos] != ".":
            sc = arp["scale"]
            semis = sc[(s // 1) % len(sc)]
            f = spec["root"] * (2.0 ** arp["octave"]) * 2.0 ** (semis / 12.0)
            v = voice("arp", (round(f, 2),), lambda: _pluck(f, seed))
            _add(out, v, tt, _LEVEL["arp"] * int(arp["pattern"][pos]) / 9.0)
            counts["arp"] = counts.get("arp", 0) + 1
        # 鐘（小節の頭・数小節おき）
        bell = spec.get("bell")
        if bell and name in bell["beats"] and pos == 0 and (s // _STEPS_PER_BAR) % bell["bars"] == 0:
            k = (s // (_STEPS_PER_BAR * bell["bars"])) % len(bell["semis"])
            f = spec["root"] * (2.0 ** bell["octave"]) * 2.0 ** (bell["semis"][k] / 12.0)
            v = voice("bell", (round(f, 2),), lambda: _bell(f, seed))
            _add(out, v, tt, _LEVEL["bell"])
            counts["bell"] = counts.get("bell", 0) + 1

    # ── サイドチェーン風: キックの瞬間にパッドを沈める（drive）──
    if spec["duck"] > 0 and kick_times:
        duck = np.ones(n_pad)
        for kt in kick_times:
            i0 = int(round(kt * SR))
            m = min(n_pad - i0, int(0.18 * SR))
            if m > 0:
                duck[i0:i0 + m] *= 1.0 - spec["duck"] * np.exp(-np.arange(m) / SR / 0.07)
        pad = pad * duck
    out[:n_pad] += pad

    # ── ④の頭で全てを止める（無音の落とし）──
    i_stop = int(round(snap(music_end) * SR))
    if i_stop < n:
        fade = min(int(0.02 * SR), i_stop)
        if fade > 0:
            out[i_stop - fade:i_stop] *= np.linspace(1.0, 0.0, fade)
        out[i_stop:] = 0.0

    # ── ⑤: 低い一撃＋ braam ＋余韻 ──
    if b5 and b5["start"] < total:
        t5 = snap(b5["start"])
        fin = spec["final"]
        dur = max(0.5, min(total - t5, 4.0))
        imp = S.synth("impact", length=min(dur, 1.6), pitch=fin["impact"], gain=0.95, seed=seed + 5)
        _add(out, imp, t5, 0.95)
        br = S.synth("braam", length=dur, pitch=fin["braam"], gain=0.8, seed=seed + 6)
        _add(out, br, t5, fin["braam_gain"])
        # 余韻: 低い雑音の尾（疑似の残響）
        tail_n = int(round(min(total - t5, 3.0) * SR))
        if tail_n > 0:
            tt_ = np.arange(tail_n) / SR
            tail = S._stft_filter(S.noise(tail_n, seed, 50), S._lowpass(700.0, 2)) * np.exp(-tt_ / 0.9)
            tail = tail / max(1e-9, float(np.max(np.abs(tail)))) * 0.25
            _add(out, tail, t5 + 0.05, 1.0)
        counts["final"] = 1

    # ── 仕上げ: 柔らかく丸めて、ピークを 0.9 に ──
    out = np.tanh(1.15 * out)
    peak = float(np.max(np.abs(out)))
    if peak > 0:
        out *= 0.9 / peak
    out[:1] = 0.0
    report = {"mood": mood, "label": spec["label"], "bpm": bpm, "duration_sec": round(total, 3),
              "stop_sec": round(snap(music_end), 4), "voices": counts, "seed": int(seed), "fps": fps}
    return out, report


def demo_audiomap(bpm: float = 120.0, fps: int = 30) -> dict:
    """試聴用: 型 v1 の既定の長さ（①〜⑤）を拍に吸着した、そのままの地図。"""
    from app.core import timing
    from app.core.templates import get_meta
    meta = get_meta("kinetic_teaser", 1)
    cfg = meta["beats"]["b3_montage"]["cuts"]
    bars = lambda b: timing.default_bars(meta, b, bpm)  # noqa: E731
    shots = [
        {"id": "s1", "beat": "b1_first", "gimmick": "K1_first", "bars": bars("b1_first"), "enter": None},
        {"id": "s2", "beat": "b2_flow", "gimmick": "K2_flow", "bars": bars("b2_flow"), "enter": None, "n_words": 5},
        {"id": "s3", "beat": "b3_montage", "gimmick": "K3_montage", "bars": bars("b3_montage"), "enter": None,
         "n_materials": 6, "cut_cfg": cfg},
        {"id": "s4", "beat": "b4_out", "gimmick": "K4_out", "bars": bars("b4_out"), "enter": None},
        {"id": "s5", "beat": "b5_title", "gimmick": "K5_title", "bars": bars("b5_title"), "enter": None},
    ]
    meta = dict(meta, fps=fps)
    return timing.plan_timeline(meta, bpm, shots)["audiomap"]
