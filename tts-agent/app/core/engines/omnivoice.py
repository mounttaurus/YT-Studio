"""
omnivoice-tts-server（多言語エンジン）クライアント — Docs/08_i18n.md §3d, §4, §7 参照。

- ref_audio / ref_text は irodori の声カタログ（VOICES_DIR）に置く `.ref.wav` / `.ref.txt`
  サイドカーを流用する（同じ声で他言語をゼロショットクローン。character.json は変更しない）。
- ref_text は絶対に省略しない。省略するとサーバー内部でWhisperが自動ロードされ
  そのままVRAMへ常駐し、以降の全生成が10倍以上劣化する（Phase1実測済みの罠）。
- position_temperature=0 / class_temperature=0 を固定送信する。既定値(5.0)では
  クロスリンガル生成時に冒頭へ幻覚フレーズが混入し、時間予算を食って文末が欠落する
  現象を実測・特定済み（Docs/08_i18n.md §3d）。
"""
import logging
import os
from pathlib import Path

import httpx

from app.core import audio_utils
from ._lock import INFERENCE_LOCK

ENGINE_NAME = "omnivoice"

OMNIVOICE_SERVER_URL = os.environ.get("OMNIVOICE_SERVER_URL", "http://omnivoice-tts-server:8880")
VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
DEFAULT_NUM_STEP = int(os.environ.get("OMNIVOICE_DEFAULT_NUM_STEP", "32"))

TIMEOUT = 120.0

logger = logging.getLogger(__name__)


class MissingRefAudioError(Exception):
    """OmniVoice用の参照クリップ(.ref.wav)またはref_text(.ref.txt)サイドカーが見つからない。"""


def _ref_paths(voice: str) -> tuple[Path, Path]:
    return VOICES_DIR / f"{voice}.ref.wav", VOICES_DIR / f"{voice}.ref.txt"


def has_ref(voice: str) -> bool:
    """この声にOmniVoice用の参照クリップ一式が揃っているか。"""
    ref_wav, ref_txt = _ref_paths(voice)
    return ref_wav.exists() and ref_txt.exists()


async def generate(
    text: str,
    voice: str = "none",
    speed: float = 1.0,
    num_step: int = DEFAULT_NUM_STEP,
    caption: str | None = None,  # irodoriとシグネチャを揃えるため受理するが未使用
) -> bytes:
    """omnivoice-tts-server の /v1/audio/speech/clone を呼び出して音声バイトを返す。"""
    if voice == "none":
        raise MissingRefAudioError(
            "話者に声が割当されていません（多言語TTSはキャラの声に紐づく参照クリップが必須です）"
        )
    ref_wav, ref_txt = _ref_paths(voice)
    if not ref_wav.exists() or not ref_txt.exists():
        raise MissingRefAudioError(
            f"{voice} に多言語用の参照クリップがありません（{ref_wav.name} / {ref_txt.name} が必要）。"
            "声カタログに .ref.wav（3〜10秒）と .ref.txt（正確な書き起こし）を用意してください"
        )
    ref_text = ref_txt.read_text(encoding="utf-8").strip()
    if not ref_text:
        raise MissingRefAudioError(f"{ref_txt.name} が空です（正確な書き起こしを入れてください）")

    data = {
        "text": text,
        "ref_text": ref_text,
        "speed": speed,
        "num_step": num_step,
        # 冒頭ハルシネーション＋語尾欠落の根治策（Docs/08_i18n.md §3d 実測）。固定・ユーザー調整不可。
        "position_temperature": 0,
        "class_temperature": 0,
        "response_format": "wav",
    }

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        with open(ref_wav, "rb") as f:
            files = {"ref_audio": (ref_wav.name, f, "audio/wav")}
            async with INFERENCE_LOCK:
                resp = await client.post(
                    f"{OMNIVOICE_SERVER_URL}/v1/audio/speech/clone",
                    data=data, files=files,
                )
        if resp.status_code >= 400:
            logger.error(
                "OmniVoice error — status: %d | voice: %s | response: %s",
                resp.status_code, voice, resp.text,
            )
        resp.raise_for_status()
        return audio_utils.to_stereo_wav_bytes(resp.content)


async def check_health() -> dict:
    """omnivoice-tts-server の死活確認"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OMNIVOICE_SERVER_URL}/health")
            resp.raise_for_status()
            body = resp.json()
            return {
                "reachable": True,
                "url": OMNIVOICE_SERVER_URL,
                "model_loaded": body.get("model_loaded", False),
            }
    except Exception as e:
        return {"reachable": False, "url": OMNIVOICE_SERVER_URL, "error": str(e)}
