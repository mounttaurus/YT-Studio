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
import io
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from app.core import background_presets, nanobanana_client, style_manager

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
BG_DIR = SHARED_DIR / "backgrounds"
INDEX_FILE = BG_DIR / "backgrounds.json"
IMAGES_DIR = BG_DIR / "images"
REF_DIR = BG_DIR / "ref"

SCHEMA_VERSION = "1.2.0"  # 1.2.0: 持ち込み登録（provider="upload"）。original_filename /
#                           width / height / edited_at を追加（追加のみ・後方互換）
#                           1.1.0: source_url / license / light_dx / light_mean /
#                           coverage / overlay を追加（外部調達素材）

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


# ─── ユーザーが持ち込んだ画像の登録 ────────────────────────────────
#
# 生成しない背景（自分で撮った・描いた・Vecteezy等で調達した）を1枚ずつ登録する口。
# ホスト側の psassist/scripts/register_backgrounds.py が PSD からまとめてやっていることを、
# UIから1枚ずつできるようにしたもの。索引のスキーマは向こうと揃える
# （Docs/BACKGROUND_ARCHIVE.md §2「外部調達素材の追加フロー」・§5 スキーマ）。

UPLOAD_CATEGORIES = ("location", "psych", "comic", "effect", "backdrop")

# 自動割当（suggest_background）が実際に読む軸。ここを自由入力にすると framing/mood が
# 一致しなくなり、割当が**黙って劣化する**（画面からは原因が見えない）ので語彙で固定する。
VOCAB_AXES = ("light", "camera", "framing")
# 人が付ける分類ラベル。bg_id とキーワードチップに出るだけで機械の判定には使わないので、
# 語彙に無い言葉（自分で撮った場所など）も受け付ける。
KEYWORD_AXES = ("spot", "motif", "era", "form", "effect")

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slug_keyword(v: str) -> str:
    """キーワードを bg_id に使える形へ。日本語だけなら空になる（bg_idからは落ちる）。

    落ちても困らない ── bg_id は識別子であって表示ラベルではなく、入力した言葉は
    entry の spot/motif/... 側にそのまま残る。
    """
    return _SLUG_RE.sub("-", (v or "").strip().lower().replace("_", "-")).strip("-")


def aspect_of(w: int, h: int) -> str:
    """近い定番比があればそれを、無ければ既約分数で返す。"""
    for label, ratio in (("16:9", 16 / 9), ("4:3", 4 / 3), ("1:1", 1.0),
                         ("9:16", 9 / 16), ("3:4", 3 / 4)):
        if h and abs(w / h - ratio) < 0.02:
            return label
    g = math.gcd(w, h) or 1
    return f"{w // g}:{h // g}"


