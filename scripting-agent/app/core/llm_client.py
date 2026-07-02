"""
LiteLLM ラッパー — OpenRouter / OpenAI / Gemini / Ollama を統一インターフェースで扱う。
"""
import csv
import os
from pathlib import Path
from typing import Optional

import httpx
import litellm

litellm.set_verbose = False

# ─── OpenRouter 無料モデル一覧の動的読み込み ──────────────────────────────
# app/data/openrouter_free_models.csv は https://openrouter.ai/api/v1/models から
# 無料テキストモデルだけを抜き出したエンドポイント一覧。実在しないモデルID指定による
# NotFoundError を防ぐため、ハードコードではなくこのCSVを正とする。
# UIの「🔄 モデル更新」から refresh_openrouter_models() でこのCSVを最新化できる。
_ENDPOINTS_CSV = Path(__file__).resolve().parents[1] / "data" / "openrouter_free_models.csv"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_CSV_FIELDS = ["Endpoint ID", "Model Name", "Input Price", "Output Price", "Context Length"]

# 画像/動画/音声/埋め込み等、台本生成に使えないモデルを除外するキーワード
_NON_TEXT_KEYWORDS = (
    "image", "vision-only", "video", "audio", "tts", "speech",
    "embedding", "embed", "moderation", "rerank", "whisper", "stt",
)


def _parse_price(raw: Optional[str]) -> Optional[str]:
    """価格セルを表示用に正規化。'Free' はそのまま、'$0.50' は '$0.50/1M' に。空欄はNone。"""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.lower() == "free":
        return "Free"
    return f"{raw}/1M"


def _row_price(row: dict, *keys: str) -> Optional[str]:
    """新旧どちらの列名でも価格セルを拾えるようにする（'Input Price' / 'Input Price (per 1M tokens)'）。"""
    for k in keys:
        if row.get(k) is not None:
            return row[k]
    return None


