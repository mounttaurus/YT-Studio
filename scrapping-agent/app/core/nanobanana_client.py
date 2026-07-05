"""
NanoBanana（Gemini画像生成API）クライアント。

ローカルSD1.5（comfy_client）と並ぶ第2の画像生成プロバイダー。
最大の強み: 参照画像を同梱して生成できるため、キャラクターの顔・衣装の
一貫性がSD1.5のseed固定より遥かに高い（Character Consistencyの本命）。

バックエンドは2系統（NANOBANANA_BACKEND= auto | google | openrouter、既定auto）:
- google:     generativelanguage.googleapis.com 直叩き。ただし無料枠は画像モデルのlimitが0
              （課金必須）。テキストは動くのに画像だけ429になるのはこのため
- openrouter: OpenRouter経由で同モデル（google/gemini-2.5-flash-image）。OPENROUTER_API_KEYで動く
- auto:       googleを試し、クォータ系エラーならopenrouterへ自動フォールバック

seedの概念はなく、一貫性は参照画像とプロンプトで制御する。
"""
import base64
import os

import httpx

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("NANOBANANA_MODEL", "gemini-2.5-flash-image")
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OR_MODEL = os.getenv("NANOBANANA_OR_MODEL", "google/gemini-2.5-flash-image")
OR_URL = "https://openrouter.ai/api/v1/chat/completions"

BACKEND = os.getenv("NANOBANANA_BACKEND", "auto")  # auto | google | openrouter

_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def is_configured() -> bool:
    if BACKEND == "google":
        return bool(GEMINI_API_KEY)
    if BACKEND == "openrouter":
        return bool(OPENROUTER_API_KEY)
    return bool(GEMINI_API_KEY or OPENROUTER_API_KEY)


def mime_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return _MIME_BY_EXT.get(ext, "image/png")


def _with_ref_instruction(prompt: str, labels: list[str] | None) -> str:
    """参照画像の役割（自由ラベル）をImage順で列挙し、プロンプト先頭に差し込む。

    labelsは添付画像と lockstep（同順・最大3）。ラベル空の画像は汎用一貫性指示にフォールバック。
    """
    if not labels:
        return prompt
    lines = []
    for i, lab in enumerate(labels, 1):
        lab = (lab or "").strip()
        if lab:
            lines.append(f"- Image {i}: {lab}")
        else:
            lines.append(f"- Image {i}: keep this consistent (same face, hairstyle, outfit)")
    return (
        "Use the attached reference image(s). For each, follow the noted role:\n"
        + "\n".join(lines)
        + "\n"
        + prompt
    )


def _labels_of(reference_images: list[tuple] | None) -> list[str]:
    """(data, mime[, label]) のリストから label 列を取り出す（labelが無ければ空文字）。"""
    out = []
    for item in (reference_images or [])[:3]:
        out.append(item[2] if len(item) >= 3 else "")
    return out


async def generate_one(
    prompt: str,
    reference_images: list[tuple] | None = None,
    aspect: str = "1:1",
    allow_fallback: bool = True,
) -> bytes:
    """1枚生成してPNG/JPEGバイト列を返す。reference_imagesは(データ, mime[, label])のリスト（最大3枚使用）。

    BACKEND=autoの場合、Google直叩きがクォータ/権限エラーならOpenRouterへ自動フォールバック。
    allow_fallback=False にすると auto でも OpenRouter へ退避しない（OpenRouter経由の画像は
    Free表示でも課金されるため、バッチ生成は既定で退避を止めて予期しない課金を防ぐ）。
    BACKEND="openrouter" の明示設定は退避ではなくユーザーの選択なので常に従う。
    """
    if BACKEND == "openrouter":
        return await _generate_openrouter(prompt, reference_images, aspect)
    if BACKEND == "auto" and not GEMINI_API_KEY:
        if not allow_fallback:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured（課金退避OFFのためOpenRouterへは退避しません）"
            )
        return await _generate_openrouter(prompt, reference_images, aspect)
    try:
        return await _generate_google(prompt, reference_images, aspect)
    except RuntimeError as e:
        quota_error = any(s in str(e) for s in ("429", "RESOURCE_EXHAUSTED", "PERMISSION_DENIED", "403"))
        if allow_fallback and BACKEND == "auto" and OPENROUTER_API_KEY and quota_error:
            return await _generate_openrouter(prompt, reference_images, aspect)
        raise


async def _generate_google(
    prompt: str,
    reference_images: list[tuple] | None = None,
    aspect: str = "1:1",
) -> bytes:
    parts: list[dict] = []
    for item in (reference_images or [])[:3]:
        data, mime = item[0], item[1]
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    labels = _labels_of(reference_images) if reference_images else None
    parts.append({"text": _with_ref_instruction(prompt, labels)})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": aspect},
        },
    }
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    async with httpx.AsyncClient(timeout=180.0) as client:
        res = await client.post(API_URL, json=body, headers=headers)
        if res.status_code == 400 and "imageConfig" in res.text:
            # 旧モデル/旧APIバージョンではimageConfig未対応 → 外してリトライ
            body["generationConfig"].pop("imageConfig", None)
            res = await client.post(API_URL, json=body, headers=headers)
        if res.status_code != 200:
            raise RuntimeError(f"NanoBanana API error {res.status_code}: {res.text[:300]}")
        data = res.json()

    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                return base64.b64decode(inline["data"])
    # 画像が返らない＝セーフティブロック等。finishReasonを含めて報告する
    reason = (data.get("candidates") or [{}])[0].get("finishReason", "unknown")
    raise RuntimeError(f"NanoBanana returned no image (finishReason={reason}): {str(data)[:300]}")


async def _generate_openrouter(
    prompt: str,
    reference_images: list[tuple] | None = None,
    aspect: str = "1:1",
) -> bytes:
    """OpenRouter経由（OpenAI互換chat/completions、modalities=image）。"""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")
    content: list[dict] = []
    for item in (reference_images or [])[:3]:
        data, mime = item[0], item[1]
        b64 = base64.b64encode(data).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    # OpenRouter経由ではimageConfigを渡せないため、アスペクト比はプロンプトで指示する
    labels = _labels_of(reference_images) if reference_images else None
    text = _with_ref_instruction(prompt, labels)
    content.append({"type": "text", "text": f"{text}\n\nGenerate the image with a {aspect} aspect ratio."})

    body = {
        "model": OR_MODEL,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
    }
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    async with httpx.AsyncClient(timeout=180.0) as client:
        res = await client.post(OR_URL, json=body, headers=headers)
        if res.status_code != 200:
            raise RuntimeError(f"NanoBanana(OpenRouter) API error {res.status_code}: {res.text[:300]}")
        data = res.json()

    for img in (data.get("choices") or [{}])[0].get("message", {}).get("images", []):
        url = (img.get("image_url") or {}).get("url", "")
        if url.startswith("data:") and "base64," in url:
            return base64.b64decode(url.split("base64,", 1)[1])
    raise RuntimeError(f"NanoBanana(OpenRouter) returned no image: {str(data)[:300]}")
