"""
モデルファイル（checkpoint / LoRA / VAE）のURLダウンローダー。

Civitai / HuggingFace の直リンクを受け取り、imagegen-agent/models/ 配下へ
ストリーミング保存する。GB級ファイルのブラウザ経由アップロードを避けるための仕組み。
ダウンロード状態はプロセス内dictで管理（--reload時は消えるが実害なし、.partは残らない）。

ComfyUIはフォルダのmtimeでファイル一覧キャッシュを更新するため、
保存完了後は追加操作なしで GET /imagegen/models に反映される。
"""
import asyncio
import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import httpx

MODELS_DIR = Path(os.getenv("MODELS_DIR", "/imagegen_models"))
KIND_DIRS = {"checkpoint": "checkpoints", "lora": "loras", "vae": "vae", "controlnet": "controlnet"}

_downloads: dict[str, dict] = {}

_FNAME_RE = re.compile(r"^[\w.\-]+$")


def status_all() -> list[dict]:
    return sorted(_downloads.values(), key=lambda d: d["started_at"], reverse=True)


def filename_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    return urllib.parse.unquote(path.rstrip("/").split("/")[-1])


def start(url: str, kind: str, filename: str | None = None) -> dict:
    """ダウンロードをバックグラウンドで開始し、状態エントリを返す。"""
    if kind not in KIND_DIRS:
        raise ValueError(f"kind must be one of {list(KIND_DIRS)}")
    name = (filename or filename_from_url(url)).strip()
    if not _FNAME_RE.match(name):
        raise ValueError(f"invalid filename: {name!r} (use filename param)")

    dest_dir = MODELS_DIR / KIND_DIRS[kind]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name

    key = f"{KIND_DIRS[kind]}/{name}"
    existing = _downloads.get(key)
    if existing and existing["status"] == "downloading":
        raise ValueError(f"already downloading: {key}")
    if dest.exists():
        raise ValueError(f"file already exists: {key}")

    entry = {
        "key": key,
        "kind": kind,
        "filename": name,
        "url": url,
        "status": "downloading",
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "error": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _downloads[key] = entry
    asyncio.get_event_loop().create_task(_run(url, dest, entry))
    return entry


async def _run(url: str, dest: Path, entry: dict) -> None:
    tmp = dest.with_name(dest.name + ".part")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True) as client:
            async with client.stream("GET", url) as res:
                res.raise_for_status()
                entry["total_bytes"] = int(res.headers.get("content-length") or 0)
                with open(tmp, "wb") as f:
                    async for chunk in res.aiter_bytes(512 * 1024):
                        f.write(chunk)
                        entry["downloaded_bytes"] += len(chunk)
        tmp.replace(dest)
        entry["status"] = "done"
    except Exception as e:
        entry["status"] = "error"
        entry["error"] = str(e)[:300]
        tmp.unlink(missing_ok=True)


def is_mounted() -> bool:
    return MODELS_DIR.exists()
