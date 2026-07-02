"""
tts.json から DaVinci Resolve 用の SRT字幕ファイルを生成する。
Docs/06_editing.md セクション5-3参照。

- audio_files[] をorder順に、timeline[]のstart_sec/end_secで配置
- テキストは text（processed_textは使わない＝絵文字プレフィックスはTTS用であり字幕に出さない）。
  caption は字幕本文には使わない（DATA_SCHEMA.md §2b: caption=キャラの字幕表示名/TTS VoiceDesign
  スタイル指示であり、行ごとの字幕テキストではない。誤用すると話者名だけの字幕になる）。
- speaker_prefix=Trueなら "speaker_name: text" 形式
"""


def _format_srt_time(sec: float) -> str:
    total_ms = round(sec * 1000)
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    seconds, ms = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def subtitle_lines(tts: dict) -> list[dict]:
    """字幕1行を表すdictのリストをorder順に返す（SRT/FCPXML共通の本籍）。

    テキストは text（processed_textは使わない＝絵文字プレフィックスはTTS用であり字幕に出さない）。
    caption はキャラの字幕表示名/TTS VoiceDesignスタイル指示であり字幕本文ではないため使わない。
    timeline[] に無い行は除外。
    各dict: index / line_id / speaker_id / speaker_name / text / start_sec / end_sec / emotion_emoji
    """
    timeline_by_line = {e["line_id"]: e for e in tts.get("timeline", [])}
    audio_files = sorted(tts.get("audio_files", []), key=lambda a: a.get("order", 0))

    lines = []
    index = 1
    for audio in audio_files:
        entry = timeline_by_line.get(audio["line_id"])
        if entry is None:
            continue
        lines.append({
            "index": index,
            "line_id": audio["line_id"],
            "speaker_id": audio.get("speaker_id", ""),
            "speaker_name": audio.get("speaker_name", ""),
            "text": audio.get("text", ""),
            "start_sec": entry["start_sec"],
            "end_sec": entry["end_sec"],
            "emotion_emoji": audio.get("emotion_emoji", ""),
        })
        index += 1
    return lines


def build_srt(tts: dict, speaker_prefix: bool = False) -> str:
    blocks = []
    for line in subtitle_lines(tts):
        text = line["text"]
        if speaker_prefix:
            text = f"{line['speaker_name']}: {text}"
        start = _format_srt_time(line["start_sec"])
        end = _format_srt_time(line["end_sec"])
        blocks.append(f"{line['index']}\n{start} --> {end}\n{text}\n")

    return "\n".join(blocks) + "\n" if blocks else ""


def count_subtitles(tts: dict) -> int:
    timeline_by_line = {e["line_id"] for e in tts.get("timeline", [])}
    return sum(1 for a in tts.get("audio_files", []) if a["line_id"] in timeline_by_line)
