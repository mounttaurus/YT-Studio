"""
キャラクター台帳の管理（プロジェクト横断、チャンネル共通）。

shared/characters/{char_id}/
  character.json   ← キャラ定義（一貫性の核: appearance_prompt＋スタイル別seed/LoRA）
  reference/       ← 確定リファレンス画像（Phase 3でNanoBananaの参照入力に使う）
  generated/       ← 生成画像（命名規則: char_{char_id}_{style}_{expression}_{NNN}.png）

character.json 構造:
  char_id, name, description
  appearance_prompt: 外見の固定プロンプト（キャラシート。髪・服装・体型等）
  uses_images: 画像を使うキャラか（Falseなら画像生成の全経路で弾く。声だけのナレーター等）
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
# 1.3.0: uses_images を追加（画像を使わない＝声だけのキャラの明示。追加のみ・後方互換）
SCHEMA_VERSION = "1.3.0"

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
    # ⚠️ setdefault(True) にしない。旧データにこのフィールドは無いので、既定値は
    # appearance_prompt の有無から**推定**する（ナレーター等の空キャラは自動的にFalse）。
    # この推定は**既定値の決定にのみ**使う。以後の判定に appearance_prompt を代用しないこと
    #（「何を描くか」と「画像を使う意思」は別物。混ぜたのが §15-1 の間違い）。
    # 一度 write_character を通れば明示値として永続化される。
    if "uses_images" not in char:
        char["uses_images"] = bool((char.get("appearance_prompt") or "").strip())
    else:
        char["uses_images"] = bool(char["uses_images"])
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
        refs = reference_files(d.name)
        meta = c.get("reference_meta", {})
        out.append({
            "char_id": c["char_id"],
            "name": c.get("name", ""),
            "caption": c.get("caption", ""),
            "description": c.get("description", ""),
            "voice": c.get("voice", {"engine": "", "voice_id": ""}),
            "generation_count": len(c.get("generations", [])),
            # per-capability 任意化（WORK_LOG 2026-07-05）: 外見が無いキャラ（声だけのナレーター等）は
            # 画像生成不可。一覧をN+1で取得させず、ここで真偽値だけ乗せる（全文は返さない）
            "has_appearance": bool((c.get("appearance_prompt") or "").strip()),
            # 画像を使う意思（キャラ設定の憲法）。False なら画像生成の全経路で弾く。
            # has_appearance と両方返すのは、UIが「なぜ生成できないか」を出し分けるため
            "uses_images": bool(c.get("uses_images")),
            "references": refs,
            # 参照画像のラベル overlay（存在するファイル分のみ）。フロントは charaRefLabel(fn) で引く。
            "reference_meta": {fn: meta.get(fn, {}) for fn in refs},
            "updated_at": c.get("updated_at", ""),
        })
    return out


def create_character(
    char_id: str, name: str, appearance_prompt: str, description: str = "",
    caption: str = "", voice: dict | None = None, uses_images: bool | None = None,
) -> dict:
    char = {
        "schema_version": SCHEMA_VERSION,
        "char_id": char_id,
        "name": name,
        "caption": caption,                       # 字幕表示名（空ならnameを使う）
        "description": description,
        "appearance_prompt": appearance_prompt,
        # None なら normalize_character が appearance_prompt から推定する
        **({} if uses_images is None else {"uses_images": bool(uses_images)}),
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


def reference_files(char_id: str) -> list[str]:
    """reference/ 内の実ファイル名（新しい順ではなく名前順）。存在が正、character.json は上乗せのみ。"""
    d = char_dir(char_id) / "reference"
    if not d.exists():
        return []
    return sorted(p.name for p in d.glob("*") if p.is_file())


def can_generate_images(char_id: str, *, require_reference: bool = True) -> tuple[bool, str]:
    """画像生成を許すか。許さないなら理由も返す（UIにそのまま出せる日本語）。

    **画像生成の可否はここ1箇所で決める**。以前は紙芝居 ``POST /panel`` と
    ``panel_library/generate`` に別々のガードが書かれていて、片方だけ直した結果
    判定軸がズレた（`appearance_prompt` の有無だけを見ており、外見だけ複製した
    Voiceバリアントキャラを通してしまっていた）。同じ間違いを繰り返さないため、
    生成経路は全てこの関数を呼ぶこと。

    判定順（この順で理由が変わる）:
      1. キャラが存在しない
      2. uses_images が False        → 「画像を使わない設定」（人が宣言した意思）
      3. appearance_prompt が空      → 「何を描くか」が無い
      4. reference/ が0枚            → 「同じ人物である担保」が無い

    ⚠️ 2 と 4 は別物。reference だけ見ると「まだ参照を登録していないだけ」と
    「そもそも画像不要」を区別できず、案内すべき次の一手が変わってしまう。

    require_reference=False は**リファレンス画像そのものを作る経路**のためにある
    （🎭キャラタブの POST /characters/{id}/generate。生成→⭐昇格で最初の1枚を得る）。
    ここまで reference 必須にすると最初の1枚が永久に作れない（鶏と卵）。
    ⚠️ 在庫に積む経路では絶対に False にしないこと ── 参照無しで積むと
    「同じキャラの並行在庫」ができる。切るのは4だけで、1〜3の判定と順序は共有する。
    """
    c = read_character(char_id)
    if c is None:
        return False, f"キャラが見つかりません: {char_id}"
    label = c.get("name") or char_id
    if not c.get("uses_images"):
        return False, (f"「{label}」は画像を使わない設定です（声のみのキャラ）。"
                       "画像を使うなら🎭キャラタブで設定を変更してください")
    if not (c.get("appearance_prompt") or "").strip():
        return False, (f"「{label}」は外見(appearance_prompt)が未設定のため生成できません。"
                       "🎭キャラタブで外見を設定してください")
    if require_reference and not reference_files(char_id):
        return False, (f"「{label}」はリファレンス画像がありません"
                       "（同じ人物である担保が取れず、生成するたびに別人になります）。"
                       "🎭キャラタブで参照画像を登録してください")
    return True, ""
