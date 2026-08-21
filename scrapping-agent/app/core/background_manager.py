"""
背景アーカイブ（shared/backgrounds/）の索引管理と生成オーケストレーション。

Aロールの aroll_manager.py と対の存在だが、背景は台本行に紐付かない独立ライブラリ
なので、行単位の同期（sync/orphan判定）は持たない。単純な「作って登録する・引く」だけ。

保管レイアウト（Docs/BACKGROUND_ARCHIVE.md §3）:
  shared/backgrounds/
    backgrounds.json      ← 索引（本ファイルが読み書きする対象）
    images/{bg_id}.png    ← 実体
    ref/{spot_id}.png     ← 定点ごとの承認済みキーフレーム（is_keyframe:true のコピー先）
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.core import background_presets, nanobanana_client, style_manager

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
BG_DIR = SHARED_DIR / "backgrounds"
INDEX_FILE = BG_DIR / "backgrounds.json"
IMAGES_DIR = BG_DIR / "images"
REF_DIR = BG_DIR / "ref"

SCHEMA_VERSION = "1.0.0"

# location カテゴリで camera が意味を持つ framing（それ以外は null。background_presets と揃える）
CAMERA_FRAMINGS = background_presets.CAMERA_FRAMINGS


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_index() -> dict:
    if not INDEX_FILE.exists():
        return {"schema_version": SCHEMA_VERSION, "updated_at": _now(), "backgrounds": []}
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        data.setdefault("backgrounds", [])
        return data
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "updated_at": _now(), "backgrounds": []}


def save_index(data: dict) -> None:
    BG_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now()
    INDEX_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_backgrounds(
    *, category: str = "", spot: str = "", motif: str = "", era: str = "",
    light: str = "", framing: str = "", mood: str = "", q: str = "",
) -> list[dict]:
    """フィルタ検索。各条件は空文字なら無視。mood/q は部分一致（mood配列に含む/自由語がbg_idに含む）。"""
    rows = load_index().get("backgrounds", [])
    out = []
    for b in rows:
        if category and b.get("category") != category:
            continue
        if spot and b.get("spot") != spot:
            continue
        if motif and b.get("motif") != motif:
            continue
        if era and b.get("era") != era:
            continue
        if light and b.get("light") != light:
            continue
        if framing and b.get("framing") != framing:
            continue
        if mood and mood not in (b.get("mood") or []):
            continue
        if q and q.lower() not in b.get("bg_id", "").lower():
            continue
        out.append(b)
    return out


def record_usage(bg_id: str) -> None:
    """背景の使用回数(times_used)を+1する。suggest_backgroundのローテーションが参照する。"""
    data = load_index()
    for b in data.get("backgrounds", []):
        if b.get("bg_id") == bg_id:
            b["times_used"] = b.get("times_used", 0) + 1
            save_index(data)
            return


def suggest_background(framing: str, emotion: str, exclude_ids: set | None = None) -> dict | None:
    """Aロール行の(shot→framing, emotion)から背景を1件サジェストする（行単位自動割当の初期値）。

    1. framing一致で絞る（一致0件ならframing条件を諦めて全件から選ぶ＝未割当より劣化選択を優先）
    2. emotion→mood対応表（background_presets.EMOTION_TO_MOOD）でmoodが重なるものを優先
       （マッチ無しならframing一致のみの候補にフォールバック）
    3. exclude_ids（直近使った背景。呼び出し側が窓を管理する）を避ける
    4. 残った候補のうちtimes_usedが最小のものを選ぶ（キャラ画像ライブラリのfind_currentと同じ
       「最小消費優先」ローテーション。新規追加した背景は自動的に優先消費される）

    候補が1件も無ければNone（呼び出し側は未割当のまま残す。無理に割り当てない）。
    """
    rows = load_index().get("backgrounds", [])
    if not rows:
        return None
    candidates = [b for b in rows if not framing or b.get("framing") == framing] or rows

    moods = background_presets.EMOTION_TO_MOOD.get(emotion or "", [])
    if moods:
        mood_matched = [b for b in candidates if any(m in (b.get("mood") or []) for m in moods)]
        if mood_matched:
            candidates = mood_matched

    if exclude_ids:
        fresh = [b for b in candidates if b.get("bg_id") not in exclude_ids]
        if fresh:
            candidates = fresh

    candidates.sort(key=lambda b: (b.get("times_used", 0), b.get("bg_id", "")))
    return candidates[0]


def _next_bg_id(category: str, key_parts: list[str]) -> str:
    """命名規則（§4）: {prefix}_{key_parts...}_{nnn}。同じ組み合わせが既にあれば連番を進める。"""
    prefix = {"location": "loc", "psych": "psy", "comic": "com"}.get(category, category)
    base = "_".join([prefix, *[p.replace("_", "-") for p in key_parts if p]])
    existing = {b["bg_id"] for b in load_index().get("backgrounds", [])}
    n = 1
    while True:
        bg_id = f"{base}_{n:03d}"
        if bg_id not in existing:
            return bg_id
        n += 1


def register(entry: dict) -> dict:
    """索引へ1件追記する（画像ファイルは呼び出し側が既に images/ へ保存済みである前提）。"""
    data = load_index()
    data["backgrounds"].append(entry)
    save_index(data)
    return entry


def delete_background(bg_id: str) -> bool:
    """索引から除去し、実体ファイルも削除する。存在しなければ False。"""
    data = load_index()
    rows = data.get("backgrounds", [])
    target = next((b for b in rows if b.get("bg_id") == bg_id), None)
    if not target:
        return False
    data["backgrounds"] = [b for b in rows if b.get("bg_id") != bg_id]
    save_index(data)
    img = (BG_DIR / target.get("image", "")).resolve()
    if img.is_relative_to(IMAGES_DIR.resolve()) and img.is_file():
        try:
            img.unlink()
        except OSError:
            pass
    return True


async def generate_and_register(
    *, category: str, spot: str = "", motif: str = "", era: str = "",
    light: str = "", camera: str = "", framing: str = "",
    form: str = "", effect: str = "", mood: list[str] | None = None,
    model: str = "", is_keyframe: bool = False, note: str = "",
) -> dict:
    """1枚生成し、shared/backgrounds/images/ へ保存、索引へ登録して返す。

    location: spot または motif または era のいずれか1つ（+ light + framing 必須）。
    psych/comic: form または effect のいずれか1つ。
    """
    style_name = background_presets.STYLE_BY_CATEGORY.get(category, "kamishibai_bg")
    base_style = style_manager.get_style(style_name)
    style_prefix = (base_style or {}).get("prefix", "")

    prompt = background_presets.build_background_prompt(
        category=category, style_prefix=style_prefix,
        spot=spot, motif=motif, era=era, light=light, camera=camera, framing=framing,
        form=form, effect=effect,
    )

    data = await nanobanana_client.generate_one(prompt, aspect="16:9", model=(model.strip() or None))

    if category == "location":
        key_parts = [spot or motif or era, light, framing]
    else:
        key_parts = [form or effect]
    bg_id = _next_bg_id(category, key_parts)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    (IMAGES_DIR / f"{bg_id}.png").write_bytes(data)

    entry = {
        "bg_id": bg_id, "category": category,
        "spot": spot or None, "motif": motif or None, "era": era or None,
        "light": light or None,
        "camera": camera if (category == "location" and framing in CAMERA_FRAMINGS) else None,
        "framing": framing or None,
        "form": form or None, "effect": effect or None,
        "mood": mood or [],
        "aspect": "16:9",
        "image": f"images/{bg_id}.png",
        "style": style_name,
        "model": model.strip() or nanobanana_client.MODEL,
        "prompt": prompt,
        "provider": "nanobanana",
        "is_keyframe": bool(is_keyframe),
        "source_ref": None,
        "created_at": _now(),
        "note": note,
    }
    register(entry)

    if is_keyframe and (spot or motif):
        REF_DIR.mkdir(parents=True, exist_ok=True)
        ref_name = f"{spot or motif}.png"
        (REF_DIR / ref_name).write_bytes(data)

    return entry
