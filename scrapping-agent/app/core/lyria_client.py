"""
Lyria（Google音楽生成）クライアント — BGM/効果音生成。

NanoBanana(nanobanana_client.py)と同じ2系統バックエンド（LYRIA_BACKEND= auto | google | openrouter、既定auto）:
- google:     generativelanguage.googleapis.com 直叩き（generateContent、WebSocket不要。
              Lyria 3 / Lyria 3 Pro は通常のgenerateContentで叩ける。RealTime版のみWebSocket必須で別物）
- openrouter: OpenRouter経由（モデル一覧上はFree表示だが、メディア生成のため実際は課金される。
              2026-06実測でGoogle直より高額）
- auto:       googleを試し、クォータ系エラーならopenrouterへ自動フォールバック

OpenRouter経由の仕様（参考）:
- chat/completions に modalities=["audio","text"] かつ stream=True が必須
  （非ストリームだと 400 "Audio output requires stream: true"）
- 音声はSSEデルタの delta.audio.data に base64 で（複数チャンクに分割され得る）届く
返るのは MP3（先頭が "ID3"）。format フィールドは無いのでMP3固定で扱う。
"""
import base64
import json
import os

import httpx

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("LYRIA_MODEL", "lyria-3-clip-preview")
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OR_MODEL = os.getenv("LYRIA_OR_MODEL", "google/lyria-3-clip-preview")
OR_URL = "https://openrouter.ai/api/v1/chat/completions"

BACKEND = os.getenv("LYRIA_BACKEND", "auto")  # auto | google | openrouter


def is_configured() -> bool:
    if BACKEND == "google":
        return bool(GEMINI_API_KEY)
    if BACKEND == "openrouter":
        return bool(OPENROUTER_API_KEY)
    return bool(GEMINI_API_KEY or OPENROUTER_API_KEY)


async def generate_one(prompt: str) -> bytes:
    """プロンプトからBGM/効果音を1本生成し、MP3バイト列を返す。

    BACKEND=autoの場合、Google直叩きがクォータ/権限エラーならOpenRouterへ自動フォールバック。
    """
    if BACKEND == "openrouter" or (BACKEND == "auto" and not GEMINI_API_KEY):
        return await _generate_openrouter(prompt)
    try:
        return await _generate_google(prompt)
    except RuntimeError as e:
        quota_error = any(s in str(e) for s in ("429", "RESOURCE_EXHAUSTED", "PERMISSION_DENIED", "403"))
        if BACKEND == "auto" and OPENROUTER_API_KEY and quota_error:
            return await _generate_openrouter(prompt)
        raise


async def _generate_google(prompt: str) -> bytes:
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["AUDIO", "TEXT"]},
    }
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    async with httpx.AsyncClient(timeout=300.0) as client:
        res = await client.post(API_URL, json=body, headers=headers)
        if res.status_code != 200:
            raise RuntimeError(f"Lyria API error {res.status_code}: {res.text[:300]}")
        data = res.json()

    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                return base64.b64decode(inline["data"])
    reason = (data.get("candidates") or [{}])[0].get("finishReason", "unknown")
    raise RuntimeError(f"Lyria returned no audio (finishReason={reason}): {str(data)[:300]}")


async def _generate_openrouter(prompt: str) -> bytes:
    """OpenRouterのSSEストリームを読み、delta.audio.data（base64）を連結してデコードする。"""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")
    body = {
        "model": OR_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["audio", "text"],
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    b64_parts: list[str] = []
    last_error = None
    async with httpx.AsyncClient(timeout=300.0) as client:
        async with client.stream("POST", OR_URL, json=body, headers=headers) as res:
            if res.status_code != 200:
                detail = (await res.aread()).decode("utf-8", "replace")[:300]
                raise RuntimeError(f"Lyria(OpenRouter) error {res.status_code}: {detail}")
            async for line in res.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if obj.get("error"):
                    last_error = obj["error"]
                    continue
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                audio = delta.get("audio")
                if audio and audio.get("data"):
                    b64_parts.append(audio["data"])

    if not b64_parts:
        raise RuntimeError(f"Lyria(OpenRouter) returned no audio: {str(last_error)[:300]}")
    return base64.b64decode("".join(b64_parts))
