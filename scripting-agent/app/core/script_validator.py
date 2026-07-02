"""
2パス目 — LLM出力をパースし、TTS向け script.json スキーマに変換・検証する。
ルールベースなので確定的に動く。
"""
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.core import character_reader


# 出力パーサー: [SECTION:x] [SPEAKER:y] [EMOTION:z] テキスト
LINE_PATTERN = re.compile(
    r"\[SECTION:(?P<section>\w+)\]\s*\[SPEAKER:(?P<speaker>\w+)\]\s*\[EMOTION:(?P<emotion>\w+)\]\s*(?P<text>.+)"
)

DEFAULT_PAUSE = 0.4
DEFAULT_SPEED = 1.0

VALID_EMOTIONS = {"happy", "neutral", "surprised", "sad", "angry", "thinking", "serious", "excited"}


def parse_llm_output(raw: str, style: dict) -> list[dict]:
    """LLM出力テキストを行リストに変換する。"""
    lines = []
    order = 1
    for raw_line in raw.strip().splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        m = LINE_PATTERN.match(raw_line)
        if not m:
            continue
        section = m.group("section")
        # LLMは話者IDの代わりにキャラ名を出すことがある（プロンプトが名前主体のため）。
        # 必ず正規の speaker_id へ寄せ、比率判定・character_id 解決が確定的に効くようにする。
        speaker_id = _normalize_speaker_id(m.group("speaker"), style)
        emotion = m.group("emotion").lower()
        text = m.group("text").strip()

        # スピーカー名を解決
        speaker_name = _resolve_speaker_name(speaker_id, style)
        # 感情を正規化
        if emotion not in VALID_EMOTIONS:
            emotion = "neutral"

        lines.append({
            "id": f"line_{order:03d}",
            "order": order,
            "speaker_id": speaker_id,
            "speaker_name": speaker_name,
            "text": text,
            "emotion": emotion,
            "speed": DEFAULT_SPEED,
            "pause_after_sec": DEFAULT_PAUSE,
            "section": section,
            "notes": "",
        })
        order += 1
    return lines


def _normalize_speaker_id(token: str, style: dict) -> str:
    """LLM出力の話者トークンをスタイルの正規 speaker_id へ寄せる。

    一致優先順位: ① 正規ID → ② スタイルの話者名 → ③ キャラ本籍の名前（character_id経由）。
    どれにも当たらなければトークンをそのまま返す（後方互換）。
    """
    speakers = style.get("speakers", [])
    # ① 正規ID一致
    for sp in speakers:
        if sp["id"] == token:
            return token
    low = token.strip().lower()
    # ② スタイル定義の話者名一致
    for sp in speakers:
        if (sp.get("name") or "").strip().lower() == low:
            return sp["id"]
    # ③ キャラ本籍の名前一致（character_id 経由＝名前の本籍はキャラ）
    for sp in speakers:
        char_id = sp.get("character_id", "")
        if char_id:
            ch = character_reader.resolve_speaker(char_id)
            if ch and (ch.get("name") or "").strip().lower() == low:
                return sp["id"]
    return token


def _resolve_speaker_name(speaker_id: str, style: dict) -> str:
    """script.json の speaker_name を解決する。

    character_id があればキャラ本籍の名前を優先（名前の本籍はキャラ＝ドリフトなし）。
    旧スタイル（character_id 無し）は従来通り style.speakers[].name を使う。
    """
    for sp in style["speakers"]:
        if sp["id"] == speaker_id:
            char_id = sp.get("character_id", "")
            if char_id:
                ch = character_reader.resolve_speaker(char_id)
                if ch and ch.get("name"):
                    return ch["name"]
            return sp.get("name") or speaker_id
    return speaker_id


