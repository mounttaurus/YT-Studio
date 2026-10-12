"""効果音の手続き合成（app/core/audio/sfx_synth.py・MOTION_UNIFIED_PLAN §9-2・M2）。
長さ・ピーク・WAV のバイト一致（同じ種）・種が違えば違う。"""
import io
import wave

import numpy as np
import pytest

from app.core.audio import sfx_synth as S

KINDS = sorted(S.PARTS)


def _read(data: bytes):
    w = wave.open(io.BytesIO(data))
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return w, pcm


def test_the_parts_exist():
    # 計画 §9-2 の8部品＋タイピング（type）・文字が現れる音（pop）
    assert KINDS == sorted(["whoosh", "riser", "impact", "stamp", "tick", "glitch", "braam", "suck", "type", "pop"])


# ── 長さ ──

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("length", [0.05, 0.37, 1.0])
def test_length_is_exact(kind, length):
    x = S.synth(kind, length=length)
    assert len(x) == round(length * S.SR)
    w, pcm = _read(S.make_wav(kind, length=length))
    assert w.getframerate() == 48000 and w.getnchannels() == 1 and w.getsampwidth() == 2
    assert w.getnframes() == len(pcm) == round(length * S.SR)


# ── ピーク ──

@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("gain", [0.2, 0.5, 0.9, 1.0])
def test_peak_is_gain_and_never_clips(kind, gain):
    _, pcm = _read(S.make_wav(kind, gain=gain))
    peak = int(np.max(np.abs(pcm.astype(np.int32))))
    assert abs(peak - round(gain * 32767)) <= 1
    assert peak <= 32767
    assert np.all(np.isfinite(S.synth(kind, gain=gain)))


@pytest.mark.parametrize("kind", KINDS)
def test_starts_and_ends_at_silence(kind):
    """頭と終わりで値が飛ばない（プチッという音を出さない）。"""
    x = S.synth(kind)
    assert abs(x[0]) < 0.02 and abs(x[-1]) < 0.02


# ── バイト一致（M2） ──

@pytest.mark.parametrize("kind", KINDS)
def test_same_arguments_give_identical_bytes(kind):
    a = S.make_wav(kind, length=0.4, pitch=500.0, gain=0.7, seed=5)
    b = S.make_wav(kind, length=0.4, pitch=500.0, gain=0.7, seed=5)
    assert a == b
    assert S.make_wav(kind) == S.make_wav(kind)


@pytest.mark.parametrize("kind", KINDS)
def test_different_seed_gives_different_sound(kind):
    a = S.make_wav(kind, length=0.5, seed=1)
    b = S.make_wav(kind, length=0.5, seed=2)
    assert a != b


@pytest.mark.parametrize("kind", KINDS)
def test_other_arguments_change_the_sound(kind):
    base = S.make_wav(kind, length=0.5, pitch=300.0, gain=0.6, seed=3)
    assert S.make_wav(kind, length=0.5, pitch=450.0, gain=0.6, seed=3) != base
    assert S.make_wav(kind, length=0.5, pitch=300.0, gain=0.3, seed=3) != base


def test_parts_are_distinct_from_each_other():
    wavs = {k: S.make_wav(k, length=0.5) for k in KINDS}
    assert len(set(wavs.values())) == len(KINDS)


def test_write_wav_matches_make_wav(tmp_path):
    p = S.write_wav(tmp_path / "sub" / "a.wav", "impact", length=0.3, seed=9)
    assert p.read_bytes() == S.make_wav("impact", length=0.3, seed=9)


def test_noise_is_integer_hashed_and_stable():
    """雑音の並びは numpy の乱数の版に依らない（整数のハッシュ）。値を固定して番人にする。"""
    assert S.noise(4, 1, 0).tolist() == pytest.approx(
        [0.06833164210493714, -0.5732450908080207, -0.5387784933073057, 0.8373522324867033], abs=1e-12)
    a = S.noise(20000, 0, 0)
    assert -1.0 <= a.min() and a.max() < 1.0 and abs(a.mean()) < 0.02
    # seed・stream を変えても、ずらしただけの同じ並びにならない
    for other in (S.noise(20000, 1, 0), S.noise(20000, 0, 1)):
        assert max(abs(np.corrcoef(a[:-10], np.roll(other, k)[:-10])[0, 1]) for k in range(-5, 6)) < 0.05
    assert S.noise(0 + 3, -1, 0).shape == (3,)   # 負の種も通る


# ── 部品ごとの性質 ──

def test_riser_peaks_exactly_at_the_end():
    """打点から逆算した長さで、ちょうど終わりで頂点（音量も音程も最大）。"""
    for length in (0.4, 1.0, 2.0):
        x = S.synth("riser", length=length)
        n = len(x)
        assert int(np.argmax(np.abs(x))) >= n - int(0.01 * S.SR)
        w = n // 10
        rms = [float(np.sqrt(np.mean(x[i * w:(i + 1) * w] ** 2))) for i in range(10)]
        assert all(b >= a * 0.999 for a, b in zip(rms, rms[1:])), rms   # 上がり続ける
        assert rms[-1] > 4 * rms[0]


