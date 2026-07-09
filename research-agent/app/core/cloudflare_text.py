"""
Cloudflare Workers AI — テキストLLM(フォールバック頭脳) と Whisper STT(音声書き起こし)。

Geminiの代替ではなく協調（保険＋音声の入口）。鍵は scrapping-agent の画像クライアントと共有
（CLOUDFLARE_API_KEY / CLOUDFLARE_ACCOUNT_ID）。モデル名は compose の environment に固定。
"""
import base64
import json
import os

import httpx

CF_API_KEY = os.getenv("CLOUDFLARE_API_KEY", "")
CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CF_TEXT_MODEL = os.getenv("CLOUDFLARE_TEXT_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
CF_STT_MODEL = os.getenv("CLOUDFLARE_STT_MODEL", "@cf/openai/whisper-large-v3-turbo")


def is_configured() -> bool:
    return bool(CF_API_KEY and CF_ACCOUNT_ID)


def _url(model: str) -> str:
    return f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{model}"


async def chat(prompt: str, system: str | None = None, max_tokens: int = 4096) -> str:
    """テキスト生成。Geminiがクォータ/レート超過した時のフォールバック頭脳。"""
    if not is_configured():
        raise RuntimeError("Cloudflare (CLOUDFLARE_API_KEY/CLOUDFLARE_ACCOUNT_ID) is not configured")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    headers = {"Authorization": f"Bearer {CF_API_KEY}", "Content-Type": "application/json"}
    payload = {"messages": messages, "max_tokens": max_tokens}
    async with httpx.AsyncClient(timeout=180.0) as client:
        res = await client.post(_url(CF_TEXT_MODEL), json=payload, headers=headers)
        if res.status_code != 200:
            raise RuntimeError(f"Cloudflare text API error {res.status_code}: {res.text[:300]}")
        body = res.json()
        if not body.get("success", True):
            raise RuntimeError(f"Cloudflare returned error: {str(body)[:300]}")
        response = (body.get("result") or {}).get("response", "") or ""
        # Workers AI はJSONを要求すると response をdictで返すことがある → 常にstrへ正規化
        if not isinstance(response, str):
            response = json.dumps(response, ensure_ascii=False)
        return response


async def transcribe(audio_bytes: bytes) -> str:
    """音声/動画の音声トラックを Whisper で書き起こす。Geminiの無料枠を消費せず音声を扱える。"""
    if not is_configured():
        raise RuntimeError("Cloudflare (CLOUDFLARE_API_KEY/CLOUDFLARE_ACCOUNT_ID) is not configured")
    headers = {"Authorization": f"Bearer {CF_API_KEY}", "Content-Type": "application/json"}
    payload = {"audio": base64.b64encode(audio_bytes).decode("ascii")}
    async with httpx.AsyncClient(timeout=300.0) as client:
        res = await client.post(_url(CF_STT_MODEL), json=payload, headers=headers)
        if res.status_code != 200:
            raise RuntimeError(f"Cloudflare STT API error {res.status_code}: {res.text[:300]}")
        body = res.json()
        return (body.get("result") or {}).get("text", "") or ""
