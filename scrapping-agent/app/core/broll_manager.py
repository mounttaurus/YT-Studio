"""Bロールの箱（`episodes/epNN/broll/broll.json`）の読み書き。設計: Docs/BROLL_PLAN.md（B1）・DATA_SCHEMA.md §6e。

不変条件（BROLL_PLAN §2）:
- BR1 行ID（line_id）が鍵。割り当ては行番号ではなく line_id に付く。
- BR2 素材ID（asset_id）は不変。一度付けた ID とファイル名は改名も振り直しもしない。
- BR3 書き手は scrapping-agent だけ。ワークベンチ・素材タブ・motion-agent・MCP は全てその API を通す。
- BR4 入口は `register_asset()` の1つ。アップロード・素材サイト・パスからの取り込み・Remotion／HyperFrames の出力は全てここを通る。
- BR5 消さない。箱から外した素材は `broll/_trash/` へ移す。行から外れた割り当ても消さない（導出で orphan として見せる）。
- BR6 行の操作に後処理を足さない（割り当ては line_id に付いているので、そのまま残る）。
- BR7 割り当てと取り込みは無料。課金する取り込み（素材サイトの DL・AI 生成）は呼ぶ側が明示して走らせる。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from app.core import media_probe
from app.core.panel_library_manager import atomic_write_text

try:                                   # コンテナ（Linux）ではファイルロックも取る。無い環境はプロセス内のロックだけ
    import fcntl
except ImportError:                    # pragma: no cover
    fcntl = None

SCHEMA_VERSION = "1.0.0"
BROLL_DIR = "broll"
DOC_NAME = "broll.json"
TRASH_DIR = "_trash"
PLANS_DIR = "plans"

SOURCE_KINDS = ("pexels", "pixabay", "vecteezy", "wikimedia", "ai", "user", "motion", "claude")
# OPENING_MOTION_PLAN §8 の語彙＋素材サイトの値。空欄は不可
LICENSES = ("own", "pd", "cc0", "cc-by", "cc-by-sa", "licensed", "unknown",
            "pexels-free", "pixabay-content-license", "vecteezy-free", "vecteezy-pro", "ai-generated")
ATTRIBUTION_LICENSES = ("cc-by", "cc-by-sa")
KIND_CODE = {"photo": "img", "video": "vid", "motion": "mot"}
ASSET_ID_RE = re.compile(r"^b_[0-9a-f]{8,12}(?:_[a-z0-9]+(?:-[a-z0-9]+)*)?_(img|vid|mot)$")
LINE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SLUG_MAX = 24


class BrollError(Exception):
    """API がそのまま返せる失敗。status は HTTP の番号。"""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 置き場・排他 ──

def broll_dir(ep_dir: Path) -> Path:
    return ep_dir / BROLL_DIR


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCK_DEPTH: dict[str, int] = {}


@contextlib.contextmanager
def lock(ep_dir: Path):
    """読んで・直して・書くを1話単位で排他にする（書き込みは必ずここを通る）。"""
    key = str(ep_dir.resolve())
    d = broll_dir(ep_dir)
    d.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCKS.setdefault(key, threading.RLock()):
        if fcntl is None or _LOCK_DEPTH.get(key, 0) > 0:
            _LOCK_DEPTH[key] = _LOCK_DEPTH.get(key, 0) + 1
            try:
                yield
            finally:
                _LOCK_DEPTH[key] -= 1
            return
        with open(d / ".broll.lock", "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            _LOCK_DEPTH[key] = 1
            try:
                yield
            finally:
                _LOCK_DEPTH[key] = 0
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _empty(project_id: str, episode: int) -> dict:
    return {"schema_version": SCHEMA_VERSION, "project_id": project_id, "episode": episode, "assets": [],
            "placements": {}, "trashed": []}


def load(ep_dir: Path, project_id: str = "", episode: int = 0) -> dict:
    """箱を読む。無ければ空の箱（書かない）。壊れた JSON は空と見なさず例外にする（上書きで消さない）。"""
    p = broll_dir(ep_dir) / DOC_NAME
    if not p.is_file():
        return _empty(project_id, episode)
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc.setdefault("assets", [])
    doc.setdefault("placements", {})
    doc.setdefault("trashed", [])
    return doc


def exists(ep_dir: Path) -> bool:
    return (broll_dir(ep_dir) / DOC_NAME).is_file()


def save(ep_dir: Path, doc: dict) -> None:
    doc["updated_at"] = _now()
    d = broll_dir(ep_dir)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(d / DOC_NAME, json.dumps(doc, ensure_ascii=False, indent=2))


# ── 台本の行 ──

def script_line_ids(ep_dir: Path) -> list[str] | None:
    """確定台本の行IDの並び（サブ行も含む）。台本が無ければ None。"""
    p = ep_dir / "script.json"
    if not p.is_file():
        return None
    try:
        script = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    # 行のIDは script.json では `id`（tts.json・aroll.json・割り当てでは line_id と呼ぶ）。どちらでも読む
    return [ln.get("line_id") or ln.get("id") for ln in script.get("lines") or [] if ln.get("line_id") or ln.get("id")]


def _line_hint(ep_dir: Path, line_id: str) -> str:
    """台本から外れた行の、当時の本文の冒頭（tts.json に退避された行から）。分からなければ空。"""
    p = ep_dir / "tts.json"
    if not p.is_file():
        return ""
    try:
        tts = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return ""
    for key in ("orphaned_audio_files", "audio_files"):
        for e in tts.get(key) or []:
            if e.get("line_id") == line_id and e.get("text"):
                return str(e["text"])[:30]
    return ""


# ── ID ──

def make_slug(name: str) -> str:
    """元の名前から語を作る（英数字とハイフンだけ・小文字）。日本語だけなら空（ID から省く）。"""
    stem = Path(name or "").stem
    s = unicodedata.normalize("NFKC", stem).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"(?:^|-)(?:img|image|photo|dsc|pxl|pexels|pixabay|download|untitled)(?:-?\d+)?(?=-|$)", "", s).strip("-")
    s = re.sub(r"\d+$", "", s).strip("-")           # 数字で終わらせない（ID の途中でも連番に見えにくくする）
    return s[:_SLUG_MAX].strip("-")


def make_asset_id(sha256: str, slug: str, kind: str, sha_len: int = 8) -> str:
    base = f"b_{sha256[:sha_len]}"
    return f"{base}_{slug}_{KIND_CODE[kind]}" if slug else f"{base}_{KIND_CODE[kind]}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 読む ──

def find_asset(doc: dict, asset_id: str) -> dict | None:
    return next((a for a in doc["assets"] if a["asset_id"] == asset_id), None)


def used_by(doc: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line_id, items in doc["placements"].items():
        for it in items:
            out.setdefault(it["asset_id"], []).append(line_id)
    return out


def view(ep_dir: Path, project_id: str = "", episode: int = 0) -> dict:
    """GET の形: 箱・割り当て・導出（使っている行・行から外れた割り当て・数）。"""
    doc = load(ep_dir, project_id, episode)
    lines = script_line_ids(ep_dir)
    line_set = set(lines) if lines is not None else None
    uses = used_by(doc)
    placements, orphans = {}, []
    for line_id, items in doc["placements"].items():
        orphan = line_set is not None and line_id not in line_set
        for it in items:
            if orphan:
                orphans.append({"line_id": line_id, "asset_id": it["asset_id"], "note": it.get("note", ""),
                                "line_text_hint": _line_hint(ep_dir, line_id)})
        if items:
            placements[line_id] = items
    assets = []
    for a in doc["assets"]:
        lines_used = uses.get(a["asset_id"], [])
        assets.append({**a, "used_by": lines_used, "unassigned": not lines_used})
    return {
        "schema_version": doc.get("schema_version", SCHEMA_VERSION), "project_id": project_id or doc.get("project_id"),
        "episode": episode or doc.get("episode"), "exists": exists(ep_dir),
        "assets": assets, "placements": placements, "orphans": orphans, "trashed": doc.get("trashed", []),
        "summary": {
            "assets": len(doc["assets"]),
            "lines_with_broll": len([1 for k, v in doc["placements"].items() if v]),
            "unassigned": len([1 for a in doc["assets"] if a["asset_id"] not in uses]),
            "orphaned": len(orphans),
            "license_unknown": len([1 for a in doc["assets"] if a.get("license") == "unknown"]),
        },
    }


# ── 取り込み（入口はこの1つ・BR4） ──

def _validate_meta(license_: str, source: dict, credit: str) -> None:
    if not isinstance(license_, str) or license_ not in LICENSES:
        raise BrollError("bad_license", f"license は {list(LICENSES)} のどれか（空欄は不可）: {license_!r}")
    if not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS:
        raise BrollError("bad_source", f"source.kind は {list(SOURCE_KINDS)} のどれか: {(source or {}).get('kind')!r}")
    if license_ in ATTRIBUTION_LICENSES and not (credit or source.get("author")):
        raise BrollError("credit_required", f"{license_} はクレジット（credit か source.author）が要る")


def register_asset(ep_dir: Path, project_id: str, episode: int, src: Path, *, original_name: str | None = None,
                   source: dict, license: str, credit: str = "", tags: dict | None = None, added_by: str = "path",
                   line_id: str | None = None, slug: str | None = None, motion: dict | None = None,
                   derived_from: list[str] | None = None, replaces: str | None = None, poster_sec: float | None = None,
                   note: str = "", extra: dict | None = None) -> dict:
    """ファイルを箱へ入れる。返り値: {asset, duplicate, assigned}。
    同じ内容（sha256）が箱にあれば、新しく作らず既存の asset を返す（line_id があればその行に当てる）。"""
    _validate_meta(license, source, credit)
    if not src.is_file():
        raise BrollError("no_file", f"ファイルが無い: {src}", 404)
    info = media_probe.probe(src)
    if info["media_type"] is None:
        raise BrollError("unsupported", info.get("error") or "画像（png・jpg・webp）か動画（mp4・mov・webm）の中身ではない")
    media_type, ext = info["media_type"], info["ext"]
    if line_id is not None:
        _check_line(ep_dir, line_id)
    digest = _sha256(src)
    name = original_name or src.name
    with lock(ep_dir):
        doc = load(ep_dir, project_id, episode)
        for key in derived_from or []:
            if find_asset(doc, key) is None:
                raise BrollError("unknown_asset", f"derived_from の素材が箱に無い: {key}", 404)
        if replaces is not None and find_asset(doc, replaces) is None:
            raise BrollError("unknown_asset", f"replaces の素材が箱に無い: {replaces}", 404)
        dup = next((a for a in doc["assets"] if a.get("sha256") == digest), None)
        if dup is not None:
            if replaces and replaces != dup["asset_id"]:
                # 描き直したら前の版と同じ中身に戻った: 割り当ては戻した素材へ（今の素材は外れる）。戻した方の「差し替えられた」印は外す
                _replace(doc, replaces, dup["asset_id"])
                dup.pop("replaced_by", None)
            assigned = _assign(doc, ep_dir, line_id, dup["asset_id"], None, note) if line_id else None
            save(ep_dir, doc)
            return {"asset": dup, "duplicate": True, "assigned": assigned}
        kind = "motion" if source.get("kind") == "motion" else media_type
        use_slug = make_slug(slug if slug is not None else name) if (slug or name) else ""
        asset_id, n = make_asset_id(digest, use_slug, kind), 8
        taken = {a["asset_id"] for a in doc["assets"]} | {t["asset_id"] for t in doc["trashed"]}
        while asset_id in taken and n < 12:               # 別の中身で頭8桁が重なった時だけ伸ばす（極めてまれ）
            n += 2
            asset_id = make_asset_id(digest, use_slug, kind, n)
        if asset_id in taken:
            raise BrollError("id_collision", f"asset_id を作れない: {asset_id}", 409)
        dst = broll_dir(ep_dir) / f"{asset_id}{ext}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
        asset = {
            "asset_id": asset_id, "file": f"{BROLL_DIR}/{asset_id}{ext}", "media_type": media_type,
            "duration_sec": info["duration_sec"], "resolution": info["resolution"], "sha256": digest,
            "source": {**source, "original_name": name}, "license": license, "credit": credit,
            "tags": {"entities": [], "kind": [], "era": "", "confirmed": False, **(tags or {})},
            "motion": None, "added_at": _now(), "added_by": added_by,
        }
        if note:
            asset["note"] = note
        if poster_sec is not None:
            asset["poster_sec"] = float(poster_sec)
        if derived_from:
            asset["derived_from"] = list(derived_from)
        if replaces:
            asset["replaces"] = replaces
        if extra:
            asset.update({k: v for k, v in extra.items() if k not in asset})
        if motion:
            asset["motion"] = _store_plan(ep_dir, asset_id, motion)
        doc["assets"].append(asset)
        assigned = None
        if replaces:
            _replace(doc, replaces, asset_id)
        if line_id:
            assigned = _assign(doc, ep_dir, line_id, asset_id, None, "")
        save(ep_dir, doc)
        return {"asset": asset, "duplicate": False, "assigned": assigned}


def _store_plan(ep_dir: Path, asset_id: str, motion: dict) -> dict:
    """作り直せる元（Remotion のプラン・HyperFrames のコンポジションの写し）を broll/plans/ へ置く（§8-2）。"""
    plans = broll_dir(ep_dir) / PLANS_DIR
    plans.mkdir(parents=True, exist_ok=True)
    engine = motion.get("engine") or "unknown"
    out: dict = {"engine": engine}
    plan = motion.get("plan")
    if isinstance(plan, (dict, list)):
        p = plans / f"{asset_id}.json"
        atomic_write_text(p, json.dumps(plan, ensure_ascii=False, indent=2))
        out["plan"] = f"{BROLL_DIR}/{PLANS_DIR}/{asset_id}.json"
    plan_dir = motion.get("plan_dir")
    if plan_dir:
        src = Path(plan_dir)
        if not src.is_dir():
            raise BrollError("no_file", f"plan_dir が無い: {plan_dir}", 404)
        dst = plans / asset_id
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("vendor", "snapshots", "renders", ".hyperframes", "history", "node_modules",
                                                      "__pycache__"))
        out["plan_dir"] = f"{BROLL_DIR}/{PLANS_DIR}/{asset_id}/"
    for k in ("fit", "composition", "source_line_id"):
        if motion.get(k) is not None:
            out[k] = motion[k]
    return out


def _check_line(ep_dir: Path, line_id: str) -> None:
    if not isinstance(line_id, str) or not LINE_ID_RE.match(line_id):
        raise BrollError("bad_line", f"line_id が正しくない: {line_id!r}")
    ids = script_line_ids(ep_dir)
    if ids is not None and line_id not in ids:
        raise BrollError("unknown_line", f"台本に無い行: {line_id}", 404)


def _assign(doc: dict, ep_dir: Path, line_id: str, asset_id: str, index: int | None, note: str) -> dict:
    _check_line(ep_dir, line_id)
    if find_asset(doc, asset_id) is None:
        raise BrollError("unknown_asset", f"箱に無い素材: {asset_id}", 404)
    items = doc["placements"].setdefault(line_id, [])
    cur = next((it for it in items if it["asset_id"] == asset_id), None)
    if cur is not None:                               # 同じ行に同じ素材は1つだけ（やり直しても増えない）
        if note:
            cur["note"] = note
        return {"line_id": line_id, "asset_id": asset_id, "index": items.index(cur)}
    item = {"asset_id": asset_id, "note": note}
    if index is None or index >= len(items):
        items.append(item)
    else:
        items.insert(max(0, index), item)
    return {"line_id": line_id, "asset_id": asset_id, "index": items.index(item)}


def _replace(doc: dict, old_id: str, new_id: str) -> None:
    """作り直し（M5）: 古い素材の割り当てを新しい素材へ差し替える。古い素材は箱に残す（印を付ける）。"""
    for items in doc["placements"].values():
        pos = next((i for i, it in enumerate(items) if it["asset_id"] == old_id), None)
        if pos is None:
            continue
        if any(it["asset_id"] == new_id for it in items):
            items.pop(pos)
        else:
            items[pos] = {**items[pos], "asset_id": new_id}
    old = find_asset(doc, old_id)
    if old is not None:
        old["replaced_by"] = new_id


# ── 割り当て ──

def assign(ep_dir: Path, project_id: str, episode: int, line_id: str, asset_id: str, index: int | None = None,
           note: str = "") -> dict:
    with lock(ep_dir):
        doc = load(ep_dir, project_id, episode)
        res = _assign(doc, ep_dir, line_id, asset_id, index, note)
        save(ep_dir, doc)
        return res


def unassign(ep_dir: Path, project_id: str, episode: int, line_id: str, asset_id: str) -> dict:
    with lock(ep_dir):
        doc = load(ep_dir, project_id, episode)
        items = doc["placements"].get(line_id, [])
        kept = [it for it in items if it["asset_id"] != asset_id]
        if len(kept) == len(items):
            raise BrollError("not_assigned", f"{line_id} に {asset_id} は当たっていない", 404)
        if kept:
            doc["placements"][line_id] = kept
        else:
            doc["placements"].pop(line_id, None)       # 空の行は残さない（素材は箱に残る）
        save(ep_dir, doc)
        return {"line_id": line_id, "asset_id": asset_id, "remaining": [it["asset_id"] for it in kept]}


def reorder(ep_dir: Path, project_id: str, episode: int, line_id: str, asset_ids: list[str]) -> dict:
    with lock(ep_dir):
        doc = load(ep_dir, project_id, episode)
        items = doc["placements"].get(line_id, [])
        if sorted(asset_ids) != sorted(it["asset_id"] for it in items):
            raise BrollError("bad_reorder", "asset_ids はその行に当たっている素材の並べ替えでなければならない（増減は assign・unassign）")
        by = {it["asset_id"]: it for it in items}
        doc["placements"][line_id] = [by[a] for a in asset_ids]
        save(ep_dir, doc)
        return {"line_id": line_id, "asset_ids": list(asset_ids)}


# ── 素材の直し・外す ──

_PATCHABLE = ("tags", "license", "credit", "note", "source_note")


def patch_asset(ep_dir: Path, project_id: str, episode: int, asset_id: str, changes: dict) -> dict:
    """タグ・ライセンス・クレジット・メモだけ直せる。asset_id・ファイル名・sha256 は変えない（BR2）。"""
    bad = [k for k in changes if k not in _PATCHABLE]
    if bad:
        raise BrollError("not_patchable", f"直せるのは {list(_PATCHABLE)} だけ（ID・ファイル名は変えない）: {bad}")
    with lock(ep_dir):
        doc = load(ep_dir, project_id, episode)
        a = find_asset(doc, asset_id)
        if a is None:
            raise BrollError("unknown_asset", f"箱に無い素材: {asset_id}", 404)
        lic = changes.get("license", a.get("license"))
        _validate_meta(lic, a.get("source") or {"kind": "user"}, changes.get("credit", a.get("credit", "")))
        for k, v in changes.items():
            if k == "tags":
                if not isinstance(v, dict):
                    raise BrollError("bad_tags", "tags は辞書")
                a["tags"] = {**a.get("tags", {}), **v}
            else:
                a[k] = v
        save(ep_dir, doc)
        return a


def trash_asset(ep_dir: Path, project_id: str, episode: int, asset_id: str) -> dict:
    """箱から外す（_trash へ移すだけ・BR5）。割り当てが残っていれば 409。"""
    with lock(ep_dir):
        doc = load(ep_dir, project_id, episode)
        a = find_asset(doc, asset_id)
        if a is None:
            raise BrollError("unknown_asset", f"箱に無い素材: {asset_id}", 404)
        lines = used_by(doc).get(asset_id, [])
        if lines:
            raise BrollError("still_placed", f"{len(lines)} 行に当たっている（先に外す）: {lines}", 409)
        src = ep_dir / a["file"]
        trash = broll_dir(ep_dir) / TRASH_DIR
        trash.mkdir(parents=True, exist_ok=True)
        moved = None
        if src.is_file():
            dst = trash / src.name
            n = 1
            while dst.exists():
                dst = trash / f"{src.stem}.{n}{src.suffix}"
                n += 1
            shutil.move(str(src), str(dst))
            moved = f"{BROLL_DIR}/{TRASH_DIR}/{dst.name}"
        doc["assets"] = [x for x in doc["assets"] if x["asset_id"] != asset_id]
        doc["trashed"].append({**a, "trashed_at": _now(), "trash_file": moved})   # ID を使い回さない（BR2）
        save(ep_dir, doc)
        return {"trashed": asset_id, "trash_file": moved}


def asset_file(ep_dir: Path, name: str) -> Path | None:
    """配信してよいファイル（broll/ 配下の、直下のファイルだけ）。"""
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    base = broll_dir(ep_dir).resolve()
    f = (base / name).resolve()
    return f if f.parent == base and f.is_file() else None


# ── 旧 footage.json からの移行（§3-3・1回・冪等） ──

_LEGACY_SOURCE = {"pexels": "pexels", "pixabay": "pixabay", "vecteezy": "vecteezy", "ai": "ai", "user_upload": "user",
                  "user": "user"}
_LEGACY_LICENSE = {"pexels-free": "pexels-free", "pixabay-content-license": "pixabay-content-license",
                   "ai-generated": "ai-generated", "user": "unknown", "vecteezy-free": "vecteezy-free",
                   "vecteezy-pro": "vecteezy-pro"}


def migrate_from_footage(ep_dir: Path, project_id: str, episode: int) -> dict:
    """旧クリップを register_asset に通して箱へ入れる（コピー）。**割り当てはしない**（旧セクション → 行は正しくない＝H2）。
    元の `footage/` と `footage.json` は触らない。同じ中身は二重に登録しない＝何度でも走らせてよい。"""
    fp = ep_dir / "footage.json"
    if not fp.is_file():
        raise BrollError("no_footage", "footage.json が無い（移行するものが無い）", 404)
    try:
        footage = json.loads(fp.read_text(encoding="utf-8"))
    except ValueError as e:
        raise BrollError("bad_footage", "footage.json が読めない") from e
    report = {"registered": [], "duplicates": [], "missing": [], "skipped": []}
    for clip in footage.get("clips") or []:
        rel = clip.get("file_path") or ""
        src = (ep_dir / rel).resolve()
        if not rel or ep_dir.resolve() not in src.parents or not src.is_file():
            report["missing"].append({"clip_id": clip.get("id"), "file_path": rel})
            continue
        kind = _LEGACY_SOURCE.get(clip.get("source") or "")
        if kind is None:
            report["skipped"].append({"clip_id": clip.get("id"), "reason": f"未知の source: {clip.get('source')!r}"})
            continue
        lic_old = clip.get("license") or ""
        lic = _LEGACY_LICENSE.get(lic_old, "unknown")
        section = clip.get("section") or ""
        note = f"旧セクション: {section}" if section else ""
        try:
            r = register_asset(
                ep_dir, project_id, episode, src, original_name=clip.get("notes") or src.name,
                source={"kind": kind, "id": clip.get("candidate_id") or "", "url": clip.get("original_url") or ""},
                license=lic, credit="", added_by="migrate", note=note,
                tags={"keywords": list(clip.get("keywords") or [])},
                extra={"legacy": {"clip_id": clip.get("id"), "license": lic_old, "section": section,
                                  "line_ids": list(clip.get("line_ids") or [])}})
        except BrollError as e:
            report["skipped"].append({"clip_id": clip.get("id"), "reason": str(e)})
            continue
        (report["duplicates"] if r["duplicate"] else report["registered"]).append(
            {"clip_id": clip.get("id"), "asset_id": r["asset"]["asset_id"]})
    with lock(ep_dir):
        doc = load(ep_dir, project_id, episode)
        doc["migrated_from"] = {"file": "footage.json", "at": _now(), "clip_count": len(footage.get("clips") or [])}
        save(ep_dir, doc)
    return report
