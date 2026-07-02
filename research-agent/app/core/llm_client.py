"""
research-agent の頭脳。Gemini API（このプロジェクト専用キー）を主、Cloudflare Workers AI を
協調フォールバックにする薄いラッパー。

- 専用キー RESEARCH_GEMINI_API_KEY を使う（画像生成アカウントの GEMINI_API_KEY とクォータ分離）。
  未設定なら GEMINI_API_KEY にフォールバック。
- Gemini がクォータ/レート(429)・一時障害(503)で落ちたら Cloudflare Llama へ自動退避。
"""
import logging
import os
from typing import Optional

import litellm

from app.core import cloudflare_text

litellm.set_verbose = False
logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("RESEARCH_DEFAULT_MODEL", "gemini/gemini-2.5-pro")
FLASH_MODEL = os.getenv("RESEARCH_FLASH_MODEL", "gemini/gemini-2.5-flash")


def gemini_api_key() -> Optional[str]:
    """専用キー優先。無ければ共有キー。"""
    return os.getenv("RESEARCH_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")


def list_models() -> list[dict]:
    """UI のモデル選択用。利用可能なものだけ available=True。"""
    gemini_ok = bool(gemini_api_key())
    cf_ok = cloudflare_text.is_configured()
    return [
        {"id": "gemini/gemini-2.5-pro", "label": "Gemini 2.5 Pro（合成・既定）", "available": gemini_ok},
        {"id": "gemini/gemini-2.5-flash", "label": "Gemini 2.5 Flash（高速）", "available": gemini_ok},
        {"id": "gemini/gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "available": gemini_ok},
        {"id": "cloudflare/llama-3.3-70b", "label": "Cloudflare Llama 3.3 70B（保険）", "available": cf_ok},
    ]


def _is_transient(err: Exception) -> bool:
    s = str(err).lower()
    return any(k in s for k in ("429", "quota", "rate", "resourceexhausted", "503", "unavailable", "overloaded"))


async def chat(
    prompt: str,
    model: Optional[str] = None,
    system: Optional[str] = None,
    temperature: float = 0.4,
    max_tokens: int = 8192,
) -> tuple[str, bool]:
    """
    応答テキストと「フォールバックを使ったか」のフラグを返す。
    既定 temperature は低め（要点抽出・構成は再現性重視）。
    """
    model = model or DEFAULT_MODEL

    # Cloudflare を明示指定された場合は直行
    if model.startswith("cloudflare/"):
        return await cloudflare_text.chat(prompt, system=system, max_tokens=max_tokens), True

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {}
    if model.startswith("gemini/"):
        key = gemini_api_key()
        if key:
            kwargs["api_key"] = key

    try:
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            num_retries=3,
            retry_strategy="exponential_backoff_retry",
            timeout=180,
            **kwargs,
        )
        return response.choices[0].message.content, False
    except Exception as e:  # noqa: BLE001 — フォールバック判定のため広く捕捉
        if _is_transient(e) and cloudflare_text.is_configured():
            logger.warning("Gemini failed (%s). Falling back to Cloudflare Llama.", str(e)[:160])
            return await cloudflare_text.chat(prompt, system=system, max_tokens=max_tokens), True
        raise
