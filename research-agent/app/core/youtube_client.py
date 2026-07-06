"""
YouTube Data API v3 の薄いラッパー。

- クォータ台帳（太平洋時間0時リセット）で日次予算を超えないよう自衛する。
- レスポンスキャッシュ（TTL付き）で同一クエリの再実行を無料化する。
- APIキーは環境変数 YOUTUBE_DATA_API_KEY（ルート .env 既存）。

呼び出し側（seo_optimizer）はここを叩くだけで、クォータ消費とキャッシュを意識しなくてよい。
"""
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://www.googleapis.com/youtube/v3"
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
CACHE_DIR = SHARED_DIR / "youtube_cache"
RESPONSES_DIR = CACHE_DIR / "responses"
LEDGER_PATH = CACHE_DIR / "quota_ledger.json"

REGION = os.getenv("YOUTUBE_REGION", "JP")
DAILY_QUOTA_BUDGET = int(os.getenv("YOUTUBE_DAILY_QUOTA_BUDGET", "8000"))
CACHE_TTL_HOURS = int(os.getenv("YOUTUBE_CACHE_TTL_HOURS", "72"))

# エンドポイントごとのクォータコスト（YouTube Data API v3 公式コスト表に準拠）
_QUOTA_COST = {
    "search": 100,
    "videos": 1,
    "channels": 1,
    "commentThreads": 1,
}


class QuotaBudgetExceeded(Exception):
    """日次クォータ予算を超えるためリクエストを送らなかった時に送出する。"""


def configured() -> bool:
    return bool(os.getenv("YOUTUBE_DATA_API_KEY"))


def _api_key() -> str:
    key = os.getenv("YOUTUBE_DATA_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_DATA_API_KEY が未設定です")
    return key


def _ensure_dirs() -> None:
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)


def _today_pt() -> str:
    return datetime.now(PACIFIC_TZ).strftime("%Y-%m-%d")


def _atomic_write(path: Path, data: dict) -> None:
    """一時ファイル→renameで書き込みを安全にする。"""
    _ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_ledger() -> dict:
    if LEDGER_PATH.exists():
        try:
            # utf-8-sig: Windows側で手編集されBOMが付いてもカウントを失わない
            ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            ledger = {}
    else:
        ledger = {}
    today = _today_pt()
    if ledger.get("date_pt") != today:
        ledger = {"date_pt": today, "used": 0}
    return ledger


def quota_status() -> dict:
    """現在のクォータ使用状況。太平洋時間の日付が変わっていれば0扱いで返す。"""
    ledger = _load_ledger()
    used = ledger.get("used", 0)
    return {
        "configured": configured(),
        "date_pt": ledger["date_pt"],
        "used": used,
        "budget": DAILY_QUOTA_BUDGET,
        "remaining": max(DAILY_QUOTA_BUDGET - used, 0),
    }


def _check_and_reserve(cost: int) -> None:
    """呼び出し前の予算チェック。超えるなら例外、大丈夫なら記帳する。"""
    ledger = _load_ledger()
    if ledger.get("used", 0) + cost > DAILY_QUOTA_BUDGET:
        raise QuotaBudgetExceeded(
            f"日次クォータ予算を超えます（使用済み{ledger.get('used', 0)} + 今回{cost} > 予算{DAILY_QUOTA_BUDGET}）"
        )
    ledger["used"] = ledger.get("used", 0) + cost
    _atomic_write(LEDGER_PATH, ledger)


