"""
Grok（xAI）動画生成クライアント — 自由生成スタジオの動画（t2v / i2v）。

xAI Grok Imagine Video。非同期ジョブ方式:
- 投入: POST https://api.x.ai/v1/videos/generations → {"request_id"}
- 状態: GET  https://api.x.ai/v1/videos/{request_id} → status: pending/done/failed/expired
  done なら video.url（一時URL＝すぐDLする）と usage.cost_in_usd_ticks（1 tick = 1e-10 USD）。

鍵は GROK_API_KEY（画像と共通）。モデル名は compose の environment（GROK_VIDEO_MODEL）。
費用の目安は表示価格ではなく実測値を基準にする（表示 $0.08/秒 に対し 720p 5秒の実費は $0.71＝
約1.8倍。memory/grok-video-actual-cost.md）。未実測の組み合わせは表示価格×1.8で見積もる。

将来 Veo 等を足す時は同じ形（estimate_usd / submit / poll / download）のモジュールを並べる。
"""
import base64
import os
from typing import Optional

import httpx

GROK_API_KEY = os.getenv("GROK_API_KEY", "")
VIDEO_MODEL = os.getenv("GROK_VIDEO_MODEL", "grok-imagine-video-1.5")
GEN_URL = "https://api.x.ai/v1/videos/generations"
STATUS_URL = "https://api.x.ai/v1/videos/{request_id}"

USD_PER_TICK = 1e-10
MIN_SEC, MAX_SEC = 1, 15
DEFAULT_SEC = 5
ASPECTS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
RESOLUTIONS = {
    "grok-imagine-video-1.5": {"480p", "720p", "1080p"},
    "grok-imagine-video": {"480p", "720p"},
}

# 秒単価（USD）。measured=True は実費（応答の ticks）から出した値。
# False は表示価格×1.8（実測の乖離率）での推定。新しい組み合わせを使ったら実測で置き換える。
_LIST_PRICE = {"grok-imagine-video-1.5": 0.08, "grok-imagine-video": 0.05}
_MEASURED = {
    ("grok-imagine-video-1.5", "720p"): 0.142,  # 2026-09-27 i2v 5秒×2本 = $0.71/本
}
_UNMEASURED_FACTOR = 1.8
_RES_FACTOR = {"480p": 0.5, "720p": 1.0, "1080p": 2.25}  # 画素数比。未実測時のみ使う


def is_configured() -> bool:
    return bool(GROK_API_KEY)


def validate(model: str, duration: int, resolution: str, aspect: str) -> Optional[str]:
    """パラメータが仕様外ならエラー文を返す（API課金前に弾く）。"""
    if model not in RESOLUTIONS:
        return f"unknown video model: {model}（{', '.join(RESOLUTIONS)}）"
    if not MIN_SEC <= duration <= MAX_SEC:
        return f"duration must be {MIN_SEC}-{MAX_SEC} sec"
    if resolution not in RESOLUTIONS[model]:
        return f"resolution {resolution} is not supported by {model}（{', '.join(sorted(RESOLUTIONS[model]))}）"
    if aspect not in ASPECTS:
        return f"aspect must be one of {', '.join(sorted(ASPECTS))}"
    return None


def estimate_usd(model: str, duration: int, resolution: str) -> dict:
    """費用の目安。{usd, usd_per_sec, measured} を返す。"""
    key = (model, resolution)
    if key in _MEASURED:
        per_sec, measured = _MEASURED[key], True
    else:
        per_sec = _LIST_PRICE.get(model, 0.08) * _UNMEASURED_FACTOR * _RES_FACTOR.get(resolution, 1.0)
        # 720p の実測値があるモデルは、そこから解像度比で推定する方が近い
        base = _MEASURED.get((model, "720p"))
        if base:
            per_sec = base * _RES_FACTOR.get(resolution, 1.0)
        measured = False
    return {"usd": round(per_sec * duration, 2), "usd_per_sec": round(per_sec, 4), "measured": measured}


def _headers() -> dict:
    return {"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"}


async def submit(prompt: str, image: Optional[bytes] = None, image_mime: str = "image/png",
                 model: str = VIDEO_MODEL, duration: int = 5, resolution: str = "720p",
                 aspect: str = "16:9", audio: bool = False) -> str:
    """生成ジョブを投入して request_id を返す。image があれば i2v、無ければ t2v。"""
    body = {"model": model, "prompt": prompt, "duration": duration,
            "aspect_ratio": aspect, "resolution": resolution, "generate_audio": audio}
    if image:
        body["image"] = {"url": f"data:{image_mime};base64,{base64.b64encode(image).decode()}"}
    async with httpx.AsyncClient(timeout=120) as client:
        res = await client.post(GEN_URL, json=body, headers=_headers())
        if res.status_code >= 400:
            raise RuntimeError(f"Grok video submit failed ({res.status_code}): {res.text[:400]}")
        rid = res.json().get("request_id")
    if not rid:
        raise RuntimeError(f"Grok video submit returned no request_id: {res.text[:300]}")
    return rid


async def poll(request_id: str) -> dict:
    """状態を1回だけ問い合わせる。{status, url, duration, cost_usd, raw_error} を返す。"""
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.get(STATUS_URL.format(request_id=request_id), headers=_headers())
    if res.status_code >= 400:
        return {"status": "error", "raw_error": f"{res.status_code}: {res.text[:300]}"}
    data = res.json()
    video = data.get("video") or {}
    ticks = (data.get("usage") or {}).get("cost_in_usd_ticks")
    return {
        "status": data.get("status", "unknown"),
        "url": video.get("url"),
        "duration": video.get("duration"),
        "cost_usd": round(ticks * USD_PER_TICK, 4) if ticks is not None else None,
        "raw_error": data.get("error"),
    }


async def download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        res = await client.get(url)
        res.raise_for_status()
        return res.content
