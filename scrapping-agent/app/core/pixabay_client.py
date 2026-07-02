"""
Pixabay API クライアント（pexels_clientと同一インターフェース）。
https://pixabay.com/api/docs/
"""
import os

import httpx

API_BASE = "https://pixabay.com/api"


def _key() -> str:
    key = os.getenv("PIXABAY_API_KEY", "")
    if not key:
        raise RuntimeError("PIXABAY_API_KEY is not configured")
    return key


def _pick_video_file(hit: dict) -> dict | None:
    """videos辞書からHD以下で最大解像度を選ぶ。"""
    sizes = [
        v for v in hit.get("videos", {}).values()
        if v.get("url") and (v.get("height") or 0) <= 1080
    ]
    if not sizes:
        sizes = [v for v in hit.get("videos", {}).values() if v.get("url")]
    if not sizes:
        return None
    return max(sizes, key=lambda v: v.get("height") or 0)


async def search_videos(query: str, per_page: int = 6) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(
            f"{API_BASE}/videos/",
            params={"key": _key(), "q": query, "per_page": max(per_page, 3)},
        )
    res.raise_for_status()
    results = []
    for hit in res.json().get("hits", [])[:per_page]:
        f = _pick_video_file(hit)
        if f is None:
            continue
        results.append({
            "candidate_id": f"pixabay_video_{hit['id']}",
            "media_type": "video",
            "source": "pixabay",
            "query": query,
            "original_url": hit.get("pageURL", ""),
            "download_url": f["url"],
            "thumbnail_url": f.get("thumbnail", ""),
            "duration_sec": float(hit.get("duration") or 0),
            "resolution": f"{f.get('width')}x{f.get('height')}",
            "photographer": hit.get("user", ""),
            "license": "pixabay-content-license",
        })
    return results


async def search_photos(query: str, per_page: int = 6) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(
            f"{API_BASE}/",
            params={"key": _key(), "q": query, "per_page": max(per_page, 3)},
        )
    res.raise_for_status()
    results = []
    for hit in res.json().get("hits", [])[:per_page]:
        results.append({
            "candidate_id": f"pixabay_photo_{hit['id']}",
            "media_type": "photo",
            "source": "pixabay",
            "query": query,
            "original_url": hit.get("pageURL", ""),
            "download_url": hit.get("largeImageURL") or hit.get("webformatURL", ""),
            "thumbnail_url": hit.get("webformatURL", ""),
            "duration_sec": 0.0,
            "resolution": f"{hit.get('imageWidth')}x{hit.get('imageHeight')}",
            "photographer": hit.get("user", ""),
            "license": "pixabay-content-license",
        })
    return results
