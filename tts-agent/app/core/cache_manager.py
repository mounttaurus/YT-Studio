import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path(os.environ.get("TTS_CACHE_DIR", "/app/tts_cache"))
CACHE_ENABLED = os.environ.get("TTS_CACHE_ENABLED", "true").lower() == "true"
INDEX_FILE = CACHE_DIR / "index.json"


def get_cache_key(text: str, voice_id: str, engine: str, caption: str | None = None, speed: float = 1.0) -> str:
    # caption / speed は音声のスタイルや再生時間に影響するためキーに含める
    # （含めないと、キャプションや速度を変更してもキャッシュヒットして
    # 音声が変わらないバグになる）
    raw = f"{text}|{voice_id}|{engine}|{caption or ''}|{speed}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"entries": []}


def _save_index(index: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_hit(key: str) -> Path | None:
    if not CACHE_ENABLED:
        return None
    path = CACHE_DIR / f"{key}.wav"
    return path if path.exists() else None


def save_to_cache(key: str, audio_bytes: bytes, text: str, voice_id: str, engine: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.wav"
    path.write_bytes(audio_bytes)

    index = _load_index()
    # 重複エントリを除去してから追加
    index["entries"] = [e for e in index["entries"] if e.get("hash") != key]
    index["entries"].append({
        "hash": key,
        "text": text[:100],
        "voice_id": voice_id,
        "engine": engine,
        "file_path": f"tts_cache/{key}.wav",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_index(index)
    return path
