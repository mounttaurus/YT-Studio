"""
LiteLLM ラッパー（scrapping-agent用の軽量版）。
キーワード抽出にのみ使うため、scripting-agentのようなプロバイダー一覧UIは持たない。
"""
import os
from typing import Optional

import litellm

litellm.set_verbose = False


def get_default_model() -> str:
    # OpenRouterは中間業者でマージンが乗るため、オリジナルAPIキーがある以上そちらを優先する。
    # 既定は直接Anthropic APIのSonnet 4.6（OpenRouter非経由）。
    return os.getenv("DEFAULT_LLM_MODEL", "anthropic/claude-sonnet-4-6")


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


async def chat(
    prompt: str,
    model: Optional[str] = None,
    system: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 2048,
) -> str:
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

    response = await litellm.acompletion(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        **_build_api_kwargs(model),
    )
    return response.choices[0].message.content
