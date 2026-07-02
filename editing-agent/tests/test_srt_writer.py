import json
from pathlib import Path

from app.core.srt_writer import _format_srt_time, build_srt, count_subtitles

FIXTURES = Path(__file__).parent / "fixtures"


def _load_tts() -> dict:
    return json.loads((FIXTURES / "tts.json").read_text(encoding="utf-8"))


def test_format_srt_time():
    assert _format_srt_time(0.0) == "00:00:00,000"
    assert _format_srt_time(5.4) == "00:00:05,400"
    assert _format_srt_time(65.123) == "00:01:05,123"
    assert _format_srt_time(3661.0) == "01:01:01,000"


def test_build_srt_basic_format_and_count():
    tts = _load_tts()
    srt_text = build_srt(tts)

    blocks = srt_text.strip().split("\n\n")
    assert len(blocks) == count_subtitles(tts) == 31

    first = blocks[0].split("\n")
    assert first[0] == "1"
    assert "-->" in first[1]
    assert first[1] == "00:00:00,000 --> 00:00:05,400"
    # processed_text の絵文字プレフィックス（😆）は字幕に出さず、textを使う
    assert first[2] == "みんな！ラリー・シルバースタインって名前、覚えてるのだ？"
    assert "😆" not in first[2]


def test_build_srt_ignores_caption():
    # caption はキャラの字幕表示名/TTS VoiceDesignスタイル指示（DATA_SCHEMA.md §2b）であり
    # 字幕本文ではない。非nullでも text を使う（話者名だけの字幕になるバグの再発防止）。
    tts = {
        "audio_files": [
            {"line_id": "line_001", "order": 1, "speaker_name": "ずんだもん", "text": "本文テキスト", "caption": "ずんだもん"},
        ],
        "timeline": [
            {"line_id": "line_001", "start_sec": 0.0, "end_sec": 2.0, "pause_after_sec": 0.0},
        ],
    }
    srt_text = build_srt(tts)
    assert "本文テキスト" in srt_text
    assert "ずんだもん" not in srt_text


def test_build_srt_speaker_prefix():
    tts = {
        "audio_files": [
            {"line_id": "line_001", "order": 1, "speaker_name": "ずんだもん", "text": "こんにちは", "caption": None},
        ],
        "timeline": [
            {"line_id": "line_001", "start_sec": 0.0, "end_sec": 1.0, "pause_after_sec": 0.0},
        ],
    }
    srt_text = build_srt(tts, speaker_prefix=True)
    assert "ずんだもん: こんにちは" in srt_text


def test_build_srt_skips_lines_without_timeline_entry():
    tts = {
        "audio_files": [
            {"line_id": "line_001", "order": 1, "speaker_name": "A", "text": "あり", "caption": None},
            {"line_id": "line_002", "order": 2, "speaker_name": "B", "text": "なし", "caption": None},
        ],
        "timeline": [
            {"line_id": "line_001", "start_sec": 0.0, "end_sec": 1.0, "pause_after_sec": 0.0},
        ],
    }
    srt_text = build_srt(tts)
    assert "あり" in srt_text
    assert "なし" not in srt_text
    assert count_subtitles(tts) == 1
