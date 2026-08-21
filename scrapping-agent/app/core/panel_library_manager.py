"""
キャラ所有ライブラリ（shared/characters/{char_id}/panel_library/）の索引管理と生成。

Aロールの演技スロット（emotion/shot/angle。panel_presets.pyの語彙・aroll_manager.pyの
slot_key照合ロジックと同じ枠組み）ごとに作り置きした画像を保持する。台本行の画像生成時、
ここに一致するスロットがあれば無料でコピーし、無ければ従来どおりNanoBananaで新規生成する
（Phase 3の「解決順序の追加」。既存のAロール生成は置き換えない。Docs/AROLL_ASSET_PLAN.md）。

保管レイアウト（背景アーカイブと同型。Docs/BACKGROUND_ARCHIVE.md §3）:
  shared/characters/{char_id}/panel_library/
    library.json            ← 索引（本ファイルが読み書きする対象）
    images/{slot_id}.png    ← 実体

★appearance_version（キャラ外見の版）を必ずキーに含める。忘れるとキャラの外見を更新した
瞬間に、古いライブラリ画像と新規生成が同一話に混在して衣装が途中で変わる
（Docs/AROLL_ASSET_PLAN.md §6）。世代違いは黙って使わず「古い」ものとして除外する
（find_currentはappearance_versionが一致する最新世代のみを返す）。

★ハッシュ方式はブリーフ（AROLL_SLOT_REUSE_BRIEF.md §5）が示唆した「mtime」ではなく
「appearance_prompt + reference/内ファイルのバイト内容」を使う。mtimeはファイルコピーや
git checkoutで中身が同じでも変わってしまい、実質無変更なのにライブラリ全体が誤って
「古い」判定されるおそれがあるため、内容ハッシュの方が安定する。
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.core import character_manager, nanobanana_client, panel_presets, style_manager

SCHEMA_VERSION = "1.0.0"

BACKGROUND_FRAGMENT = "plain solid pastel background, flat single color, no scenery"
PROMPT_SUFFIX = "No text, no letters, no speech bubbles, no watermark in the image."


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def library_dir(char_id: str) -> Path:
    return character_manager.char_dir(char_id) / "panel_library"


def images_dir(char_id: str) -> Path:
    return library_dir(char_id) / "images"


def index_file(char_id: str) -> Path:
    return library_dir(char_id) / "library.json"


def appearance_version(char_id: str) -> str:
    """appearance_prompt + reference/内ファイルの内容ハッシュ（先頭12桁）。

    キャラの外見が変わった瞬間にこの値も変わる＝ライブラリの世代判定キー。
    """
    c = character_manager.read_character(char_id) or {}
    h = hashlib.sha256()
    h.update((c.get("appearance_prompt") or "").strip().encode("utf-8"))
    ref_dir = character_manager.char_dir(char_id) / "reference"
    if ref_dir.exists():
        for p in sorted(ref_dir.glob("*")):
            if p.is_file():
                h.update(p.name.encode("utf-8"))
                h.update(p.read_bytes())
    return h.hexdigest()[:12]


def load_index(char_id: str) -> dict:
    f = index_file(char_id)
    if not f.exists():
        return {"schema_version": SCHEMA_VERSION, "char_id": char_id, "updated_at": _now(), "entries": []}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        data.setdefault("entries", [])
        return data
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "char_id": char_id, "updated_at": _now(), "entries": []}


def save_index(char_id: str, data: dict) -> None:
    library_dir(char_id).mkdir(parents=True, exist_ok=True)
    images_dir(char_id).mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now()
    index_file(char_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_entries(char_id: str, *, emotion: str = "", shot: str = "", angle: str = "") -> list[dict]:
    """索引一覧を返す。各entryに is_stale（世代違いか）と review_status（欠落は"approved"扱い）を正規化して付与する。"""
    current = appearance_version(char_id)
    out = []
    for e in load_index(char_id).get("entries", []):
        if emotion and e.get("emotion") != emotion:
            continue
        if shot and e.get("shot") != shot:
            continue
        if angle and e.get("angle") != angle:
            continue
        out.append({
            **e,
            "is_stale": e.get("appearance_version") != current,
            "review_status": e.get("review_status", "approved"),
        })
    return out


def find_current(char_id: str, emotion: str, shot: str, angle: str) -> dict | None:
    """slot(emotion/shot/angle)に一致し、appearance_versionが今と同じ・かつ承認済みのentryのうち
    「最も使われていない」1件を返す（ローテーション。2026-08-21）。

    世代違い・review_status="pending"（未承認）は対象外（Noneを返す＝呼び出し側は通常どおり
    新規生成にフォールバックする）。review_status欠落は既存資産の後方互換として承認済み扱い。

    候補が複数ある場合は`times_used`（record_usageで実消費のたびに+1される）が最小のものを選ぶ。
    タイブレークはslot_id昇順（決定的にするため）。新規追加したバリアントはtimes_used=0から
    始まるため自然に優先消費され、シリーズを通算して均等にローテーションする（1話内のdedupに
    閉じない。Docs/AROLL_ASSET_PLAN.md §16参照）。

    ⚠️ この関数自体は状態を変更しない（ドライラン安全）。実際に選んだentryを消費したら、
    呼び出し側が必ず record_usage() を呼ぶこと（generate_line_imageのみが呼ぶ想定。
    generation_plan_estimateのようなドライランは呼んではいけない）。
    """
    current = appearance_version(char_id)
    candidates = [
        e for e in load_index(char_id).get("entries", [])
        if e.get("emotion") == emotion and e.get("shot") == shot and e.get("angle") == angle
        and e.get("appearance_version") == current
        and e.get("review_status", "approved") == "approved"
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda e: (e.get("times_used", 0), e.get("slot_id", "")))
    return candidates[0]


def record_usage(char_id: str, slot_id: str) -> None:
    """ライブラリentryの使用回数(times_used)を+1する。find_currentのローテーションが参照する。

    実際に画像を消費した時だけ呼ぶ（ドライランでは呼ばない）。
    """
    data = load_index(char_id)
    for e in data.get("entries", []):
        if e.get("slot_id") == slot_id:
            e["times_used"] = e.get("times_used", 0) + 1
            save_index(char_id, data)
            return


def approve_entry(char_id: str, slot_id: str) -> dict | None:
    """pending状態のentryを承認する（review_status="approved"）。以後find_currentの対象になる。"""
    data = load_index(char_id)
    for e in data.get("entries", []):
        if e.get("slot_id") == slot_id:
            e["review_status"] = "approved"
            save_index(char_id, data)
            return e
    return None


def _next_slot_id(emotion: str, shot: str, angle: str, existing: set[str]) -> str:
    base = f"{emotion}_{shot}_{angle}"
    n = 1
    while True:
        sid = f"{base}_{n:03d}"
        if sid not in existing:
            return sid
        n += 1


def delete_entry(char_id: str, slot_id: str) -> bool:
    """索引から除去し、実体ファイルも削除する。存在しなければFalse。"""
    data = load_index(char_id)
    entries = data.get("entries", [])
    target = next((e for e in entries if e.get("slot_id") == slot_id), None)
    if not target:
        return False
    data["entries"] = [e for e in entries if e.get("slot_id") != slot_id]
    save_index(char_id, data)
    img = (library_dir(char_id) / target.get("image", "")).resolve()
    if img.is_relative_to(images_dir(char_id).resolve()) and img.is_file():
        try:
            img.unlink()
        except OSError:
            pass
    return True


def _resolve_refs(char_id: str) -> list[tuple[bytes, str, str]]:
    """aroll_manager._resolve_refsと同じ方式（単独キャラなので最大2枚）。"""
    c = character_manager.read_character(char_id)
    name = (c or {}).get("name") or char_id
    ref_dir = character_manager.char_dir(char_id) / "reference"
    refs: list[tuple[bytes, str, str]] = []
    if not ref_dir.exists():
        return refs
    files = sorted(
        (p for p in ref_dir.glob("*") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:2]
    for p in files:
        label = f"{name}: keep this character consistent (same face, hairstyle, outfit)"
        refs.append((p.read_bytes(), nanobanana_client.mime_for(p.name), label))
    return refs


async def generate_and_register(
    char_id: str, *, emotion: str, shot: str, angle: str, pose: str = "",
    style_name: str = "kamishibai", model: str = "", replace_stale: bool = True,
    review_status: str = "approved",
) -> dict:
    """1スロット生成し、panel_library/images/へ保存・索引登録して返す。

    replace_stale=True（既定）: 同じ(emotion,shot,angle)を持つ旧世代（appearance_version不一致）
    のentryがあれば実体ごと削除してから追加する（世代混在を索引に残さない）。

    review_status="approved"（既定）: 1件だけ明示的に生成する通常の呼び出しは、呼び出した人が
    その場で結果を見て判断できるためそのまま承認済み扱いにする。generate_variants()経由の
    一括バリアント生成だけは"pending"で登録し、人の目視確認（approve_entry）を経てから
    find_current（Aロール消費）の対象になる（同一キャラなのに瞳の色が違う等の個体差が
    無審査で本番に流れるのを防ぐ。Docs/AROLL_ASSET_PLAN.md §14参照）。
    """
    c = character_manager.read_character(char_id)
    if not c:
        raise ValueError(f"character not found: {char_id}")
    appearance = (c.get("appearance_prompt") or "").strip()
    style = style_manager.get_style(style_name) or {}
    prefix = (style.get("prefix") or "").strip()

    body = panel_presets.build_panel_prompt(
        appearance, prefix, emotion_id=emotion, shot_id=shot, angle_id=angle,
        pose_id=pose, background_mode="flat",
    )
    prompt = f"{body}, {BACKGROUND_FRAGMENT}. {PROMPT_SUFFIX}"
    refs = _resolve_refs(char_id)

    data = await nanobanana_client.generate_one(
        prompt, refs, aspect="16:9", allow_fallback=False, model=(model.strip() or None),
    )

    ver = appearance_version(char_id)
    idx = load_index(char_id)

    if replace_stale:
        stale = [
            e for e in idx["entries"]
            if e.get("emotion") == emotion and e.get("shot") == shot and e.get("angle") == angle
            and e.get("appearance_version") != ver
        ]
        for e in stale:
            img = (library_dir(char_id) / e.get("image", "")).resolve()
            if img.is_relative_to(images_dir(char_id).resolve()) and img.is_file():
                try:
                    img.unlink()
                except OSError:
                    pass
        stale_ids = {e["slot_id"] for e in stale}
        idx["entries"] = [e for e in idx["entries"] if e.get("slot_id") not in stale_ids]

    existing_ids = {e["slot_id"] for e in idx["entries"]}
    slot_id = _next_slot_id(emotion, shot, angle, existing_ids)
    images_dir(char_id).mkdir(parents=True, exist_ok=True)
    (images_dir(char_id) / f"{slot_id}.png").write_bytes(data)

    entry = {
        "slot_id": slot_id,
        "emotion": emotion, "shot": shot, "angle": angle, "pose": pose or None,
        "appearance_version": ver,
        "aspect": "16:9",
        "image": f"images/{slot_id}.png",
        "style": style_name,
        "model": model.strip() or nanobanana_client.MODEL,
        "prompt": prompt,
        "provider": "nanobanana",
        "created_at": _now(),
        "note": "",
        "review_status": review_status,
        "times_used": 0,
    }
    idx["entries"].append(entry)
    save_index(char_id, idx)
    return entry


async def generate_variants(
    char_id: str, *, emotion: str, shot: str, angle: str, poses: list[str],
    style_name: str = "kamishibai", model: str = "",
) -> list[dict]:
    """同じ(emotion,shot,angle)にposeだけを変えて複数バリアントを一括生成する。

    matching key（slot_key）はemotion/shot/angleの3軸のまま変えない（組み合わせ爆発を避ける。
    Docs/AROLL_SLOT_REUSE_BRIEF.md §2-2の踏襲）。poseは既存の固定語彙（panel_presets.py）から
    選ぶ想定＝真に自由なLLM即興文にはしない（識別情報のブレを最小化するため）。

    生成物は全てreview_status="pending"で登録される。承認するまでfind_current（Aロール消費）
    からは見えない＝人が目視確認してapprove_entry()を呼ぶまで本番に流れない設計。
    """
    entries = []
    for pose in poses:
        entry = await generate_and_register(
            char_id, emotion=emotion, shot=shot, angle=angle, pose=pose,
            style_name=style_name, model=model, replace_stale=False,
            review_status="pending",
        )
        entries.append(entry)
    return entries