def light_metrics(rgba: Image.Image) -> tuple[float | None, float | None, float]:
    """左右の輝度差（正=右が明るい）・平均輝度・不透明率。

    ⚠️ psassist/scripts/register_backgrounds.py の同名関数と**同じ式**。ホスト側の
    スクリプトはコンテナのコードをimportできない（HTTPでも繋がない方針）ので、
    ここだけは二重に持つ。片方を変えたらもう片方も変えること。
    用途は Docs/BACKGROUND_ARCHIVE.md §5「光源メタ（light_dx）の用途」。
    """
    a = np.asarray(rgba.convert("RGBA")).astype(float)
    alpha = a[:, :, 3] > 128
    coverage = float(alpha.mean())
    if coverage < 0.05:
        return None, None, round(coverage, 3)
    lum = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    t = max(1, lum.shape[1] // 3)

    def band(sl) -> float | None:
        m = alpha[:, sl]
        return float(lum[:, sl][m].mean()) if m.sum() > 50 else None

    left, right = band(slice(0, t)), band(slice(-t, None))
    if left is None or right is None:
        return None, round(float(lum[alpha].mean()), 1), round(coverage, 3)
    denom = max(1.0, (left + right) / 2)
    return (round(float((right - left) / denom), 3),
            round(float(lum[alpha].mean()), 1),
            round(coverage, 3))


def validate_axes(values: dict) -> None:
    """category / VOCAB_AXES / mood を語彙で検証する。空文字とNoneは無視。"""
    presets = background_presets.load_presets()
    cat = values.get("category")
    if cat and cat not in UPLOAD_CATEGORIES:
        allowed = " / ".join(UPLOAD_CATEGORIES)
        raise ValueError(f"系統が不正です: {cat}（{allowed} のいずれか）")
    for axis in VOCAB_AXES:
        v = values.get(axis)
        if v and v not in {p.get("id") for p in presets.get(axis, [])}:
            raise ValueError(f"{axis} に無い値です: {v}"
                             "（自動割当が読む軸なので語彙から選んでください）")
    known = {p.get("id") for p in presets.get("mood", [])}
    for m in values.get("mood") or []:
        if m not in known:
            raise ValueError(f"mood に無い値です: {m}（自動割当が読む軸です）")


def _keyword_parts(category: str, spot: str, motif: str, era: str,
                   form: str, effect: str, light: str, framing: str) -> list[str]:
    """bg_id の中間部（§4 命名規則）。生成側 generate_and_register と同じ組み立て。"""
    if category == "location":
        return [slug_keyword(spot or motif or era), light, framing]
    if category in ("psych", "backdrop"):
        return [slug_keyword(form)]
    return [slug_keyword(effect)]                      # comic | effect


def register_upload(
    data: bytes, *, category: str, spot: str = "", motif: str = "", era: str = "",
    light: str = "", camera: str = "", framing: str = "", form: str = "", effect: str = "",
    mood: list[str] | None = None, is_keyframe: bool = False, note: str = "",
    source_url: str = "", license: str = "", style: str = "", filename: str = "",
) -> dict:
    """持ち込み画像を1件アーカイブへ登録する（**生成しない**）。

    ⚠️ **透過を捨てない。** effect 系は「重ねて使う」素材で、アルファを捨てると背景を
    覆ってしまう（Docs/BACKGROUND_ARCHIVE.md §2）。元がRGBの画像だけRGBで保存する。
    """
    mood = [m for m in (mood or []) if m]
    if not category:
        raise ValueError("系統（category）は必須です")
    validate_axes({"category": category, "light": light, "camera": camera,
                   "framing": framing, "mood": mood})
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:                             # noqa: BLE001 — 画像でない入力
        raise ValueError(f"画像として読めませんでした: {type(e).__name__}") from e

    rgba = img.convert("RGBA")
    has_alpha = rgba.getchannel("A").getextrema()[0] < 255
    dx, mean, coverage = light_metrics(rgba)

    parts = [p for p in _keyword_parts(category, spot, motif, era, form, effect,
                                       light, framing) if p]
    bg_id = _next_bg_id(category, parts or ["user"])

    buf = io.BytesIO()
    (rgba if has_alpha else rgba.convert("RGB")).save(buf, "PNG")
    png = buf.getvalue()
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    (IMAGES_DIR / f"{bg_id}.png").write_bytes(png)

    entry = {
        "bg_id": bg_id, "category": category,
        "spot": spot or None, "motif": motif or None, "era": era or None,
        "light": light or None,
        "camera": camera if (category == "location" and framing in CAMERA_FRAMINGS) else None,
        "framing": framing or None,
        "form": form or None, "effect": effect or None,
        "mood": mood,
        "aspect": aspect_of(rgba.width, rgba.height),
        "image": f"images/{bg_id}.png",
        "style": style or None,
        "model": None,
        "prompt": None,
        "provider": "upload",
        "source_url": source_url or None,
        "license": license or None,
        "is_keyframe": bool(is_keyframe),
        "source_ref": None,
        "created_at": _now(),
        "note": note,
        "times_used": 0,
        # 光源メタ（§5）。キャラの陰影と向きが逆だと違和感が出るので候補の並べ替えに使う
        "light_dx": dx, "light_mean": mean, "coverage": coverage,
        "overlay": category == "effect",
        "original_filename": filename or None,
        "width": rgba.width, "height": rgba.height,
    }
    register(entry)

    if is_keyframe and (spot or motif):
        REF_DIR.mkdir(parents=True, exist_ok=True)
        (REF_DIR / f"{slug_keyword(spot or motif) or bg_id}.png").write_bytes(png)
    return entry


# メタデータを人が直せる項目。bg_id・image・実測値（light_dx等）はここに入れない
EDITABLE_FIELDS = ("category", "spot", "motif", "era", "light", "camera", "framing",
                   "form", "effect", "style", "note", "source_url", "license")


def update_background(bg_id: str, *, mood: list[str] | None = None,
                      is_keyframe: bool | None = None, **fields) -> dict | None:
    """メタデータを人が直す（画像は差し替えない）。None を渡した項目は触らない。

    ⚠️ **bg_id は書き換えない。** bg_id は実体ファイル名（images/{bg_id}.png）であり、
    aroll.json の panels[].background_id が指す先でもある。ラベルを直すたびに改名すると
    割当済みの行の参照が全部切れる。bg_id は「登録した時の分類が残った識別子」であって
    現在のラベルではない（キャラ所有ライブラリの slot_id と同じ扱い）。
    """
    fields = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    validate_axes({**fields, "mood": mood})
    data = load_index()
    for b in data.get("backgrounds", []):
        if b.get("bg_id") != bg_id:
            continue
        changed = []
        for k, v in fields.items():
            if v is None:
                continue
            new = v if k == "note" else (v or None)
            if b.get(k) != new:
                b[k] = new
                changed.append(k)
        if mood is not None:
            new_mood = [m for m in mood if m]
            if b.get("mood") != new_mood:
                b["mood"] = new_mood
                changed.append("mood")
        if is_keyframe is not None and bool(b.get("is_keyframe")) != bool(is_keyframe):
            b["is_keyframe"] = bool(is_keyframe)
            changed.append("is_keyframe")
        if "category" in changed:
            # effect は「重ねて使う」素材。系統を直したら合成側の扱いも追随させる（§2）
            b["overlay"] = b.get("category") == "effect"
            changed.append("overlay")
        if "framing" in changed and b.get("framing") not in CAMERA_FRAMINGS:
            b["camera"] = None                     # camera は wide/full_body でしか意味を持たない
        if changed:
            b["edited_at"] = _now()
            save_index(data)
        return {**b, "changed": changed}
    return None


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
