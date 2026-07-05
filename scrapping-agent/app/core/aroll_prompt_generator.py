"""
Aロール（マンガ形式パネル）用のプロンプト生成。

台本 script.json のセリフ行ごとに、マンガの1コマとして描く画像生成プロンプトを
LLMで生成する。章（section）単位で1回のLLM呼び出しにまとめる（30行→1コール、
レートセーフかつ前後の文脈で演出が繋がる）。

- 保存されるプロンプトは「演出部分（表情/ポーズ/ショット/構図）」のみ。
  スタイル接頭辞とキャラ外見は画像生成時に aroll_manager 側で合成する
  （後からスタイルを替えてもプロンプト再生成が不要）。
- LLMチェーン: 既定 gemini/gemini-2.5-flash（無料枠）→ OpenRouter Free Models Router。
  query_generator と同じ _parse_llm_json パターンでJSONを取り出す。
"""
import json
import os
import re
from typing import Optional

from app.core import llm_client
from app.core.query_generator import _parse_llm_json, group_lines_by_section

SYSTEM_PROMPT = (
    "You are a storyboard artist and prompt engineer for a manga-style YouTube video. "
    "For each dialogue line you write ONE image-generation prompt describing a single manga panel: "
    "the featured character(s) by name, their emotion, pose, camera shot (close-up / bust shot / "
    "waist-up / wide shot), camera angle, and a simple background. "
    "The image model also receives labeled reference images of the characters, so refer to characters "
    "by name only — never describe hair, face or clothing. "
    "Vary shots and angles across consecutive lines like a real manga page. "
    "Never include text, letters or speech bubbles in the image. "
    "Prompts must be in English. Respond with JSON only, no explanation."
)

PROMPT_TEMPLATE = """以下はYouTube動画の台本の1章分です。各セリフ行ごとに、マンガの1コマとして描く画像生成プロンプトを作ってください。

登場キャラクター（characters には char_id を使うこと）:
{characters_block}

ルール:
- "characters" は映すキャラの char_id を1〜2人。基本はその行の話者。会話の掛け合いで聞き手のリアクションや対面カット(two shot)が効果的な行では2人にする。
- "prompt" は英語。表情・ポーズ・ショット・アングル・簡素な背景を含める。髪型・服装・顔立ちは書かない（参照画像が担保する）。
- 吹き出しを後から載せるため、キャラを画面の片側に寄せて余白を作る構図指示を入れてよい。画像内に文字・吹き出しは絶対に描かせない。
- 連続する行で同じ構図を繰り返さない（ショット/アングルを切り替える）。
{extra}
台本（章: {section}）:
{lines_text}

出力形式（JSONのみ・全行分を line_id 順に）:
{{
  "panels": [
    {{"line_id": "line_001", "characters": ["char_id"], "prompt": "..."}}
  ]
}}
"""

# プロンプト生成LLMのチェーン（先頭から順に試す）。テキストのみなので無料枠で足りる。
DEFAULT_MODEL = os.getenv("AROLL_PROMPT_LLM", "gemini/gemini-2.5-flash")
FALLBACK_MODEL = os.getenv("AROLL_PROMPT_LLM_FALLBACK", "openrouter/openrouter/free")


def _model_available(model: str) -> bool:
    """モデルのAPIキーが設定されているかを判定する。"""
    if model.startswith("gemini/"):
        return bool(os.getenv("GEMINI_API_KEY"))
    if model.startswith("openrouter/"):
        return bool(os.getenv("OPENROUTER_API_KEY"))
    if model.startswith("anthropic/"):
        return bool(os.getenv("ANTHROPIC_API_KEY"))
    if model.startswith("openai/"):
        return bool(os.getenv("OPENAI_API_KEY"))
    return True


