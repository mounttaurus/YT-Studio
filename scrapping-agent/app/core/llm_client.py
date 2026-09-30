"""
LiteLLM ラッパー（scrapping-agent用の軽量版）。
キーワード抽出にのみ使うため、scripting-agentのようなプロバイダー一覧UIは持たない。
"""
import asyncio
import os
from typing import Optional

import litellm

litellm.set_verbose = False


def get_default_model() -> str:
    # OpenRouterは中間業者でマージンが乗るため、オリジナルAPIキーがある以上そちらを優先する。
    # 既定は直接Anthropic APIのSonnet 5（OpenRouter非経由）。
    return os.getenv("DEFAULT_LLM_MODEL", "anthropic/claude-sonnet-5")


# Anthropicの新世代モデル(Sonnet 5 / Opus 4.7以降 / Fable系)は temperature 等の
# samplingパラメータを非デフォルト値で送ると 400 を返すため、該当モデルには送らない。
_NO_SAMPLING_MARKERS = ("claude-sonnet-5", "claude-opus-4-7", "claude-opus-4-8",
                        "claude-fable", "claude-mythos")


def _supports_temperature(model: str) -> bool:
    return not any(marker in model for marker in _NO_SAMPLING_MARKERS)


def _is_free_openrouter_model(model: str) -> bool:
    """OpenRouter経由のモデルが無料かどうかを判定する（:free サフィックス or Free Models Router）。"""
    endpoint_id = model[len("openrouter/"):]
    return endpoint_id.endswith(":free") or endpoint_id == "openrouter/free"


def _build_api_kwargs(model: str) -> dict:
    kwargs = {}
    if model.startswith("openrouter/"):
        key = os.getenv("OPENROUTER_API_KEY")
        if key:
            kwargs["api_key"] = key
        kwargs["api_base"] = "https://openrouter.ai/api/v1"
    elif model.startswith("ollama/"):
        kwargs["api_base"] = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    return kwargs


class LLMTimeout(Exception):
    """応答が時間内に終わらなかった（プロバイダ側で止まった・混雑）。"""


class LLMRefused(Exception):
    """安全フィルタで拒否された、または中身の無い応答が返った（別のモデルなら通ることがある）。"""


# 安全フィルタの拒否として扱う例外文言（Gemini は SAFETY / PROHIBITED_CONTENT、litellm は content_filter）
_REFUSAL_MARKERS = ("safety", "content_filter", "prohibited_content", "blocked", "recitation")


def is_refusal(err: Exception) -> bool:
    if isinstance(err, LLMRefused):
        return True
    text = str(err).lower()
    return any(m in text for m in _REFUSAL_MARKERS)


async def chat(
    prompt: str,
    model: Optional[str] = None,
    system: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    timeout: Optional[float] = None,
) -> str:
    """1回の問い合わせ。`timeout`（秒）を渡すと、応答全体（ストリームの受信まで）がその時間を
    超えたら `LLMTimeout`。安全フィルタの拒否・空の応答は `LLMRefused`。"""
    if timeout is None:
        return await _chat(prompt, model, system, temperature, max_tokens)
    try:
        return await asyncio.wait_for(_chat(prompt, model, system, temperature, max_tokens), timeout)
    except asyncio.TimeoutError:
        raise LLMTimeout(f"{model or get_default_model()} が {timeout:.0f} 秒以内に応答しませんでした")


async def _chat(prompt, model, system, temperature, max_tokens) -> str:
    model = model or get_default_model()
    # OpenRouterは中間業者でマージンが乗る。オリジナルAPI(anthropic/, openai/, gemini/)がある
    # モデルをOpenRouter経由の有料枠で叩く意味は無いため、無料モデル以外は拒否する。
    if model.startswith("openrouter/") and not _is_free_openrouter_model(model):
        raise ValueError(
            f"OpenRouter経由の有料モデルは使えません: {model}\n"
            "OpenRouterは無料モデル限定です。有料で使うならオリジナルAPI"
            "（anthropic/... , openai/... , gemini/...）を直接指定してください。"
        )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = _build_api_kwargs(model)
    if _supports_temperature(model):
        kwargs["temperature"] = temperature

    # 常にストリーミングで受信して結合する。非ストリーミングだと応答完了まで無通信になり、
    # Docker Desktop(Windows)のNAT経路が約30秒でアイドル接続を切断するため（scripting-agentと同じ対策）。
    stream = await litellm.acompletion(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        stream=True,
        **kwargs,
    )
    parts: list[str] = []
    finish = None
    async for chunk in stream:
        if chunk.choices:
            delta = chunk.choices[0].delta.content
            if delta:
                parts.append(delta)
            finish = getattr(chunk.choices[0], "finish_reason", None) or finish
    text = "".join(parts)
    if finish and str(finish).lower() in ("content_filter", "safety", "prohibited_content", "recitation"):
        raise LLMRefused(f"{model} が安全フィルタで応答を止めました（finish_reason={finish}）")
    if not text.strip():
        raise LLMRefused(f"{model} の応答が空でした（安全フィルタで拒否された時の典型）")
    return text
