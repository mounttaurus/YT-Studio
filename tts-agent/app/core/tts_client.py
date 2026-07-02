import logging
import os

import httpx

from app.core import audio_utils

IRODORI_SERVER_URL = os.environ.get("IRODORI_SERVER_URL", "http://irodori-tts-server:8088")
DEFAULT_NUM_STEPS = int(os.environ.get("IRODORI_DEFAULT_NUM_STEPS", "40"))
DEFAULT_VOICE = os.environ.get("IRODORI_DEFAULT_VOICE", "")

TIMEOUT = 600.0  # 初回リクエスト時のモデルDL・ロードを考慮

logger = logging.getLogger(__name__)


async def generate(
    text: str,
    voice: str = "none",
    speed: float = 1.0,
    num_steps: int = DEFAULT_NUM_STEPS,
    caption: str | None = None,
    cfg_scale_caption: float = 5.0,
) -> bytes:
    """Irodori-TTS-Server の OpenAI 互換 API を呼び出して音声バイトを返す。

    voice: voices/ フォルダに置いた wav ファイルのstem名（例: "zundamon"）。
           "none" の場合は参照音声なし。
    caption: VoiceDesign モデルのスタイル指示テキスト（任意）。
             ref_wav + caption の組み合わせが最も効果的。
    """
    resolved_voice = voice or DEFAULT_VOICE or "none"

    payload: dict = {
        "model": "irodori-tts",
        "input": text,
        "voice": resolved_voice,
        "speed": speed,
        "response_format": "wav",
        "irodori": {
            "num_steps": num_steps,
        },
    }

    if caption:
        payload["caption"] = caption
        payload["irodori"]["cfg_scale_caption"] = cfg_scale_caption

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(f"{IRODORI_SERVER_URL}/v1/audio/speech", json=payload)
        if resp.status_code >= 400:
            logger.error(
                "Irodori error — status: %d | payload: %s | response: %s",
                resp.status_code, payload, resp.text,
            )
        resp.raise_for_status()
        return audio_utils.to_stereo_wav_bytes(resp.content)


async def check_health() -> dict:
    """Irodori-TTS-Server の死活確認"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{IRODORI_SERVER_URL}/health")
            resp.raise_for_status()
            return {"reachable": True, "url": IRODORI_SERVER_URL}
    except Exception as e:
        return {"reachable": False, "url": IRODORI_SERVER_URL, "error": str(e)}
