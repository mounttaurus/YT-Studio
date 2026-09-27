"""
Veo（Google Gemini API）動画生成クライアント — 自由生成スタジオの動画（t2v / i2v）。

grok_video_client と同じ形（validate / estimate_usd / submit / poll / download）で並べる。
鍵は GEMINI_API_KEY（NanoBanana と共通・課金有効化済みが前提）。モデル名は compose の VEO_VIDEO_MODEL。

非同期ジョブ方式:
- 投入: POST v1beta/models/{model}:predictLongRunning → {"name": "models/.../operations/..."}
- 状態: GET  v1beta/{name} → done:true で response.generateVideoResponse.generatedSamples[0].video.uri
  （DLにも x-goog-api-key ヘッダが要る。サーバ保存は2日）

実測で公式ドキュメントと違った点（memory/veo-api-actual-spec.md）:
- 画像は {"bytesBase64Encoded", "mimeType"}（inlineData は400）
- Lite は negativePrompt 非対応 → 送らない（禁止事項はプロンプト本文に書く）
- durationSeconds は数値
- 応答に費用が入らない → 目安は表示価格。実費は Google の請求画面でしか分からない
- 音声は常に生成される（料金込み）
"""
import base64
import os
from typing import Optional

import httpx

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
VIDEO_MODEL = os.getenv("VEO_VIDEO_MODEL", "veo-3.1-lite-generate-preview")
BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_SEC = 4

DURATIONS = {4, 6, 8}
ASPECTS = {"16:9", "9:16"}
RESOLUTIONS = {
    "veo-3.1-lite-generate-preview": {"720p", "1080p"},
    "veo-3.1-fast-generate-preview": {"720p", "1080p", "4k"},
    "veo-3.1-generate-preview": {"720p", "1080p", "4k"},
}
# 表示価格（USD/秒・音声込み・2026-09 公式 pricing）。応答に費用が無いので実測で置き換えられない。
_LIST_PRICE = {
    "veo-3.1-lite-generate-preview": {"720p": 0.05, "1080p": 0.08},
    "veo-3.1-fast-generate-preview": {"720p": 0.10, "1080p": 0.12, "4k": 0.30},
    "veo-3.1-generate-preview": {"720p": 0.40, "1080p": 0.40, "4k": 0.60},
}


def is_configured() -> bool:
    return bool(GEMINI_API_KEY)


def validate(model: str, duration: int, resolution: str, aspect: str) -> Optional[str]:
    if model not in RESOLUTIONS:
        return f"unknown video model: {model}（{', '.join(RESOLUTIONS)}）"
    if duration not in DURATIONS:
        return "Veo の duration は 4 / 6 / 8 秒のいずれか"
    if resolution not in RESOLUTIONS[model]:
        return f"resolution {resolution} is not supported by {model}（{', '.join(sorted(RESOLUTIONS[model]))}）"
    if resolution in ("1080p", "4k") and duration != 8:
        return f"Veo の {resolution} は duration=8 が必須"
    if aspect not in ASPECTS:
        return "Veo の aspect は 16:9 / 9:16 のみ"
    return None


def estimate_usd(model: str, duration: int, resolution: str) -> dict:
    per_sec = _LIST_PRICE.get(model, {}).get(resolution, 0.40)
    return {"usd": round(per_sec * duration, 2), "usd_per_sec": per_sec, "measured": False,
            "note": "表示価格。Veoは応答に費用が入らないため実費はGoogleの請求画面で確認"}


def _headers() -> dict:
    return {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}


def _image_part(data: bytes, mime: str) -> dict:
    return {"bytesBase64Encoded": base64.b64encode(data).decode(), "mimeType": mime}


async def submit(prompt: str, image: Optional[bytes] = None, image_mime: str = "image/png",
                 model: str = VIDEO_MODEL, duration: int = DEFAULT_SEC, resolution: str = "720p",
                 aspect: str = "16:9", audio: bool = True,
                 last_frame: Optional[bytes] = None, last_frame_mime: str = "image/png") -> str:
    """生成ジョブを投入して operation 名を返す。audio は無視（Veo は常に音声付き）。

    last_frame を渡すと「最初の絵(image)→最後の絵(last_frame)」をつなぐ補間動画になる。
    """
    inst: dict = {"prompt": prompt}
    if image:
        inst["image"] = _image_part(image, image_mime)
    if last_frame:
        inst["lastFrame"] = _image_part(last_frame, last_frame_mime)
    params = {"aspectRatio": aspect, "resolution": resolution, "durationSeconds": duration}
    async with httpx.AsyncClient(timeout=120) as client:
        res = await client.post(f"{BASE}/models/{model}:predictLongRunning",
                                json={"instances": [inst], "parameters": params}, headers=_headers())
    if res.status_code >= 400:
        raise RuntimeError(f"Veo submit failed ({res.status_code}): {res.text[:400]}")
    name = res.json().get("name")
    if not name:
        raise RuntimeError(f"Veo submit returned no operation name: {res.text[:300]}")
    return name


async def poll(request_id: str) -> dict:
    """状態を1回だけ問い合わせる。grok_video_client.poll と同じ形の dict を返す。"""
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.get(f"{BASE}/{request_id}", headers=_headers())
    if res.status_code >= 400:
        return {"status": "error", "raw_error": f"{res.status_code}: {res.text[:300]}"}
    op = res.json()
    if not op.get("done"):
        return {"status": "pending"}
    if op.get("error"):
        return {"status": "failed", "raw_error": op["error"]}
    gvr = (op.get("response") or {}).get("generateVideoResponse") or {}
    samples = gvr.get("generatedSamples") or []
    uri = ((samples[0] if samples else {}).get("video") or {}).get("uri")
    if not uri:
        # 安全フィルタで弾かれた時は samples が空で理由だけ返る
        reason = gvr.get("raiMediaFilteredReasons") or gvr or op.get("response")
        return {"status": "failed", "raw_error": f"no video (filtered?): {str(reason)[:400]}"}
    return {"status": "done", "url": uri, "duration": None, "cost_usd": None, "raw_error": None}


async def download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        res = await client.get(url, headers={"x-goog-api-key": GEMINI_API_KEY})
        res.raise_for_status()
        return res.content