def test_riser_pitch_rises():
    """終わり際の方が高域が多い（スペクトルの重心が上がる）。"""
    x = S.synth("riser", length=1.5)
    n = len(x)

    def centroid(seg):
        m = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        f = np.fft.rfftfreq(len(seg), 1 / S.SR)
        return float((m * f).sum() / m.sum())

    q = n // 4
    assert centroid(x[3 * q:4 * q]) > 1.5 * centroid(x[:q])


def test_suck_swells_then_stops_at_the_end():
    x = S.synth("suck", length=1.0)
    n = len(x)
    head, tail = x[:n // 10], x[-n // 10:]
    assert np.sqrt(np.mean(tail ** 2)) > 3 * np.sqrt(np.mean(head ** 2))
    assert int(np.argmax(np.abs(x))) > int(0.9 * n)


def test_impact_hits_first_and_decays():
    x = S.synth("impact", length=1.4)
    n = len(x)
    assert int(np.argmax(np.abs(x))) < int(0.05 * S.SR)
    q = n // 4
    r1 = float(np.sqrt(np.mean(x[:q] ** 2)))
    r4 = float(np.sqrt(np.mean(x[3 * q:] ** 2)))
    assert r4 < 0.1 * r1


def test_whoosh_is_a_hill():
    """山なり: 頭と終わりが静かで、頂は途中（3〜6割のあたり）。"""
    x = S.synth("whoosh", length=0.8)
    n = len(x)
    assert 0.25 * n < int(np.argmax(np.abs(x))) < 0.65 * n
    w = n // 8
    rms = [float(np.sqrt(np.mean(x[i * w:(i + 1) * w] ** 2))) for i in range(8)]
    assert rms[0] < 0.5 * max(rms) and rms[-1] < 0.3 * max(rms)


def test_tick_is_short_and_front_loaded():
    x = S.synth("tick")
    assert len(x) == round(0.05 * S.SR)
    h = len(x) // 2
    assert np.sum(x[:h] ** 2) > 3 * np.sum(x[h:] ** 2)


def test_stamp_is_a_low_thump():
    x = S.synth("stamp", length=0.4)
    m = np.abs(np.fft.rfft(x))
    f = np.fft.rfftfreq(len(x), 1 / S.SR)
    assert f[int(np.argmax(m))] < 400


def test_type_is_a_short_key_strike_that_varies_by_seed():
    x = S.synth("type", seed=3)
    assert len(x) == round(0.07 * S.SR)
    h = len(x) // 3
    assert np.sum(x[:h] ** 2) > 4 * np.sum(x[h:] ** 2)            # 頭に集まる
    # 低い pitch の方が低音寄り（スペクトルの重心が低い）
    def centroid(v):
        m = np.abs(np.fft.rfft(v * np.hanning(len(v))))
        f = np.fft.rfftfreq(len(v), 1 / S.SR)
        return float((m * f).sum() / m.sum())
    assert centroid(S.synth("type", pitch=160.0, seed=3)) < centroid(S.synth("type", pitch=400.0, seed=3))
    # 打ごとに揺れる（種が違えば波形が違い、同じ打の連続にならない）
    a, b = S.synth("type", seed=1), S.synth("type", seed=2)
    assert float(np.max(np.abs(a - b))) > 0.05 * float(np.max(np.abs(a)))


def test_pop_is_round_and_front_loaded():
    x = S.synth("pop")
    assert len(x) == round(0.14 * S.SR)
    h = int(0.06 * S.SR)
    assert np.sum(x[:h] ** 2) > 4 * np.sum(x[h:] ** 2)
    m = np.abs(np.fft.rfft(x))
    f = np.fft.rfftfreq(len(x), 1 / S.SR)
    assert 400 < f[int(np.argmax(m))] < 2000


def test_glitch_has_gaps():
    x = S.synth("glitch", length=1.0, seed=4)
    quiet = np.abs(x) < 0.02 * np.max(np.abs(x))   # 隙間は（直流を除いた分の）ごく小さな値
    runs = 0
    cur = 0
    for q in quiet:
        cur = cur + 1 if q else 0
        runs = max(runs, cur)
    assert runs > int(0.004 * S.SR)   # 4ms 以上の無音の隙間がある


# ── 引数の検査 ──

def test_defaults_and_describe():
    d = S.defaults("whoosh")
    assert set(d) == {"length", "pitch", "gain", "seed"}
    info = {p["kind"]: p for p in S.describe()}
    assert set(info) == set(KINDS)
    assert info["riser"]["anchor"] == "end" and info["suck"]["anchor"] == "end" and info["whoosh"]["anchor"] == "start"


@pytest.mark.parametrize("kind,params", [
    ("nope", {}),
    ("tick", {"length": 0}),
    ("tick", {"length": 60}),
    ("tick", {"pitch": 5}),
    ("tick", {"pitch": 99999}),
    ("tick", {"gain": 0}),
    ("tick", {"gain": 1.5}),
    ("tick", {"seed": 1.5}),
    ("tick", {"seed": True}),
    ("tick", {"length": "0.1"}),
    ("tick", {"volume": 1}),
])
def test_bad_arguments_are_value_errors(kind, params):
    with pytest.raises(ValueError):
        S.synth(kind, **params)