def validate_and_fix(lines: list[dict], style: dict) -> tuple[list[dict], list[str]]:
    """
    構造チェックと自動修正を行う。
    Returns: (修正済み行リスト, 警告メッセージリスト)
    """
    warnings = []
    if not lines:
        warnings.append("台本が空です。")
        return lines, warnings

    # 話者バランスチェック（スタイルの speaker_id を使って柔軟に判定）
    balance = style.get("balance_ratio", {})
    total = len(lines)
    counts: dict[str, int] = {}
    for line in lines:
        counts[line["speaker_id"]] = counts.get(line["speaker_id"], 0) + 1

    # balance_ratio のキーがスタイルの speaker ID と一致する場合のみチェック
    style_speaker_ids = {sp["id"] for sp in style.get("speakers", [])}
    for sp_id, expected_ratio in balance.items():
        if sp_id not in style_speaker_ids:
            continue  # スタイルに存在しない speaker_id は無視
        actual = counts.get(sp_id, 0) / total if total > 0 else 0
        if abs(actual - expected_ratio) > 0.25:
            sp_name = next((sp["name"] for sp in style["speakers"] if sp["id"] == sp_id), sp_id)
            warnings.append(
                f"話者「{sp_name}」の比率が期待値({expected_ratio:.0%})から外れています（実際: {actual:.0%}）"
            )

    # 行数チェック — content_mode / line_count_mode ベース
    content_mode = style.get("content_mode", "long")
    line_count_mode = style.get("line_count_mode", "auto")

    if line_count_mode == "fixed":
        # 固定モード: target_line_count を基準にした厳密チェック
        if content_mode == "short":
            target_short = style.get("target_line_count", 30)
            min_short = max(target_short - 2, 1)
            max_short = target_short + 10
            if total < min_short:
                warnings.append(
                    f"⚠ ショートモードの行数が少なすぎます（{total}行 / 目安: {min_short}行以上）。"
                    f"フィードバックで「行数が足りません。{min_short}行以上になるよう再生成してください」と指示してください。"
                )
            elif total > max_short:
                warnings.append(f"ショートモードですが行数が多めです（{total}行）。目安は{min_short}〜{max_short}行です。")
        else:
            target = style.get("target_line_count", 60)
            if total < target * 0.6:
                warnings.append(f"行数が目標より少ない可能性があります（{total}行 / 目標{target}行）。フィードバックで再生成を試みてください。")
    else:
        # 自動モード: 厳密な行数は問わない。極端に短い/長い場合のみ緩やかに警告
        if content_mode == "short":
            if total < 12:
                warnings.append(f"⚠ ショート動画として行数が極端に少ないです（{total}行）。会話が成立していない可能性があります。フィードバックで「内容が薄いので、もう少し会話を膨らませて完結する形にしてください」と指示してみてください。")
            elif total > 70:
                warnings.append(f"ショート動画としては行数がやや多めです（{total}行）。長尺動画として再構成することも検討してください。")
        else:
            if total < 30:
                warnings.append(f"⚠ 長尺動画として行数が少なすぎる可能性があります（{total}行）。フィードバックで「内容が薄いので、各セクションをもっと深掘りしてください」と指示してみてください。")

    # セクション整合性チェック
    valid_sections = {sec["id"] for sec in style["structure"]}
    for line in lines:
        if line["section"] not in valid_sections:
            warnings.append(f"不明なセクション '{line['section']}' が line_{line['order']:03d} にあります")
            line["section"] = style["structure"][0]["id"]  # 先頭セクションにフォールバック

    return lines, warnings


def build_script_json(
    lines: list[dict],
    style: dict,
    project_id: str,
    llm_model: str,
) -> dict:
    """TTS向けの script.json を構築する。"""
    sections_used: dict[str, list[str]] = {}
    for line in lines:
        sec = line["section"]
        sections_used.setdefault(sec, [])
        sections_used[sec].append(line["id"])

    sections = [
        {"id": sec["id"], "label": sec["label"], "line_ids": sections_used.get(sec["id"], [])}
        for sec in style["structure"]
    ]

    estimated_duration = sum(
        max(len(line["text"]) / 5, 1) * (1.0 / line["speed"]) + line["pause_after_sec"]
        for line in lines
    )

    return {
        "schema_version": "1.0.0",
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_duration_sec": round(estimated_duration),
        "lines": lines,
        "sections": sections,
        "metadata": {
            "line_count": len(lines),
            "estimated_duration_sec": round(estimated_duration),
            "style": style["style_id"],
            "style_name": style.get("style_name", style["style_id"]),
            "content_mode": style.get("content_mode", "long"),
            "llm_model": llm_model,
            "checked_by_director": False,
            "check_passed": False,
            "check_notes": "",
        },
    }
