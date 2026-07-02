"""
キャラクター・ライブラリの読み取り専用アクセス（scripting-agent 用）。

キャラの本籍は shared/characters/{char_id}/character.json（DATA_SCHEMA §2b）。
CRUD は scrapping-agent の責務。scripting-agent はスタイルの話者解決・UI表示のために
**ディスク直読み**するだけ（tts-agent `_character_voice_caption` と同じ方針）。
HTTP で scrapping-agent に依存しない＝scrapping-agent が落ちていても台本生成は動く
（各素材生成コンテナはスタンドアローンで動く、という憲法を守る）。
"""
import json
import os
from pathlib import Path
from typing import Optional

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
CHARACTERS_DIR = SHARED_DIR / "characters"


def read_character(char_id: str) -> Optional[dict]:
    """character.json をそのまま返す（無ければ None）。"""
    if not char_id:
        return None
    f = CHARACTERS_DIR / char_id / "character.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_characters() -> list[dict]:
    """全キャラの要約一覧（UIの話者ピッカー用）。重い generations は件数のみ。"""
    if not CHARACTERS_DIR.exists():
        return []
    out = []
    for d in sorted(CHARACTERS_DIR.iterdir()):
        if not d.is_dir():
            continue
        c = read_character(d.name)
        if c is None:
            continue
        voice = c.get("voice", {"engine": "", "voice_id": ""})
        refs = sorted(p.name for p in (d / "reference").glob("*") if p.is_file()) \
            if (d / "reference").exists() else []
        out.append({
            "char_id": c["char_id"],
            "name": c.get("name", ""),
            "caption": c.get("caption", ""),
            "description": c.get("description", ""),
            "voice": voice,
            "has_voice": bool(voice.get("voice_id")),
            "references": refs,
            "generation_count": len(c.get("generations", [])),
        })
    return out


def resolve_speaker(character_id: str) -> Optional[dict]:
    """character_id から台本生成・字幕表示に必要な値を解決する。

    名前・声・字幕・性格はキャラが唯一の本籍（スタイルに複製しない＝ドリフトなし）。
    返り値: {char_id, name, caption, voice_id, engine, description} or None。
    """
    c = read_character(character_id)
    if c is None:
        return None
    voice = c.get("voice", {"engine": "", "voice_id": ""})
    return {
        "char_id": c["char_id"],
        "name": c.get("name", ""),
        "caption": c.get("caption", ""),
        "voice_id": voice.get("voice_id", ""),
        "engine": voice.get("engine", ""),
        "description": c.get("description", ""),
    }


def resolve_cast_names(cast_speakers: list[dict]) -> dict:
    """配役（config.tts.speakers[]）から speaker_id → 表示名（キャラのname）を作る。

    名前の唯一の本籍はキャラ（DATA_SCHEMA §2b）。character_id があればキャラ本籍の
    name を採用する（＝リネームが即反映・ドリフトなし）。character_id が無い／解決できない
    役はマップに載せない（呼び出し側で script.json の既存 speaker_name にフォールバックさせる）。
    """
    out: dict[str, str] = {}
    for sp in cast_speakers or []:
        sid = sp.get("id")
        if not sid:
            continue
        char_id = sp.get("character_id", "")
        if not char_id:
            continue
        ch = read_character(char_id)
        if ch and ch.get("name"):
            out[sid] = ch["name"]
    return out
