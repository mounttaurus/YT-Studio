"""
Grok（xAI）画像生成クライアント — 自由生成スタジオの第5プロバイダ。

xAI の Grok Imagine 画像モデル。API は OpenAI 互換（Bearer 認証・JSON）。
- t2i: POST https://api.x.ai/v1/images/generations（prompt から生成。aspect_ratio 指定可）
- i2i: POST https://api.x.ai/v1/images/edits（image に参照画像を data URI で渡し、指示文で編集）

鍵は GROK_API_KEY（litellm の既定 XAI_API_KEY ではなく本repoの命名）。
モデル名は固定値（compose の environment）で持つ＝名前が腐ったらそこを直す。
nanobanana_client.generate_one と同シグネチャ（prompt, reference_images, aspect）。
"""
import base64
import io
import os

import httpx
from PIL import Image

GROK_API_KEY = os.getenv("GROK_API_KEY", "")
# t2i/i2i 共通の画像モデル。quality=高品質版（grok-imagine-image=安価版）。
IMAGE_MODEL = os.getenv("GROK_IMAGE_MODEL", "grok-imagine-image-quality")
EDIT_MODEL = os.getenv("GROK_EDIT_MODEL", "grok-imagine-image-quality")
GEN_URL = "https://api.x.ai/v1/images/generations"
EDIT_URL = "https://api.x.ai/v1/images/edits"

_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def is_configured() -> bool:
    return bool(GROK_API_KEY)


def mime_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return _MIME_BY_EXT.get(ext, "image/png")


def _data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _to_png(data: bytes) -> bytes:
    """出力をPNGに正規化する（.png保存＆iTXt埋め込みが前提のため）。既にPNGなら素通し。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _decode_result(client: httpx.AsyncClient, data: dict) -> bytes:
    """OpenAI互換の画像レスポンス(data[].b64_json または data[].url)から画像バイトを取り出す。"""
    items = data.get("data") or []
    if not items:
        raise RuntimeError(f"Grok returned no image: {str(data)[:300]}")
    first = items[0]
    if first.get("b64_json"):
        return _to_png(base64.b64decode(first["b64_json"]))
    url = first.get("url")
    if url:
        img = await client.get(url)
        img.raise_for_status()
        return _to_png(img.content)
    raise RuntimeError(f"Grok returned no image data: {str(data)[:300]}")


async def generate_one(
    prompt: str,
    reference_images: list[tuple] | None = None,
    aspect: str = "1:1",
) -> bytes:
    """1枚生成して画像バイト列を返す。reference_imagesは(データ, mime[, label])のリスト。

    参照ありは images/edits（先頭1枚を編集対象に使用）、参照なしは images/generations（t2i）。
    """
    if not is_configured():
        raise RuntimeError("Grok (GROK_API_KEY) is not configured")
    headers = {"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=180.0) as client:
        if reference_images:
            # i2i: 先頭1枚を編集対象に。editsエンドポイントは aspect_ratio 非対応のため指示文に含める。
            ref = reference_images[0]
            mime = ref[1] if len(ref) > 1 else "image/png"
            body = {
                "model": EDIT_MODEL,
                "prompt": f"{prompt}\n\n(Output aspect ratio: {aspect})",
                "image": {"url": _data_uri(ref[0], mime), "type": "image_url"},
                "response_format": "b64_json",
            }
            res = await client.post(EDIT_URL, json=body, headers=headers)
        else:
            body = {
                "model": IMAGE_MODEL,
                "prompt": prompt,
                "n": 1,
                "response_format": "b64_json",
                "aspect_ratio": aspect,
            }
            res = await client.post(GEN_URL, json=body, headers=headers)

        if res.status_code != 200:
            raise RuntimeError(f"Grok image API error {res.status_code}: {res.text[:400]}")
        return await _decode_result(client, res.json())
