"""
スタイル定義の管理。
- デフォルトスタイル: app/styles/*.json（コンテナ内・変更不可）
- ユーザー作成スタイル: shared/styles/*.json（SHARED_DIR経由・追加・削除可）
"""
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core import character_reader

DEFAULT_STYLES_DIR = Path(__file__).parent.parent / "styles"
SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
USER_STYLES_DIR = SHARED_DIR / "styles"


def _ensure_user_styles_dir():
    USER_STYLES_DIR.mkdir(parents=True, exist_ok=True)


def _load_from_dir(directory: Path, is_builtin: bool) -> dict[str, dict]:
    styles = {}
    if not directory.exists():
        return styles
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["_is_builtin"] = is_builtin
            data["_file_path"] = str(path)
            styles[data["style_id"]] = data
        except Exception:
            pass
    return styles


def _load_all() -> dict[str, dict]:
    styles = _load_from_dir(DEFAULT_STYLES_DIR, is_builtin=True)
    styles.update(_load_from_dir(USER_STYLES_DIR, is_builtin=False))
    return styles


def _style_to_summary(s: dict) -> dict:
    return {
        "style_id": s["style_id"],
        "style_name": s["style_name"],
        "description": s["description"],
        "speaker_count": len(s["speakers"]),
        "section_count": len(s.get("structure", [])),
        "content_mode": s.get("content_mode", "long"),
        "line_count_mode": s.get("line_count_mode", "auto"),
        "series_mode": s.get("series_mode", False),
        "target_line_count": s.get("target_line_count", 30),
        "is_builtin": s.get("_is_builtin", True),
        "is_default": s.get("is_default", False),
        "speakers": [
            {
                "id": sp["id"],
                "name": sp["name"],
                "voice_id": sp.get("voice_id", ""),
                "character_id": sp.get("character_id", ""),
                "role": sp["role"],
                "tone": sp.get("tone", ""),
                "default_emotion": sp.get("default_emotion", "neutral"),
            }
            for sp in s["speakers"]
        ],
        "structure": s.get("structure", []),
    }


def list_styles() -> list[dict]:
    return [_style_to_summary(s) for s in _load_all().values()]


def get_style(style_id: str) -> Optional[dict]:
    return _load_all().get(style_id)


# ─── ユーザースタイル作成 ─────────────────────────────────────────────

def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_-]+", "_", text).strip("_")
    slug = re.sub(r"[^\x00-\x7F]", "", text).lower()
    # 日本語名は非ASCII除去後にほぼ空になる（例:「3章構成」→"3"）。
    # 短すぎるslugは他スタイルと衝突しやすい紛らわしいIDになるため時刻ベースにフォールバックする
    if len(slug) < 3:
        slug = f"style_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    return slug[:40]


def _auto_prompt_template(style_name: str, speaker_count: int) -> str:
    if speaker_count == 1:
        speakers_label = "ナレーター設定"
        example_line = "[SECTION:intro] [SPEAKER:speaker_a] [EMOTION:neutral] セリフ内容"
    else:
        speakers_label = "キャラクター設定"
        example_line = "[SECTION:intro] [SPEAKER:speaker_a] [EMOTION:happy] セリフ内容\n[SECTION:intro] [SPEAKER:speaker_b] [EMOTION:neutral] セリフ内容"

    return (
        f"あなたはYouTube動画の脚本ライターです。\n"
        f"以下の情報をもとに、「{{style_name}}」スタイルの台本を作成してください。\n\n"
        f"## {speakers_label}\n{{speakers_description}}\n\n"
        f"## 構成\n{{structure_description}}\n\n"
        f"## 入力情報\n{{input_content}}\n\n"
        f"## 出力形式\n各セリフを以下の形式で出力してください：\n"
        f"[SECTION:セクションID] [SPEAKER:話者ID] [EMOTION:感情] セリフ内容\n\n"
        f"例：\n{example_line}\n\n"
        f"## 注意事項\n"
        f"- 合計{{target_line_count}}行前後になるようにしてください\n"
        f"- 入力情報のすべてのトピックを台本に反映してください\n"
        f"- 各話者のトーンを忠実に守ってください\n"
        f"- 視聴者が飽きないようテンポよく進めてください"
    )


