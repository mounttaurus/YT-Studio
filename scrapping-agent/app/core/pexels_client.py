"""
Pexels API クライアント（動画＋写真検索、ファイルダウンロード）。
https://www.pexels.com/api/documentation/

ソース追加（Pixabay等）の際は、このモジュールと同じ
search_videos/search_photos/download の形で別モジュールを作り、
routes側でsourceパラメータにより切り替える想定。
"""
import os
from pathlib import Path

import httpx

API_BASE = "https://api.pexels.com"


def _headers() -> dict:
    key = os.getenv("PEXELS_API_KEY", "")
    if not key:
        raise RuntimeError("PEXELS_API_KEY is not configured")
    return {"Authorization": key}


def _pick_video_file(video: dict) -> dict | None:
    """video_filesからHD以下で最大解像度のmp4を選ぶ（4Kは容量過大のため避ける）。"""
    files = [
        f for f in video.get("video_files", [])
        if f.get("file_type") == "video/mp4" and (f.get("height") or 0) <= 1080
    ]
    if not files:
        files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"]
    if not files:
        return None
    return max(files, key=lambda f: f.get("height") or 0)


async def search_videos(query: str, per_page: int = 6) -> list[dict]:
    """候補リストを返す。要素は footage_draft.json の candidates 形式。"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(
            f"{API_BASE}/videos/search",
            headers=_headers(),
            params={"query": query, "per_page": per_page},
        )
    res.raise_for_status()
    results = []
    for v in res.json().get("videos", []):
        f = _pick_video_file(v)
        if f is None:
            continue
        results.append({
            "candidate_id": f"pexels_video_{v['id']}",
            "media_type": "video",
            "source": "pexels",
            "query": query,
            "original_url": v.get("url", ""),
            "download_url": f.get("link", ""),
            "thumbnail_url": v.get("image", ""),
            "duration_sec": float(v.get("duration") or 0),
            "resolution": f"{f.get('width')}x{f.get('height')}",
            "photographer": v.get("user", {}).get("name", ""),
            "license": "pexels-free",
        })
    return results


async def search_photos(query: str, per_page: int = 6) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(
            f"{API_BASE}/v1/search",
            headers=_headers(),
            params={"query": query, "per_page": per_page},
        )
    res.raise_for_status()
    results = []
    for p in res.json().get("photos", []):
        src = p.get("src", {})
        results.append({
            "candidate_id": f"pexels_photo_{p['id']}",
            "media_type": "photo",
            "source": "pexels",
            "query": query,
            "original_url": p.get("url", ""),
            "download_url": src.get("large2x") or src.get("original", ""),
            "thumbnail_url": src.get("medium", ""),
            "duration_sec": 0.0,
            "resolution": f"{p.get('width')}x{p.get('height')}",
            "photographer": p.get("photographer", ""),
            "license": "pexels-free",
        })
    return results


async def download(url: str, dest: Path) -> None:
    """素材ファイルをdestへダウンロードする。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
        async with client.stream("GET", url) as res:
            res.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in res.aiter_bytes(1024 * 256):
                    f.write(chunk)