def _cache_key(endpoint: str, params: dict) -> str:
    """エンドポイント名＋ソート済みパラメータのsha1。APIキーは含めない。"""
    safe_params = {k: v for k, v in params.items() if k != "key"}
    raw = endpoint + "|" + json.dumps(safe_params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_path(endpoint: str, params: dict) -> Path:
    return RESPONSES_DIR / f"{_cache_key(endpoint, params)}.json"


def _read_cache(endpoint: str, params: dict) -> Optional[dict]:
    path = _cache_path(endpoint, params)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fetched_at = cached.get("fetched_at")
    if not fetched_at:
        return None
    try:
        fetched_dt = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    age_hours = (datetime.now(fetched_dt.tzinfo) - fetched_dt).total_seconds() / 3600
    if age_hours > CACHE_TTL_HOURS:
        return None
    return cached.get("data")


def _write_cache(endpoint: str, params: dict, data) -> None:
    path = _cache_path(endpoint, params)
    _atomic_write(path, {
        "fetched_at": datetime.now().astimezone().isoformat(),
        "data": data,
    })


async def _get(endpoint: str, params: dict, force: bool = False) -> dict:
    """キャッシュ→クォータ予約→API呼び出しの共通経路。"""
    if not force:
        cached = _read_cache(endpoint, params)
        if cached is not None:
            return cached

    cost = _QUOTA_COST.get(endpoint, 1)
    _check_and_reserve(cost)

    query = {**params, "key": _api_key()}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(f"{BASE_URL}/{endpoint}", params=query)
        if res.status_code != 200:
            raise RuntimeError(f"YouTube API error {res.status_code} ({endpoint}): {res.text[:300]}")
        data = res.json()

    _write_cache(endpoint, params, data)
    return data


def _batch(ids: list[str], size: int = 50) -> list[list[str]]:
    return [ids[i:i + size] for i in range(0, len(ids), size)]


async def search(
    query: str,
    max_results: int = 25,
    order: str = "relevance",
    region_code: Optional[str] = None,
    relevance_language: str = "ja",
    force: bool = False,
) -> dict:
    """動画検索（part=snippet, type=video）。クォータ100。"""
    params = {
        "part": "snippet",
        "type": "video",
        "q": query,
        "maxResults": max_results,
        "order": order,
        "regionCode": region_code or REGION,
        "relevanceLanguage": relevance_language,
    }
    return await _get("search", params, force=force)


async def videos(video_ids: list[str], force: bool = False) -> list[dict]:
    """動画詳細（part=snippet,statistics）。50件ずつバッチ、1バッチ=クォータ1。"""
    items: list[dict] = []
    for chunk in _batch(video_ids):
        if not chunk:
            continue
        params = {"part": "snippet,statistics", "id": ",".join(chunk)}
        data = await _get("videos", params, force=force)
        items.extend(data.get("items", []))
    return items


async def channels(channel_ids: list[str], force: bool = False) -> list[dict]:
    """チャンネル詳細（part=snippet,statistics）。50件ずつバッチ、1バッチ=クォータ1。"""
    items: list[dict] = []
    unique_ids = list(dict.fromkeys(channel_ids))  # 順序を保ちつつ重複除去
    for chunk in _batch(unique_ids):
        if not chunk:
            continue
        params = {"part": "snippet,statistics", "id": ",".join(chunk)}
        data = await _get("channels", params, force=force)
        items.extend(data.get("items", []))
    return items


async def comment_threads(video_id: str, max_results: int = 100, force: bool = False) -> list[dict]:
    """コメントスレッド（part=snippet, order=relevance）。クォータ1。
    コメント無効の動画（403 commentsDisabled）は空リストで返す（例外にしない）。
    """
    params = {
        "part": "snippet",
        "videoId": video_id,
        "order": "relevance",
        "maxResults": max_results,
    }
    if not force:
        cached = _read_cache("commentThreads", params)
        if cached is not None:
            return cached.get("items", [])

    cost = _QUOTA_COST.get("commentThreads", 1)
    _check_and_reserve(cost)

    query = {**params, "key": _api_key()}
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await client.get(f"{BASE_URL}/commentThreads", params=query)
        if res.status_code == 403:
            try:
                reason = res.json().get("error", {}).get("errors", [{}])[0].get("reason", "")
            except (ValueError, KeyError, IndexError):
                reason = ""
            if reason == "commentsDisabled":
                empty = {"items": []}
                _write_cache("commentThreads", params, empty)
                return []
            raise RuntimeError(f"YouTube API error 403 (commentThreads): {res.text[:300]}")
        if res.status_code != 200:
            raise RuntimeError(f"YouTube API error {res.status_code} (commentThreads): {res.text[:300]}")
        data = res.json()

    _write_cache("commentThreads", params, data)
    return data.get("items", [])
