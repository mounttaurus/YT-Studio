"""
Vecteezy API クライアント（pexels_clientと同一インターフェース）。
仕様: https://www.vecteezy.com/api-docs/index.html（swagger: /api-docs/api/v2/swagger.json）

他ソースとの違い:
- V1 APIはこのアカウントでは使用不可（"V1 API usage is not permitted"）。V2を使用。
- V2はパスに account_id を含む: /v2/{account_id}/resources 等（認証は Authorization: Bearer に加え必須）
- 検索結果に直接のダウンロードURLが無い。確定時に GET /v2/{account_id}/resources/{id}/download を
  呼んで署名付きURLを取得する2段階方式（resolve_download_url）
- ★ダウンロードはクォータ消費（account/info の download.call_limit、無料枠は月500）。
  候補のサムネ表示はthumbnail（無料）なので、コストが発生するのは確定時のみ
"""
import os

import httpx

API_BASE = "https://api.vecteezy.com"


def _account_id() -> str:
    uid = os.getenv("VECTEEZY_USER_ID", "")
    if not uid:
        raise RuntimeError("VECTEEZY_USER_ID is not configured")
    return uid


def _headers() -> dict:
    key = os.getenv("VECTEEZY_API_KEY", "")
    if not key:
        raise RuntimeError("VECTEEZY_API_KEY is not configured")
    return {"Authorization": f"Bearer {key}"}


def _pick_file_type(res: dict, media: str) -> str:
    types = [
        t.get("extension", "").lower()
        for t in (res.get("file_metadata", {}).get("available_file_types") or [])
    ]
    prefer = ["mp4", "mov"] if media == "video" else ["jpg", "jpeg", "png"]
    for p in prefer:
        if p in types:
            return p
    return types[0] if types else ("mp4" if media == "video" else "jpg")


def _to_candidate(res: dict, media: str, query: str) -> dict:
    sizes = res.get("file_metadata", {}).get("available_download_sizes") or []
    original = next((s for s in sizes if s.get("id") == "original"), sizes[0] if sizes else {})
    resolution = f"{original['width']}x{original['height']}" if original.get("width") else ""
    return {
        "candidate_id": f"vecteezy_{media}_{res['id']}",
        "media_type": media,
        "source": "vecteezy",
        "query": query,
        "original_url": f"https://www.vecteezy.com/resources/{res['id']}",
        "download_url": "",   # 確定時にresolve_download_urlで取得（クォータ保護）
        "thumbnail_url": res.get("thumbnail_url") or "",
        "duration_sec": 0.0,  # 検索レスポンスに尺情報なし（静止画換算で扱われる）
        "resolution": resolution,
        "photographer": res.get("title") or "",
        "license": "vecteezy-license",
        # resolve用の追加情報
        "vz_id": res["id"],
        "vz_file_type": _pick_file_type(res, media),
    }


async def _search(query: str, content_type: str, media: str, per_page: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(
            f"{API_BASE}/v2/{_account_id()}/resources",
            headers=_headers(),
            params={
                "term": query,
                "content_type": content_type,
                "per_page": max(per_page, 3),
                "sort_by": "relevance",
                "license_type": "commercial",
            },
        )
    res.raise_for_status()
    out = []
    for r in res.json().get("resources", [])[:per_page]:
        out.append(_to_candidate(r, media, query))
    return out


async def search_videos(query: str, per_page: int = 6) -> list[dict]:
    return await _search(query, "video", "video", per_page)


async def search_photos(query: str, per_page: int = 6) -> list[dict]:
    return await _search(query, "photo", "photo", per_page)


async def resolve_download_url(cand: dict) -> str:
    """確定時に呼ぶ: ダウンロード用の署名付きURLを取得する（クォータを消費する）。"""
    rid = cand.get("vz_id") or cand["candidate_id"].rsplit("_", 1)[-1]
    # file_size指定はリサイズ扱いとなり無料アカウントでは422になるため付けない
    params = {}
    if cand.get("vz_file_type"):
        params["file_type"] = cand["vz_file_type"]
    async with httpx.AsyncClient(timeout=60.0) as client:
        res = await client.get(
            f"{API_BASE}/v2/{_account_id()}/resources/{rid}/download",
            headers=_headers(), params=params,
        )
        res.raise_for_status()
        data = res.json()
        url = data.get("url") or data.get("inline_url") or ""
        if not url:
            raise RuntimeError(f"vecteezy download url not available: {str(data)[:200]}")
        return url


async def account_info() -> dict:
    """クォータ確認用（残ダウンロード数等）。"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.get(
            f"{API_BASE}/v2/{_account_id()}/account/info", headers=_headers(),
        )
        res.raise_for_status()
        return res.json()
