"""「✋ 手直し済み」の判定（読み取り専用・Docs/LINE_WORKBENCH_PLAN.md §6-2・D15）。

記録（`psassist/build_records.json`）を書くのはホスト常駐の host_worker（`psassist/scripts/build_records.py`）。
director はそれを読み、今の PSD の内容が記録の指紋と違う行を「手直し済み」として画面に出すだけ（何も書かない）。
形式は `build_records.py` と同じ。**記録の形を変える時は両方を直す。**

近道: 記録した (size, mtime_ns) と一致すれば「変わっていない」。一致しない時だけ sha256 で確かめる
（コピーや同期ソフトで時刻だけ変わった行を手直しと誤判定しない）。sha256 は (パス, size, mtime_ns) でキャッシュする。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

RECORDS_NAME = "build_records.json"
_sha_cache: dict[tuple[str, int, int], str] = {}


def _sha256(path: Path, size: int, mtime_ns: int) -> str:
    key = (str(path), size, mtime_ns)
    if key not in _sha_cache:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        if len(_sha_cache) > 2000:
            _sha_cache.clear()
        _sha_cache[key] = h.hexdigest()
    return _sha_cache[key]


def load_records(psa_dir: Path) -> dict:
    try:
        doc = json.loads((psa_dir / RECORDS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    recs = doc.get("records") if isinstance(doc, dict) else None
    return recs if isinstance(recs, dict) else {}


def psd_state(psa_dir: Path, records: dict, line_id: str) -> dict:
    """1行のPSDの状態 `{has_psd, edited, built_at}`。記録が無い行は edited=False（判定不能＝手直し扱いしない）。"""
    p = psa_dir / "psd_final" / f"panel_{line_id}.psd"
    try:
        st = p.stat()
    except OSError:
        return {"has_psd": False, "edited": False, "built_at": None}
    rec = records.get(line_id)
    if not rec:
        return {"has_psd": True, "edited": False, "built_at": None}
    edited = False
    if not (st.st_size == rec.get("size") and st.st_mtime_ns == rec.get("mtime_ns")):
        try:
            edited = _sha256(p, st.st_size, st.st_mtime_ns) != rec.get("psd_sha256")
        except OSError:
            edited = False
    return {"has_psd": True, "edited": edited, "built_at": rec.get("built_at")}
