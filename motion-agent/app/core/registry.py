"""ギミックの台帳（remotion/src/gimmicks/registry.json）の読み込み。

ID は「カタログ ID＋名前」（B3_decode など）。短い別名（decode など）でも呼べるが、保存する時は
正式な ID に直す（`canonical`）。何が実装済みかの本籍は台帳（opening_gimmicks がそのまま返す）。
"""
from __future__ import annotations

import json
from functools import lru_cache

from app.core.config import REMOTION_DIR

REGISTRY_FILE = REMOTION_DIR / "src" / "gimmicks" / "registry.json"


@lru_cache(maxsize=1)
def _load(mtime: float) -> dict:
    return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))


def load() -> dict:
    return _load(REGISTRY_FILE.stat().st_mtime)


def canonical(gid: str | None) -> str | None:
    """別名を正式な ID に直す。知らない名前はそのまま返す（検査が指摘する）。"""
    if not isinstance(gid, str):
        return gid
    return load()["aliases"].get(gid, gid)


def gimmick(gid: str) -> dict | None:
    gid = canonical(gid)
    return load()["gimmicks"].get(gid) if isinstance(gid, str) else None


def enter(eid: str | None) -> dict | None:
    eid = canonical(eid) if eid else "cut"
    return load()["enters"].get(eid) if isinstance(eid, str) else None
