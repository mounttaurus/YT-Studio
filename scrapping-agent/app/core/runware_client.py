"""
Runware クライアント — 画像生成の本命プロバイダ（NanoBanana/Cloudflareが規約で拒む題材用）。

オープンモデル基盤(GPU)で safety checker が無い/任意のため、有名人・政治家・故人など
実在人物の肖像も生成できる（Cloudflareの output flag=AUP code 3030 を回避できる本命）。
SD/SDXL/FLUX を AIR識別子で選べ、img2img(seedImage)で原画像の忠実な画風変換ができる。

- API: POST https://api.runware.ai/v1（JSON配列・Bearer認証）
- 1リクエスト=タスク配列。imageInference タスクで t2i / i2i(seedImage+strength)。
- 出力は imageURL（HTTPS）。ここでDLして画像バイトで返す。
- width/height は 64 の倍数が必要。
"""
import base64
import io
import os
import uuid

import httpx
from PIL import Image

RUNWARE_API_KEY = os.getenv("RUNWARE_API_KEY", "")
# t2i用: 安価・高速な FLUX.1 schnell（AIR: runware:100@1）。dev=runware:101@1。
RUNWARE_MODEL = os.getenv("RUNWARE_IMAGE_MODEL", "runware:100@1")
# i2i編集用: FLUX.1 Kontext dev（runware:106@1）。指示ベースで顔の同一性を保ったまま画風変換。
# plain img2imgと違い「本人を保つ＋画風を変える」を両立できる（有名人の忠実イラスト化の本命）。
RUNWARE_EDIT_MODEL = os.getenv("RUNWARE_EDIT_MODEL", "runware:106@1")
API_URL = "https://api.runware.ai/v1"


def _is_kontext(model: str) -> bool:
    return "106@1" in model or "kontext" in model.lower()

_ASPECT_DIMS = {
    "1:1": (1024, 1024), "16:9": (1024, 576), "9:16": (576, 1024),
    "4:3": (1024, 768), "3:4": (768, 1024),
}


def is_configured() -> bool:
    return bool(RUNWARE_API_KEY)


def _data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# FLUX Kontext が受け付ける解像度バケット（~1MP・各種アスペクト）。任意寸法は unsupportedDimensions。
_KONTEXT_DIMS = [
    (672, 1568), (688, 1504), (720, 1456), (752, 1392), (800, 1328), (832, 1248),
    (880, 1184), (944, 1104), (1024, 1024), (1104, 944), (1184, 880), (1248, 832),
    (1328, 800), (1392, 752), (1456, 720), (1504, 688), (1568, 672),
]


def _kontext_dims(data: bytes) -> tuple[int, int]:
    """参照画像のアスペクトに最も近い Kontext 許可バケットを返す（横伸ばし＆寸法エラー防止）。"""
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    ar = w / h
    return min(_KONTEXT_DIMS, key=lambda d: abs(d[0] / d[1] - ar))


def _dims_from_ref(data: bytes, max_side: int = 1024, multiple: int = 64,
                   min_side: int = 0) -> tuple[int, int]:
    """参照画像のアスペクト比を保った出力寸法を返す（i2iの横伸ばし防止）。

    長辺を max_side に合わせ multiple の倍数へ丸める。min_side>0 ならその下限へクランプ
    （Kontextは width/height とも 672 以上が必要）。
    """
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    scale = max_side / max(w, h)
    rnd = lambda x: max(multiple, round(x * scale / multiple) * multiple)
    ow, oh = rnd(w), rnd(h)
    if min_side:
        ow, oh = max(ow, min_side), max(oh, min_side)
    return ow, oh


async def generate_one(
    prompt: str,
    reference_images: list[tuple] | None = None,
    aspect: str = "1:1",
    model: str | None = None,
    strength: float = 0.75,
    negative: str = "",
) -> bytes:
    """1枚生成し画像バイト列を返す。reference_images=(データ,mime[,label])。先頭1枚をseedImage(i2i)に使う。

    nanobanana_client.generate_one と同シグネチャ（model/strength/negative 追加）。
    """
    if not is_configured():
        raise RuntimeError("Runware (RUNWARE_API_KEY) is not configured")
    # モデル自動選択: 参照あり=編集(Kontext) / 参照なし=t2i(schnell)。明示modelがあれば優先。
    if model is None:
        model = RUNWARE_EDIT_MODEL if reference_images else RUNWARE_MODEL
    kontext = _is_kontext(model)

    # i2i は参照画像のアスペクトを保持（横伸ばし防止）。t2i は指定アスペクト。
    if reference_images:
        if kontext:
            w, h = _kontext_dims(reference_images[0][0])
        else:
            w, h = _dims_from_ref(reference_images[0][0], max_side=1024, multiple=64)
    else:
        w, h = _ASPECT_DIMS.get(aspect, (1024, 1024))

    task: dict = {
        "taskType": "imageInference",
        "taskUUID": str(uuid.uuid4()),
        "positivePrompt": prompt,
        "model": model,
        "width": w,
        "height": h,
        "numberResults": 1,
        "outputType": "URL",
        "outputFormat": "PNG",
    }
    if reference_images:
        uris = []
        for it in reference_images[:4]:
            uris.append(_data_uri(it[0], it[1] if len(it) > 1 else "image/png"))
        if kontext:
            # Kontext: 指示ベース編集。referenceImages配列・strength無し・identity保持。
            task["referenceImages"] = uris
        else:
            # 通常img2img: seedImage(先頭1枚)+strength(=変化量)。
            task["seedImage"] = uris[0]
            task["strength"] = max(0.0, min(strength, 1.0))
    # Kontextはnegative非対応。通常モデルのみ付与。
    if negative.strip() and not kontext:
        task["negativePrompt"] = negative.strip()

    headers = {"Authorization": f"Bearer {RUNWARE_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=180.0) as client:
        res = await client.post(API_URL, json=[task], headers=headers)
        if res.status_code != 200:
            raise RuntimeError(f"Runware API error {res.status_code}: {res.text[:400]}")
        body = res.json()
        if isinstance(body, dict) and body.get("errors"):
            raise RuntimeError(f"Runware error: {str(body['errors'])[:400]}")
        items = body.get("data") if isinstance(body, dict) else None
        url = next((it.get("imageURL") for it in (items or []) if it.get("imageURL")), None)
        if not url:
            raise RuntimeError(f"Runware returned no image: {str(body)[:400]}")
        img = await client.get(url)
        img.raise_for_status()
        return img.content
