"""行の音声が「今の台本に対して最新か」の判定（Docs/LINE_WORKBENCH_PLAN.md §4-3・W2）。

director の TTS タブが JS で持っている `lineTtsState` と**同じ規則**を、行の確定が
「要再生成・未生成の行だけを作り直す」ためにサーバー側へ置いたもの（生成側の tts-agent が
声・字幕の解決を持っているので、判定もここが本籍）。

- missing … tts.json にエントリが無い、または wav ファイルが無い
- stale   … 生成後に 本文 / 感情 / 速度 / 声 / 字幕 のどれかが変わった
- current … 最新（作り直し不要）

emotion / speed は古い tts.json に無い項目なので、記録が無い（None）エントリではその項目を
比較しない（一律「要再生成」にしない・JS 側と同じ後方互換）。
純粋関数（ファイルI/Oなし）。
"""
from __future__ import annotations

CURRENT, STALE, MISSING = "current", "stale", "missing"


def audio_state(entry: dict | None, wav_exists: bool, *, text: str, emotion: str | None,
                speed: float | None, voice: str, caption: str | None) -> str:
    if not entry or not wav_exists:
        return MISSING
    same_text = entry.get("text") == text
    e_emotion = entry.get("emotion")
    same_emotion = e_emotion is None or e_emotion == (emotion or "neutral")
    e_speed = entry.get("speed")
    same_speed = e_speed is None or float(e_speed) == float(speed if speed is not None else 1.0)
    same_voice = (entry.get("voice_id") or "none") == (voice or "none")
    same_caption = (entry.get("caption") or None) == (caption or None)
    return CURRENT if (same_text and same_emotion and same_speed and same_voice and same_caption) else STALE