def _load_openrouter_models() -> list[dict]:
    models: list[dict] = []
    try:
        with open(_ENDPOINTS_CSV, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                endpoint_id = (row.get("Endpoint ID") or "").strip()
                name = (row.get("Model Name") or "").strip()
                if not endpoint_id or not name:
                    continue
                low = (endpoint_id + " " + name).lower()
                if any(kw in low for kw in _NON_TEXT_KEYWORDS):
                    continue
                is_free = endpoint_id.endswith(":free")
                input_price = _parse_price(_row_price(row, "Input Price", "Input Price (per 1M tokens)"))
                output_price = _parse_price(_row_price(row, "Output Price", "Output Price (per 1M tokens)"))
                # 価格データから「無料」を判定できる場合はそちらを優先（:free サフィックスが無いケースもカバー）
                if input_price == "Free" and output_price == "Free":
                    is_free = True
                models.append({
                    "id": f"openrouter/{endpoint_id}",
                    "label": name,
                    "tier": "free" if is_free else "mid",
                    "category": "free" if is_free else None,
                    "input_price": input_price,
                    "output_price": output_price,
                })
    except OSError:
        pass
    return models


# PROVIDERS[0]["models"] がこのリスト“オブジェクト”を参照する。refresh時は中身を入れ替える
# （clear→extend）ことで参照を保ったままホットリロードする。
_OPENROUTER_MODELS = _load_openrouter_models()


def reload_openrouter_models() -> int:
    """CSVを読み直して in-memory のモデル一覧を入れ替える。件数を返す。"""
    new = _load_openrouter_models()
    _OPENROUTER_MODELS.clear()
    _OPENROUTER_MODELS.extend(new)
    return len(_OPENROUTER_MODELS)


def _is_free_pricing(pricing: dict) -> bool:
    def _zero(v) -> bool:
        try:
            return float(v) == 0.0
        except (TypeError, ValueError):
            return False
    return _zero(pricing.get("prompt")) and _zero(pricing.get("completion"))


def _is_text_output(arch: dict) -> bool:
    """テキストを出力できるモデルか。output_modalities 優先、無ければ modality 文字列で判定。"""
    out = arch.get("output_modalities") or []
    if out:
        return "text" in out
    return "text" in (arch.get("modality") or "")


async def refresh_openrouter_models() -> int:
    """
    OpenRouter公開API(/models)から無料テキストモデルを取得し、CSVを書き換えてホットリロードする。
    APIキー不要。取得→書き込み成功後に in-memory を更新し、書き込んだ件数を返す。
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(OPENROUTER_MODELS_URL)
        resp.raise_for_status()
        data = resp.json().get("data", [])

    rows: list[dict] = []
    seen: set[str] = set()
    for m in data:
        mid = (m.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        pricing = m.get("pricing") or {}
        if not _is_free_pricing(pricing):
            continue
        arch = m.get("architecture") or {}
        if not _is_text_output(arch):
            continue
        seen.add(mid)
        rows.append({
            "Endpoint ID": mid,
            "Model Name": (m.get("name") or mid).strip(),
            "Input Price": "Free",
            "Output Price": "Free",
            "Context Length": m.get("context_length") or "",
        })

    _ENDPOINTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(_ENDPOINTS_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    return reload_openrouter_models()

# ─── プロバイダー定義 ────────────────────────────────────────────────────
# 新しいプロバイダー/モデルはここに追記するだけ。コード変更不要。

PROVIDERS: list[dict] = [
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "description": "多数のモデルを1つのAPIで利用",
        "color": "#6d28d9",
        "requires_key": "OPENROUTER_API_KEY",
        "models": _OPENROUTER_MODELS,
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "description": "Claude 直接接続",
        "color": "#cc785c",
        "requires_key": "ANTHROPIC_API_KEY",
        "models": [
            {"id": "anthropic/claude-opus-4-8",   "label": "Claude Opus 4.8",   "tier": "high"},
            {"id": "anthropic/claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "tier": "mid"},
            {"id": "anthropic/claude-haiku-4-5",  "label": "Claude Haiku 4.5",  "tier": "fast"},
        ],
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "description": "OpenAI 直接接続",
        "color": "#059669",
        "requires_key": "OPENAI_API_KEY",
        "models": [
            {"id": "openai/gpt-4o",       "label": "GPT-4o",       "tier": "high"},
            {"id": "openai/gpt-4o-mini",  "label": "GPT-4o Mini",  "tier": "mid"},
            {"id": "openai/gpt-4.1",      "label": "GPT-4.1",      "tier": "high"},
            {"id": "openai/gpt-4.1-mini", "label": "GPT-4.1 Mini", "tier": "mid"},
            {"id": "openai/gpt-4.1-nano", "label": "GPT-4.1 Nano", "tier": "fast"},
        ],
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "description": "Google AI 直接接続",
        "color": "#d97706",
        "requires_key": "GEMINI_API_KEY",
        "models": [
            {"id": "gemini/gemini-2.5-pro",        "label": "Gemini 2.5 Pro",        "tier": "high"},
            {"id": "gemini/gemini-2.5-flash",      "label": "Gemini 2.5 Flash",      "tier": "mid"},
            {"id": "gemini/gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "tier": "fast"},
        ],
    },
    {
        # xAI(Grok) 直接接続。litellm は xai/ プレフィクスでネイティブ対応するが、キー名は
        # 本repoの命名(GROK_API_KEY)に合わせるため _build_api_kwargs で api_key を明示注入する。
        # モデルIDは api.x.ai の /v1/language-models 実在リストに準拠（名前が腐ったら .env では
        # なくここを直す＝1正本）。grok-4.3 が現行フラッグシップ。
        "id": "grok",
        "label": "xAI (Grok)",
        "description": "Grok 直接接続",
        "color": "#111827",
        "requires_key": "GROK_API_KEY",
        "models": [
            {"id": "xai/grok-4.3",                       "label": "Grok 4.3",            "tier": "high"},
            {"id": "xai/grok-4.20-0309-reasoning",       "label": "Grok 4.20 (推論)",    "tier": "high"},
            {"id": "xai/grok-4.20-0309-non-reasoning",   "label": "Grok 4.20 (非推論)",  "tier": "mid"},
        ],
    },
    {
        "id": "ollama",
        "label": "Ollama",
        "description": "ローカルLLM（要Ollama起動）",
        "color": "#374151",
        "requires_key": None,
        "models": [
            {"id": "ollama/llama3",          "label": "Llama 3 (8B)",     "tier": "local"},
            {"id": "ollama/llama3:70b",      "label": "Llama 3 (70B)",    "tier": "local"},
            {"id": "ollama/mistral",         "label": "Mistral 7B",       "tier": "local"},
            {"id": "ollama/gemma3",          "label": "Gemma 3",          "tier": "local"},
            {"id": "ollama/qwen2.5:7b",     "label": "Qwen 2.5 7B",      "tier": "local"},
            {"id": "ollama/deepseek-r1:8b", "label": "DeepSeek R1 8B",   "tier": "local"},
        ],
    },
]

# tier ラベル（UIバッジ用）
TIER_LABELS = {
    "high":  {"label": "高品質", "color": "#7c3aed"},
    "mid":   {"label": "バランス", "color": "#0891b2"},
    "fast":  {"label": "高速",   "color": "#059669"},
    "local": {"label": "ローカル","color": "#374151"},
    "free":  {"label": "無料",   "color": "#16a34a"},
}

# OpenRouterモデルのカテゴリ表示名（モデル選択UIのグルーピング用）
CATEGORY_LABELS = {
    "anthropic":   "Anthropic",
    "openai":      "OpenAI",
    "google":      "Google",
    "independent": "独立系",
    "free":        "無料モデル",
}


def _key_ok(provider: dict) -> bool:
    key = provider["requires_key"]
    return key is None or bool(os.getenv(key))


def get_providers() -> list[dict]:
    """
    UIに渡すプロバイダー一覧。
    キー未設定のプロバイダーは available=False として含める（UIで灰色表示）。
    Ollamaは常に available=True。
    """
    result = []
    for p in PROVIDERS:
        available = _key_ok(p)
        models_with_tier = [
            {
                **m,
                "tier_label": TIER_LABELS.get(m.get("tier", "mid"), TIER_LABELS["mid"]),
                "category_label": CATEGORY_LABELS.get(m.get("category"), None),
            }
            for m in p["models"]
        ]
        result.append({
            "id":          p["id"],
            "label":       p["label"],
            "description": p["description"],
            "color":       p["color"],
            "available":   available,
            "models":      models_with_tier,
        })
    return result


def get_available_models() -> list[dict]:
    """後方互換：フラットなモデルリストを返す（MCPツール用）。"""
    result = []
    for p in PROVIDERS:
        if not _key_ok(p):
            continue
        for m in p["models"]:
            result.append({"id": m["id"], "label": f"{m['label']} ({p['label']})", "provider": p["id"]})
    return result


def get_default_model() -> str:
    # OpenRouterは中間業者でマージンが乗るため、オリジナルAPIキーがある以上そちらを優先する。
    # 既定は直接Anthropic APIのSonnet 4.6（OpenRouter非経由）。
    return os.getenv("DEFAULT_LLM_MODEL", "anthropic/claude-sonnet-4-6")


def _is_free_openrouter_model(model: str) -> bool:
    """OpenRouter経由のモデルが無料かどうかを判定する。"""
    endpoint_id = model[len("openrouter/"):]
    if endpoint_id.endswith(":free") or endpoint_id == "openrouter/free":
        return True
    return any(m["id"] == model and m.get("tier") == "free" for m in _OPENROUTER_MODELS)


def get_default_provider() -> str:
    default = get_default_model()
    for p in PROVIDERS:
        if any(m["id"] == default for m in p["models"]):
            return p["id"]
    return PROVIDERS[0]["id"]


def _build_api_kwargs(model: str) -> dict:
    """モデルに応じた追加APIパラメータを返す。"""
    kwargs = {}
    if model.startswith("openrouter/"):
        key = os.getenv("OPENROUTER_API_KEY")
        if key:
            kwargs["api_key"] = key
        kwargs["api_base"] = "https://openrouter.ai/api/v1"
    elif model.startswith("xai/"):
        # litellm は既定で XAI_API_KEY を読むが、本repoの鍵名は GROK_API_KEY。
        # api_key を明示注入し、api_base も固定して経路を確定させる。
        key = os.getenv("GROK_API_KEY")
        if key:
            kwargs["api_key"] = key
        kwargs["api_base"] = "https://api.x.ai/v1"
    elif model.startswith("ollama/"):
        base = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
        kwargs["api_base"] = base
    return kwargs


async def openrouter_credit_balance() -> dict:
    """OpenRouterの残高(累計購入額・累計消費額)を返す。アカウント単位の実額（セッション内カウンタではない）。

    NanoBanana/Lyriaのフォールバック分もOPENROUTER_API_KEYを共有するため、この残高に含まれる。
    """
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {key}"},
        )
        res.raise_for_status()
        data = res.json().get("data", {})
    total = data.get("total_credits", 0)
    used = data.get("total_usage", 0)
    return {"total_credits": total, "total_usage": used, "remaining": round(total - used, 4)}


async def chat(
    prompt: str,
    model: Optional[str] = None,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> str:
    """LLMにプロンプトを送り、応答テキストを返す。"""
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

    # 503 UNAVAILABLE / 429 RateLimit など一時的エラーは自動リトライ（指数バックオフ）。
    # 特に Gemini 無料枠の flash 系はスパイク時に 503 を返しやすい。
    response = await litellm.acompletion(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        num_retries=4,
        retry_strategy="exponential_backoff_retry",
        timeout=120,
        **kwargs,
    )
    return response.choices[0].message.content
