"""他コンテナのAPIを呼ぶ口（行操作の窓口・派生物が使う）。

URL は `api/routes.py` と同じ環境変数（`docker-compose.yml` の `environment:` が本籍）。
テストは `set_transport()` で httpx のトランスポートを差し替えて、他コンテナ無しで動かす。
"""
from __future__ import annotations

import os

import httpx

URLS = {
    "scripting": os.getenv("SCRIPTING_AGENT_URL", "http://scripting-agent:8002"),
    "tts": os.getenv("TTS_AGENT_URL", "http://tts-agent:8004"),
    "scrapping": os.getenv("SCRAPPING_AGENT_URL", "http://scrapping-agent:8003"),
}

_transport: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """テスト用。None で本物のネットワークへ戻す。"""
    global _transport
    _transport = transport


async def call(service: str, method: str, path: str, *, params: dict | None = None,
               json: dict | None = None, timeout: float = 60.0) -> httpx.Response:
    """`service`（scripting/tts/scrapping）へリクエストする。接続できない時は httpx.RequestError。"""
    async with httpx.AsyncClient(timeout=timeout, transport=_transport) as client:
        return await client.request(method, f"{URLS[service]}{path}", params=params, json=json)


def error_detail(res: httpx.Response) -> str:
    """FastAPI のエラー応答（{"detail": ...}）から人が読める文字列を取り出す。"""
    try:
        d = res.json().get("detail")
        if isinstance(d, str):
            return d
        if d is not None:
            return str(d)
    except Exception:
        pass
    return f"HTTP {res.status_code}: {res.text[:200]}"
