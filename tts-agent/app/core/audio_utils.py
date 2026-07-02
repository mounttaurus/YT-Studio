"""TTS音声(WAV)のチャンネル変換・再生時間計算ユーティリティ。

Irodori-TTS-Serverはモノラル(1ch)のWAVを生成するが、DaVinci Resolve等の
編集ソフトでタイムラインに配置した際にL(左)チャンネルのみで再生されるため、
生成直後にL/R同一データのステレオ(デュアルモノ)へ変換する。
"""
import io
import wave


def to_stereo_wav_bytes(audio_bytes: bytes) -> bytes:
    """WAVバイト列がモノラルならステレオ(デュアルモノ)に変換して返す。

    すでにステレオ、またはWAVとして読めない場合はそのまま返す。
    """
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as r:
            if r.getnchannels() != 1:
                return audio_bytes
            sampwidth = r.getsampwidth()
            framerate = r.getframerate()
            frames = r.readframes(r.getnframes())
    except (wave.Error, EOFError):
        return audio_bytes

    stereo_frames = bytearray()
    for i in range(0, len(frames), sampwidth):
        sample = frames[i:i + sampwidth]
        stereo_frames += sample + sample

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(bytes(stereo_frames))
    return buf.getvalue()


def wav_duration_sec(audio_bytes: bytes) -> float:
    """WAVバイト列から実際の再生時間（秒）を計算する。"""
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as r:
            return r.getnframes() / r.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError):
        return 0.0


def wav_duration_sec_from_file(path) -> float:
    """WAVファイルから実際の再生時間（秒）を計算する。"""
    try:
        with wave.open(str(path), "rb") as r:
            return r.getnframes() / r.getframerate()
    except (wave.Error, EOFError, OSError, ZeroDivisionError):
        return 0.0
