import json
import xml.etree.ElementTree as ET
from pathlib import Path

from app.core.fcpxml_subtitle_writer import build_fcpxml, count_titles

FIXTURES = Path(__file__).parent / "fixtures"


def _load_tts() -> dict:
    return json.loads((FIXTURES / "tts.json").read_text(encoding="utf-8"))


def _parse(xml_text: str) -> ET.Element:
    # DOCTYPE行を除いてからパース（ElementTreeはDOCTYPEを扱わない）
    body = xml_text.split("<fcpxml", 1)
    return ET.fromstring("<fcpxml" + body[1])


def test_wellformed_and_title_count():
    tts = _load_tts()
    xml_text = build_fcpxml(tts)
    assert xml_text.startswith('<?xml version="1.0"')
    assert "<!DOCTYPE fcpxml>" in xml_text

    root = _parse(xml_text)
    titles = root.findall(".//title")
    assert len(titles) == count_titles(tts) == 31


def test_effect_is_basic_title():
    tts = _load_tts()
    root = _parse(build_fcpxml(tts))
    effect = root.find(".//resources/effect")
    assert effect.get("name") == "Basic Title"
    assert effect.get("uid").endswith("Basic Title.moti")
    # 全titleがこのeffectを参照
    for title in root.findall(".//title"):
        assert title.get("ref") == effect.get("id")


def test_timing_matches_tts_timeline():
    tts = _load_tts()
    root = _parse(build_fcpxml(tts, fps=30))
    first = root.findall(".//title")[0]
    # line_001: start 0.0s, end 5.4s -> offset 0, duration 162frame=16200/3000
    assert first.get("offset") == "0/3000s"
    assert first.get("duration") == "16200/3000s"
    second = root.findall(".//title")[1]
    # line_002: start 5.8s -> 174frame -> 17400/3000
    assert second.get("offset") == "17400/3000s"


def test_per_speaker_color_override():
    tts = {
        "audio_files": [
            {"line_id": "l1", "order": 1, "speaker_id": "speaker_a", "text": "あ", "caption": None},
            {"line_id": "l2", "order": 2, "speaker_id": "speaker_b", "text": "い", "caption": None},
        ],
        "timeline": [
            {"line_id": "l1", "start_sec": 0.0, "end_sec": 1.0},
            {"line_id": "l2", "start_sec": 1.0, "end_sec": 2.0},
        ],
    }
    style = {
        "default": {"color": [1, 1, 1, 1]},
        "per_speaker": {
            "speaker_a": {"color": [1, 0.9, 0.2, 1]},
            "speaker_b": {"color": [0, 1, 1, 1]},
        },
    }
    root = _parse(build_fcpxml(tts, style=style))
    styles = root.findall(".//text-style-def/text-style")
    assert styles[0].get("fontColor") == "1 0.9 0.2 1"
    assert styles[1].get("fontColor") == "0 1 1 1"


def test_position_y_to_adjust_transform_scale():
    # Transform Y = adjustY * (height/100) -> adjustY = position_y / 10.8
    tts = {
        "audio_files": [{"line_id": "l1", "order": 1, "speaker_id": "a", "text": "x", "caption": None}],
        "timeline": [{"line_id": "l1", "start_sec": 0.0, "end_sec": 1.0}],
    }
    root = _parse(build_fcpxml(tts, style={"position_y": -250}, height=1080))
    at = root.find(".//title/adjust-transform")
    # -250 / 10.8 = -23.1481...
    assert at.get("position").startswith("0 -23.1")


def test_stroke_omitted_when_zero_width():
    tts = {
        "audio_files": [{"line_id": "l1", "order": 1, "speaker_id": "a", "text": "x", "caption": None}],
        "timeline": [{"line_id": "l1", "start_sec": 0.0, "end_sec": 1.0}],
    }
    root = _parse(build_fcpxml(tts, style={"default": {"stroke_width": 0}}))
    ts = root.find(".//text-style-def/text-style")
    assert ts.get("strokeWidth") is None
    assert ts.get("strokeColor") is None


def test_uses_text_not_caption_and_no_emoji():
    tts = _load_tts()
    root = _parse(build_fcpxml(tts))
    first_text = root.findall(".//text/text-style")[0].text
    assert first_text == "みんな！ラリー・シルバースタインって名前、覚えてるのだ？"
    assert "😆" not in first_text


def test_ignores_caption_field():
    # caption はキャラの字幕表示名/TTS VoiceDesignスタイル指示であり字幕本文ではない。
    # 非nullでも text を使う（話者名だけの字幕になるバグの再発防止）。
    tts = {
        "audio_files": [
            {"line_id": "l1", "order": 1, "speaker_id": "a", "text": "本文テキスト", "caption": "ルカ"},
        ],
        "timeline": [{"line_id": "l1", "start_sec": 0.0, "end_sec": 1.0}],
    }
    root = _parse(build_fcpxml(tts))
    first_text = root.findall(".//text/text-style")[0].text
    assert first_text == "本文テキスト"