def _model_chain(model: Optional[str]) -> list[str]:
    chain = [model] if model else [DEFAULT_MODEL, FALLBACK_MODEL]
    if model and model != FALLBACK_MODEL:
        chain.append(FALLBACK_MODEL)
    return [m for m in chain if m and _model_available(m)]


def _normalize_characters(
    raw: list, known_chars: dict[str, dict], fallback_char_id: str,
) -> list[str]:
    """LLM出力のキャラ列をchar_idへ正規化する（名前ゆらぎ対応・最大2人）。

    known_chars: {char_id: {"name": ...}}。1人も解決できなければ話者のキャラへフォールバック。
    """
    out: list[str] = []
    for token in raw if isinstance(raw, list) else []:
        t = str(token).strip()
        if not t:
            continue
        if t in known_chars:
            resolved = t
        else:
            # 名前一致（完全→部分）で拾う
            resolved = next(
                (cid for cid, c in known_chars.items() if c.get("name") and c["name"] == t),
                None,
            ) or next(
                (cid for cid, c in known_chars.items()
                 if c.get("name") and (t in c["name"] or c["name"] in t)),
                None,
            )
        if resolved and resolved not in out:
            out.append(resolved)
        if len(out) >= 2:
            break
    if not out and fallback_char_id:
        out = [fallback_char_id]
    return out


async def generate_section_prompts(
    section: str,
    lines: list[dict],
    speaker_map: dict[str, dict],
    known_chars: dict[str, dict],
    extra_prompt: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[dict[str, dict], list[str]]:
    """1章分のセリフ→パネルプロンプトを生成する。

    lines: script.json の lines（この章のもの）
    speaker_map: {speaker_id: {"name", "character_id"}}
    known_chars: {char_id: {"name"}}
    Returns: ({line_id: {"characters", "prompt"}}, warnings)
    """
    characters_block = "\n".join(
        f"- char_id: {cid} / 名前: {c.get('name') or cid}"
        for cid, c in known_chars.items()
    ) or "- （キャラ未登録。charactersは空配列でよい）"

    lines_text = "\n".join(
        f"{ln.get('id')} [{speaker_map.get(ln.get('speaker_id'), {}).get('name') or ln.get('speaker_name') or ln.get('speaker_id')}] {ln.get('text', '')}"
        for ln in lines
    )
    extra = f"- 追加指示（最優先で考慮）: {extra_prompt}\n" if extra_prompt else ""

    prompt = PROMPT_TEMPLATE.format(
        characters_block=characters_block, section=section,
        lines_text=lines_text, extra=extra,
    )

    warnings: list[str] = []
    parsed = None
    last_err: Exception | None = None
    for m in _model_chain(model):
        try:
            raw = await llm_client.chat(prompt, model=m, system=SYSTEM_PROMPT, max_tokens=8192)
            parsed = _parse_llm_json(raw)
            break
        except Exception as e:  # 429/503/パース失敗 → 次のモデルへ
            last_err = e
            warnings.append(f"[{section}] {m} failed: {str(e)[:150]}")
    if parsed is None:
        raise RuntimeError(f"prompt generation failed for section '{section}': {last_err}")

    by_line: dict[str, dict] = {}
    for p in parsed.get("panels", []):
        lid = str(p.get("line_id", "")).strip()
        if not lid:
            continue
        by_line[lid] = p

    result: dict[str, dict] = {}
    for ln in lines:
        lid = ln.get("id")
        speaker = speaker_map.get(ln.get("speaker_id"), {})
        fallback_char = speaker.get("character_id") or ""
        p = by_line.get(lid)
        if p is None:
            warnings.append(f"[{section}] LLM応答に {lid} が無いためスキップ")
            continue
        chars = _normalize_characters(p.get("characters"), known_chars, fallback_char)
        text = str(p.get("prompt", "")).strip()
        if not text:
            warnings.append(f"[{section}] {lid} のpromptが空のためスキップ")
            continue
        result[lid] = {"characters": chars, "prompt": text}
    return result, warnings
