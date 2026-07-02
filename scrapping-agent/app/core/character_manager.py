"""
キャラクター台帳の管理（プロジェクト横断、チャンネル共通）。

shared/characters/{char_id}/
  character.json   ← キャラ定義（一貫性の核: appearance_prompt＋スタイル別seed/LoRA）
  reference/       ← 確定リファレンス画像（Phase 3でNanoBananaの参照入力に使う）
  generated/       ← 生成画像（命名規則: char_{char_id}_{style}_{expression}_{NNN}.png）

character.json 構造:
  char_id, name, description
  appearance_prompt: 外見の固定プロンプト（キャラシート。髪・服装・体型等）
  styles: { "comic"|"realistic"|"deformed": {"seed": int|null, "loras": [...], "extra_prompt": ""} }
  provider: "comfy"（Phase 3で"nanobanana"追加予定）
  generations: 生成履歴 [{filename, style, expression, seed, prompt, created_at}]
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
CHARACTERS_DIR = SHARED_DIR / "characters"

# キャラ生成MVPの3スタイル（styles.jsonのusage=character/bothと対応）
CHARACTER_STYLES = ["comic", "realistic", "deformed"]

# character.json の現行スキーマ版。書き込み時に必ずこの値へスタンプし直す（旧ラベルのドリフトを断つ）。
SCHEMA_VERSION = "1.2.0"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_id(char_id: str) -> bool:
    return bool(_ID_RE.match(char_id))


def char_dir(char_id: str) -> Path:
    return CHARACTERS_DIR / char_id


def _json_path(char_id: str) -> Path:
    return char_dir(char_id) / "character.json"


def read_character(char_id: str) -> dict | None:
    f = _json_path(char_id)
    if not f.exists():
        return None
    try:
        return normalize_character(json.loads(f.read_text(encoding="utf-8")))
    except Exception:
        return None


def normalize_character(char: dict) -> dict:
    """任意の（旧/部分的な）キャラ辞書を現行スキーマへ正規化する（in-place）。

    キャラスキーマは追加専用の進化（voice/caption/reference_meta を足しただけ＝
    リネーム・削除なし）なので、ここでは欠落フィールドの既定値補完と schema_version の
    再スタンプのみ行う。将来リネーム/削除が生じたら、この1関数に吸収すれば
    import・読み込みの全経路が一括で守られる（現行スキーマ定義の単一の本籍）。
    """
    char.setdefault("char_id", "")
    char.setdefault("name", "")
    char.setdefault("caption", "")
    char.setdefault("description", "")
    char.setdefault("appearance_prompt", "")
    char.setdefault("voice", {"engine": "", "voice_id": ""})
    char.setdefault("provider", "comfy")
    char.setdefault("styles", {s: {"seed": None, "loras": [], "extra_prompt": ""} for s in CHARACTER_STYLES})
    char.setdefault("reference_meta", {})
    char.setdefault("generations", [])
    char["schema_version"] = SCHEMA_VERSION  # 常に現行値で上書き
    return char


def write_character(char: dict) -> None:
    normalize_character(char)
    d = char_dir(char["char_id"])
    (d / "reference").mkdir(parents=True, exist_ok=True)
    (d / "generated").mkdir(parents=True, exist_ok=True)
    char["updated_at"] = _now()
    _json_path(char["char_id"]).write_text(
        json.dumps(char, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_characters() -> list[dict]:
    """全キャラの要約一覧（generations履歴は件数のみ）。"""
    if not CHARACTERS_DIR.exists():
        return []
    out = []
    for d in sorted(CHARACTERS_DIR.iterdir()):
        c = read_character(d.name)
        if c is None:
            continue
        refs = sorted(p.name for p in (d / "reference").glob("*") if p.is_file())
        meta = c.get("reference_meta", {})
        out.append({
            "char_id": c["char_id"],
            "name": c.get("name", ""),
            "caption": c.get("caption", ""),
            "description": c.get("description", ""),
            "voice": c.get("voice", {"engine": "", "voice_id": ""}),
            "generation_count": len(c.get("generations", [])),
            "references": refs,
            # 参照画像のラベル overlay（存在するファイル分のみ）。フロントは charaRefLabel(fn) で引く。
            "reference_meta": {fn: meta.get(fn, {}) for fn in refs},
            "updated_at": c.get("updated_at", ""),
        })
    return out


def create_character(
    char_id: str, name: str, appearance_prompt: str, description: str = "",
    caption: str = "", voice: dict | None = None,
) -> dict:
    char = {
        "schema_version": SCHEMA_VERSION,
        "char_id": char_id,
        "name": name,
        "caption": caption,                       # 字幕表示名（空ならnameを使う）
        "description": description,
        "appearance_prompt": appearance_prompt,
        "voice": voice or {"engine": "", "voice_id": ""},  # 声の本籍（shared/voices/{engine}/参照）
        "provider": "comfy",
        "styles": {s: {"seed": None, "loras": [], "extra_prompt": ""} for s in CHARACTER_STYLES},
        # 参照画像のラベル overlay（ファイル名→{label}）。NanoBananaの役割分担プロンプト用。
        # reference/内のファイル実体が存在の正、ここはラベルの上乗せのみ（ドリフトしない）。
        "reference_meta": {},
        "generations": [],
        "created_at": _now(),
    }
    write_character(char)
    return char


def next_filename(char_id: str, style: str, expression: str) -> str:
    """命名規則 char_{char_id}_{style}_{expression}_{NNN}.png の次の空き連番を採番する。"""
    expr = re.sub(r"[^a-z0-9-]", "", expression.lower()) or "base"
    prefix = f"char_{char_id}_{style}_{expr}_"
    gen_dir = char_dir(char_id) / "generated"
    used = set()
    for p in gen_dir.glob(f"{prefix}*.png"):
        m = re.search(r"_(\d{3})\.png$", p.name)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"{prefix}{n:03d}.png"


def append_generation(char_id: str, entry: dict) -> None:
    char = read_character(char_id)
    if char is None:
        return
    entry["created_at"] = _now()
    char.setdefault("generations", []).append(entry)
    write_character(char)


def delete_generation(char_id: str, filename: str) -> bool:
    """character.json の generations[] から該当ファイルのエントリを除去する。

    ファイル実体の削除は呼び出し側（routes）が行う。除去が起きたら True。
    """
    char = read_character(char_id)
    if char is None:
        return False
    gens = char.get("generations", [])
    kept = [g for g in gens if g.get("filename") != filename]
    if len(kept) == len(gens):
        return False
    char["generations"] = kept
    write_character(char)
    return True


def get_reference_meta(char_id: str) -> dict:
    """参照画像のラベル overlay（ファイル名→{label}）を返す。"""
    char = read_character(char_id)
    if char is None:
        return {}
    return char.get("reference_meta", {})


def set_reference_label(char_id: str, filename: str, label: str) -> bool:
    """参照画像にラベルを付与/更新する（reference_meta[filename].label）。

    空ラベルはキーごと除去する。存在しないキャラは False。
    """
    char = read_character(char_id)
    if char is None:
        return False
    meta = char.setdefault("reference_meta", {})
    label = (label or "").strip()
    if label:
        meta[filename] = {"label": label}
    else:
        meta.pop(filename, None)
    write_character(char)
    return True