def create_user_style(
    style_name: str,
    description: str,
    speakers: list[dict],
    structure: list[dict],
    target_line_count: int,
    content_mode: str = "long",
    line_count_mode: str = "auto",
    series_mode: bool = False,
    is_default: bool = False,
    style_id: Optional[str] = None,
) -> dict:
    """ユーザー定義スタイルを shared/styles/ に保存して返す。"""
    _ensure_user_styles_dir()

    if not style_id:
        base = _slugify(style_name)
        style_id = base
        counter = 1
        while (USER_STYLES_DIR / f"{style_id}.json").exists():
            style_id = f"{base}_{counter}"
            counter += 1

    # 話者IDを正規化（speaker_a, speaker_b, ... に統一）
    normalized_speakers = []
    for i, sp in enumerate(speakers):
        sp_id = sp.get("id") or f"speaker_{'abcdefghij'[i]}"
        normalized_speakers.append({
            "id": sp_id,
            "name": sp.get("name", f"話者{i+1}"),
            "voice_id": sp.get("voice_id", ""),
            "character_id": sp.get("character_id", ""),  # キャラ本籍参照（任意・あれば名前/声/性格の出所）
            "role": sp.get("role", ""),
            "tone": sp.get("tone", ""),
            "default_emotion": sp.get("default_emotion", "neutral"),
        })

    # 構成セクションのIDを正規化
    normalized_structure = []
    for sec in structure:
        normalized_structure.append({
            "id": sec.get("id") or re.sub(r"\s+", "_", sec.get("label", "section")).lower(),
            "label": sec.get("label", ""),
            "description": sec.get("description", ""),
        })

    balance = {sp["id"]: round(1.0 / len(normalized_speakers), 2) for sp in normalized_speakers}

    style_data = {
        "style_id": style_id,
        "style_name": style_name,
        "description": description,
        "speakers": normalized_speakers,
        "structure": normalized_structure,
        "content_mode": content_mode if content_mode in ("short", "long") else "long",
        "line_count_mode": line_count_mode if line_count_mode in ("auto", "fixed") else "auto",
        "series_mode": series_mode,
        "target_line_count": target_line_count,
        "balance_ratio": balance,
        "emotions_allowed": ["happy", "neutral", "surprised", "sad", "angry", "thinking", "serious", "excited"],
        "is_default": is_default,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prompt_template": _auto_prompt_template(style_name, len(normalized_speakers)),
    }

    file_path = USER_STYLES_DIR / f"{style_id}.json"
    file_path.write_text(json.dumps(style_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return style_data


def import_user_style(style_data: dict) -> dict:
    """エクスポートされたスタイルJSONを新規ユーザースタイルとして取り込む。

    style_id は組み込み/既存ユーザースタイルと衝突しないよう自動採番し直す（上書きしない）。
    prompt_template・balance_ratio など元の全フィールドをそのまま保持する。
    """
    _ensure_user_styles_dir()
    builtin_ids = set(_load_from_dir(DEFAULT_STYLES_DIR, is_builtin=True).keys())

    base = style_data.get("style_id") or _slugify(style_data.get("style_name", "style"))
    style_id = base
    counter = 1
    while style_id in builtin_ids or (USER_STYLES_DIR / f"{style_id}.json").exists():
        style_id = f"{base}_{counter}"
        counter += 1

    imported = dict(style_data)
    imported["style_id"] = style_id
    imported["is_default"] = False  # 取り込みでアプリ全体の既定スタイルを変えない
    imported.setdefault("created_at", datetime.now(timezone.utc).isoformat())

    file_path = USER_STYLES_DIR / f"{style_id}.json"
    file_path.write_text(json.dumps(imported, ensure_ascii=False, indent=2), encoding="utf-8")
    return imported


def update_user_style(
    style_id: str,
    style_name: str,
    description: str,
    speakers: list[dict],
    structure: list[dict],
    target_line_count: int,
    content_mode: str = "long",
    line_count_mode: str = "auto",
    series_mode: bool = False,
    is_default: bool = False,
) -> Optional[dict]:
    """ユーザースタイルを上書き更新する。組み込みスタイルは不可（None を返す）。"""
    path = USER_STYLES_DIR / f"{style_id}.json"
    if not path.exists():
        return None
    # create_user_style を style_id 固定で呼び出す（上書き）
    return create_user_style(
        style_name=style_name,
        description=description,
        speakers=speakers,
        structure=structure,
        target_line_count=target_line_count,
        content_mode=content_mode,
        line_count_mode=line_count_mode,
        series_mode=series_mode,
        is_default=is_default,
        style_id=style_id,
    )


def copy_builtin_to_user(style_id: str) -> Optional[dict]:
    """組み込みスタイルを shared/styles/ にコピーして返す（編集の前処理）。"""
    original = _load_from_dir(DEFAULT_STYLES_DIR, is_builtin=True).get(style_id)
    if original is None:
        return None
    _ensure_user_styles_dir()
    new_id = f"{style_id}_custom"
    counter = 1
    while (USER_STYLES_DIR / f"{new_id}.json").exists():
        new_id = f"{style_id}_custom_{counter}"
        counter += 1
    copy = {k: v for k, v in original.items() if not k.startswith("_")}
    copy["style_id"] = new_id
    copy["style_name"] = original["style_name"] + "（カスタム）"
    copy["is_default"] = False
    copy["created_at"] = datetime.now(timezone.utc).isoformat()
    file_path = USER_STYLES_DIR / f"{new_id}.json"
    file_path.write_text(json.dumps(copy, ensure_ascii=False, indent=2), encoding="utf-8")
    return copy


def toggle_default(style_id: str, is_default: bool) -> bool:
    """ユーザースタイルの is_default フラグを切り替える。組み込みスタイルは不可。"""
    path = USER_STYLES_DIR / f"{style_id}.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    data["is_default"] = is_default
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def delete_user_style(style_id: str) -> bool:
    """ユーザースタイルを削除する。組み込みスタイルは削除不可。"""
    path = USER_STYLES_DIR / f"{style_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


# ─── プロンプトビルド ─────────────────────────────────────────────────

def resolve_speaker_fields(sp: dict) -> dict:
    """話者の実効的な表示名・役割・トーンを解決する。

    `character_id` があればキャラ本籍（shared/characters）から名前・性格を補完する
    （名前・声・性格はキャラが唯一の本籍＝スタイルに複製しない＝ドリフトなし）。
    `role`/`tone` はそのスタイル固有の演技指示として尊重し、キャラ性格は補足として連結する。
    character_id が無い旧スタイルは従来通り sp の name/tone をそのまま使う（後方互換）。
    """
    name = sp.get("name", "")
    persona = ""
    char_id = sp.get("character_id", "")
    if char_id:
        ch = character_reader.resolve_speaker(char_id)
        if ch:
            name = ch.get("name") or name
            persona = ch.get("description") or ""
    role = sp.get("role", "")
    tone = sp.get("tone", "")
    if persona and tone:
        effective_tone = f"{tone}（キャラ設定: {persona}）"
    elif persona:
        effective_tone = persona
    else:
        effective_tone = tone
    return {"name": name, "role": role, "tone": effective_tone}


def build_speakers_description(style: dict) -> str:
    # 話者IDを明示し、[SPEAKER:話者ID] には名前ではなくこのIDを使うようLLMに伝える
    # （IDで出力させることで比率判定・character_id 解決が確定的に効く）。
    lines = []
    for sp in style["speakers"]:
        r = resolve_speaker_fields(sp)
        lines.append(f"- 話者ID `{sp['id']}` = {r['name']}（{r['role']}）: {r['tone']}")
    return "\n".join(lines)


def build_structure_description(style: dict) -> str:
    lines = []
    for sec in style["structure"]:
        lines.append(f"- {sec['id']}（{sec['label']}）: {sec['description']}")
    return "\n".join(lines)


def _build_content_mode_instruction(style: dict) -> str:
    """content_mode / line_count_mode に応じたLLMへの出力方針指示を返す。

    line_count_mode:
      - "auto"  (デフォルト): 行数をLLMの判断に委ねる。「内容が自然に完結する長さ」を最優先。
      - "fixed": target_line_count を厳密な目標として指示する（オプション）。
    """
    mode = style.get("content_mode", "long")
    line_count_mode = style.get("line_count_mode", "auto")

    if mode == "short":
        if line_count_mode == "fixed":
            target = style.get("target_line_count", 30)
            min_lines = max(target - 2, 1)
            max_lines = target + 10
            return (
                f"## コンテンツモード: ショート動画（行数固定）\n"
                f"このスクリプトはYouTubeショート動画（60秒前後）用です。\n"
                f"### 行数ルール\n"
                f"- **{min_lines}行以上{max_lines}行以下**を目安に生成してください\n"
                f"### 最重要：物語の完結\n"
                f"- 必ず**起承転結が閉じた、単体で完結する話**にしてください\n"
                f"- 「次回予告」「続きはまた今度」のような続編を匂わせる終わり方は**絶対禁止**です\n"
                f"- 結論・オチ・締めの一言まで到達させてください\n"
                f"### コンテンツ方針\n"
                f"- 情報量よりテンポと面白さを優先してください\n"
                f"- 各話者に均等にセリフが回るようにしてください\n"
            )
        else:
            return (
                f"## コンテンツモード: ショート動画（自動）\n"
                f"このスクリプトはYouTubeショート動画（60秒前後）用です。\n"
                f"### 最重要：物語の完結\n"
                f"- 行数は固定しません。**内容が自然に完結する長さ**で生成してください\n"
                f"- 目安は20〜50行程度ですが、内容次第で前後して構いません\n"
                f"- 必ず**起承転結が閉じた、単体で完結する話**にしてください\n"
                f"- 「次回予告」「続きはまた今度」のような続編を匂わせる終わり方は**絶対禁止**です\n"
                f"- 入力情報のすべてを語ろうとせず、ショート動画として面白い部分を選び、その範囲内で結論まで到達させてください\n"
                f"### コンテンツ方針\n"
                f"- 情報量よりテンポと面白さを優先してください\n"
                f"- 詳細な説明は省き、視聴者の興味を引くポイントだけを残してください\n"
                f"- 各話者に均等にセリフが回るようにしてください\n"
            )
    else:
        if line_count_mode == "fixed":
            target = style.get("target_line_count", 60)
            return (
                f"## コンテンツモード: 長尺動画（行数固定）\n"
                f"このスクリプトは長時間動画用です。\n"
                f"- 入力情報に含まれるすべてのトピックを余すことなく扱ってください\n"
                f"- 各セクションを十分に展開し、{target}行前後になるよう生成してください\n"
                f"- 情報を省略・要約しないでください\n"
                f"- 必ず最後まで完結させてください（途中で終わらせない）\n"
            )
        else:
            return (
                f"## コンテンツモード: 長尺動画（自動）\n"
                f"このスクリプトは長時間動画用です。\n"
                f"### 方針\n"
                f"- 行数は固定しません。**入力情報を十分に深掘りできる長さ**で生成してください\n"
                f"- 各セクションを丁寧に展開し、情報を省略・要約しないでください\n"
                f"### 最重要：完結\n"
                f"- 必ず**最後のセクション（まとめ・アウトロ等）まで到達**させ、完結させてください\n"
                f"- 文の途中や話の途中で出力を止めないでください\n"
                f"- もし長くなりすぎると判断した場合は、各セクションの深掘り度合いを調整してでも、必ず最後まで書き切ってください\n"
            )


def build_prompt(style: dict, input_content: str) -> str:
    content_mode = style.get("content_mode", "long")
    line_count_mode = style.get("line_count_mode", "auto")
    target = style.get("target_line_count", 30 if content_mode == "short" else 60)

    # auto モードでは「行数を固定する」表現を避け、目安としての言い回しに置き換える
    if line_count_mode == "fixed":
        if content_mode == "short":
            min_lines = max(target - 2, 1)
            max_lines = target + 10
            line_count_str = f"{min_lines}〜{max_lines}（目安・必ず完結させること）"
        else:
            line_count_str = f"{target}前後（目安・必ず完結させること）"
    else:
        line_count_str = "内容が自然に完結する長さ（行数は固定しません。必ず完結させること）"

    template = style["prompt_template"]
    base = (
        template
        .replace("{style_name}", style["style_name"])
        .replace("{speakers_description}", build_speakers_description(style))
        .replace("{structure_description}", build_structure_description(style))
        .replace("{input_content}", input_content)
        .replace("{target_line_count}", line_count_str)
    )

    # content_mode 指示を先頭に挿入
    mode_instruction = _build_content_mode_instruction(style)
    prompt = mode_instruction + "\n" + base

    # 末尾にも完結リマインダーを追加（LLMは最後の指示を優先する傾向があるため）
    if content_mode == "short":
        prompt += (
            f"\n\n## 【最終確認・必読】\n"
            f"出力する前に確認してください。\n"
            f"- このスクリプトは**単体で完結**していますか？\n"
            f"- 「次回」「続き」を匂わせる終わり方になっていませんか？（なっていれば不合格です）\n"
            f"- 結論・オチ・締めの一言まで書けていますか？\n"
        )
    else:
        prompt += (
            f"\n\n## 【最終確認・必読】\n"
            f"出力する前に確認してください。\n"
            f"- 構成の最後のセクション（まとめ・アウトロ等）まで到達していますか？\n"
            f"- 文の途中で切れていませんか？\n"
            f"- 完結していない場合は、各セクションを調整してでも必ず最後まで書き切ってください\n"
        )

    return prompt
