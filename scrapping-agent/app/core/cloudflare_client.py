"""
Cloudflare Workers AI クライアント — NanoBananaが規約で拒む画像の外部フォールバック先。

オープンモデル(FLUX.2 [klein] 4B 等)をホストするため Gemini のような語レベル拒否が無く、
骸骨・爆発・政治家など機微な題材を生成しやすい。Neurons建ての無料枠/日があり、構築中の
テスト反復に向く（全モデルが同一APIで課金の見通しが良い）。

主用途は写真→イラスト化(i2i): input_image_0..3 に最大4枚（各 512px 以下が必須）。
"take the subject of image 1 and style it like image 0" のように、プロンプトで
コンテンツ／スタイルを分離して指示できる。
- API: POST /accounts/{account_id}/ai/run/{model}（multipart/form-data・Bearer 認証）
- 入力画像は 512x512 以下が必須（超過はエラー）→ サーバ側 Pillow で縮小して送る
- steps は klein では 4 固定（指定不可）。出力は base64（JSON result.image）
"""
import base64
import io
import os

import httpx
from PIL import Image

CF_API_KEY = os.getenv("CLOUDFLARE_API_KEY", "")
CF_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CF_MODEL = os.getenv("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-2-klein-4b")

MAX_REF_PX = 512   # Workers AI の入力画像上限（超えるとエラー）
MAX_REFS = 4       # input_image_0..3

_ASPECT_DIMS = {
    "1:1": (1024, 1024), "16:9": (1024, 576), "9:16": (576, 1024),
    "4:3": (1024, 768), "3:4": (768, 1024),
}


def is_configured() -> bool:
    return bool(CF_API_KEY and CF_ACCOUNT_ID)


def _api_url(model: str) -> str:
    return f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{model}"


def _to_png(data: bytes) -> bytes:
    """生成結果(klein は JPEG 直返し)を PNG に正規化する。

    自由生成の保存系は .png 前提（候補URL・iTXtメタ埋め込み）なので、ここで統一しておく。
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _dims_from_ref(data: bytes, max_side: int = 1024, multiple: int = 16) -> tuple[int, int]:
    """参照画像のアスペクト比を保った出力寸法を返す（i2iの横伸ばし防止）。

    長辺を max_side に合わせ、multiple の倍数へ丸める。元画像が縦長なら出力も縦長になる。
    """
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    scale = max_side / max(w, h)
    rnd = lambda x: max(multiple, round(x * scale / multiple) * multiple)
    return rnd(w), rnd(h)


def _downscale(data: bytes, max_px: int = MAX_REF_PX) -> bytes:
    """参照画像を長辺<=max_pxに縮小しPNGバイトで返す（Workers AIの512px制約対策）。"""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    scale = min(max_px / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def generate_one(
    prompt: str,
    reference_images: list[tuple] | None = None,
    aspect: str = "1:1",
    model: str | None = None,
) -> bytes:
    """1枚生成しPNG/画像バイト列を返す。reference_imagesは(データ, mime[, label])のリスト（最大4枚）。

    nanobanana_client.generate_one と同シグネチャ（model 追加）。multipart 必須・参照は512px以下へ縮小。
    """
    if not is_configured():
        raise RuntimeError("Cloudflare (CLOUDFLARE_API_KEY/CLOUDFLARE_ACCOUNT_ID) is not configured")
    model = model or CF_MODEL
    # i2i は参照画像のアスペクトを保持（横伸ばし防止）。t2i は指定アスペクト。
    if reference_images:
        w, h = _dims_from_ref(reference_images[0][0])
    else:
        w, h = _ASPECT_DIMS.get(aspect, (1024, 1024))

    # テキスト欄も (None, value) で files に載せ、prompt only でも multipart を保証する。
    parts: list = [
        ("prompt", (None, prompt)),
        ("width", (None, str(w))),
        ("height", (None, str(h))),
    ]
    for i, item in enumerate((reference_images or [])[:MAX_REFS]):
        parts.append((f"input_image_{i}", (f"ref{i}.png", _downscale(item[0]), "image/png")))

    headers = {"Authorization": f"Bearer {CF_API_KEY}"}
    async with httpx.AsyncClient(timeout=180.0) as client:
        res = await client.post(_api_url(model), files=parts, headers=headers)
        if res.status_code != 200:
            # code 3030 / "flagged" = Cloudflareのコンテンツモデレーション（AUP）。
            # 有名人など実在人物の肖像で多発。Cloudflareは切るトグルが無いのでRunware等へ誘導する。
            if '"code":3030' in res.text or "flagged" in res.text.lower():
                raise RuntimeError(
                    "Cloudflareのコンテンツフィルタにブロックされました（有名人・実在人物の肖像など）。"
                    "この題材は別プロバイダ（Runware等）をお試しください。"
                )
            raise RuntimeError(f"Cloudflare API error {res.status_code}: {res.text[:300]}")
        # 成功時は JSON {result:{image:<b64>}} か、画像バイナリ直返しの両対応
        if "application/json" in res.headers.get("content-type", ""):
            body = res.json()
            if not body.get("success", True):
                raise RuntimeError(f"Cloudflare returned error: {str(body)[:300]}")
            img_b64 = (body.get("result") or {}).get("image")
            if not img_b64:
                raise RuntimeError(f"Cloudflare returned no image: {str(body)[:300]}")
            return _to_png(base64.b64decode(img_b64))
        return _to_png(res.content)


async def list_image_models() -> list[dict]:
    """アカウントが使える Image-to-Image モデルを返す（実在確認・モデル選択UI用）。"""
    if not is_configured():
        return []
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/models/search"
    headers = {"Authorization": f"Bearer {CF_API_KEY}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(url, params={"task": "Image-to-Image", "per_page": 100}, headers=headers)
        res.raise_for_status()
        return [{"name": m.get("name"), "description": m.get("description")}
                for m in res.json().get("result", [])]
