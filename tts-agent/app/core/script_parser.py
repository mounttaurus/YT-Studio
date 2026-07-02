import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Line:
    id: str
    order: int
    speaker_id: str
    speaker_name: str
    text: str
    emotion: str = "neutral"
    speed: float = 1.0
    pause_after_sec: float = 0.3
    section: str = "main"
    notes: str = ""


def parse_script_json(path: Path) -> list[Line]:
    data = json.loads(path.read_text(encoding="utf-8"))
    lines = []
    for item in data.get("lines", []):
        lines.append(Line(
            id=item["id"],
            order=item["order"],
            speaker_id=item["speaker_id"],
            speaker_name=item.get("speaker_name", item["speaker_id"]),
            text=item["text"],
            emotion=item.get("emotion", "neutral"),
            speed=item.get("speed", 1.0),
            pause_after_sec=item.get("pause_after_sec", 0.3),
            section=item.get("section", "main"),
            notes=item.get("notes", ""),
        ))
    return lines


def parse_plain_text(text: str, speakers: list[dict], style: str = "dialogue") -> list[Line]:
    """
    プレーンテキストを Lines に変換する。
    dialogue: "A: テキスト" または "B: テキスト" 形式
    monologue/narration: 全行を speaker_a として扱う
    """
    lines = []
    speaker_a = speakers[0] if speakers else {"id": "speaker_a", "name": "話者A"}
    speaker_b = speakers[1] if len(speakers) > 1 else speaker_a

    for i, raw_line in enumerate(text.strip().splitlines(), start=1):
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        speaker = speaker_a
        content = raw_line

        if style == "dialogue":
            m = re.match(r"^([AB]|speaker_[ab])\s*[:：]\s*(.+)$", raw_line, re.IGNORECASE)
            if m:
                tag = m.group(1).upper()
                content = m.group(2).strip()
                speaker = speaker_b if tag in ("B", "SPEAKER_B") else speaker_a

        lines.append(Line(
            id=f"line_{i:03d}",
            order=i,
            speaker_id=speaker["id"],
            speaker_name=speaker.get("name", speaker["id"]),
            text=content,
        ))

    return lines


def parse_colon_format(text: str, speaker_map: dict) -> list[dict]:
    """
    'A: テキスト' 形式をパース。
    speaker_map = {"A": {"voice": "none", "emotion": "neutral"}}
    """
    lines = []
    order = 1
    for raw in text.strip().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = re.match(r"^(.+?)\s*[:：]\s*(.+)$", raw)
        if m:
            speaker_key = m.group(1).strip()
            content = m.group(2).strip()
            info = speaker_map.get(speaker_key, {"voice": "none", "emotion": "neutral"})
            lines.append({
                "id": f"line_{order:03d}",
                "order": order,
                "speaker_id": f"speaker_{re.sub(r'[^a-zA-Z0-9_]', '_', speaker_key).lower()}",
                "speaker_name": speaker_key,
                "text": content,
                "emotion": info.get("emotion", "neutral"),
                "speed": 1.0,
                "pause_after_sec": 0.3,
                "section": "main",
                "notes": "",
            })
            order += 1
    return lines


def parse_bracket_format(text: str, speaker_map: dict) -> list[dict]:
    """
    '【話者名】テキスト' 形式をパース。
    """
    pattern = re.compile(r"^【(.+?)】\s*(.+)$")
    lines = []
    order = 1
    for raw in text.strip().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = pattern.match(raw)
        if m:
            speaker_key = m.group(1)
            content = m.group(2).strip()
            info = speaker_map.get(speaker_key, {"voice": "none", "emotion": "neutral"})
            lines.append({
                "id": f"line_{order:03d}",
                "order": order,
                "speaker_id": f"speaker_{re.sub(r'[^a-zA-Z0-9_]', '_', speaker_key).lower()}",
                "speaker_name": speaker_key,
                "text": content,
                "emotion": info.get("emotion", "neutral"),
                "speed": 1.0,
                "pause_after_sec": 0.3,
                "section": "main",
                "notes": "",
            })
            order += 1
    return lines


def detect_speakers(text: str, fmt: str = "colon") -> list[str]:
    """テキストから話者名を順序付きで重複なく抽出する"""
    seen = []
    if fmt == "bracket":
        pattern = re.compile(r"^【(.+?)】")
        for raw in text.strip().splitlines():
            m = pattern.match(raw.strip())
            if m:
                name = m.group(1)
                if name not in seen:
                    seen.append(name)
    else:
        pattern = re.compile(r"^(.+?)\s*[:：]")
        for raw in text.strip().splitlines():
            m = pattern.match(raw.strip())
            if m:
                name = m.group(1).strip()
                if name not in seen:
                    seen.append(name)
    return seen
