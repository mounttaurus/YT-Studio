"""
AI自動選択 — 候補素材から、セクションの尺と台本の意味に合うものをLLMに選ばせる。
選択結果はチェック状態としてUIに反映され、最終確定（ダウンロード）は人間の承認で行う。
"""
import json
from typing import Optional

from app.core import llm_client, project_manager, query_generator

# 静止画1枚を何秒の尺としてカウントするか（編集時の一般的なカット尺）
PHOTO_CLIP_SEC = 5.0
# 日本語セリフの読み上げ速度の概算（tts.jsonが無い場合のフォールバック）
CHARS_PER_SEC = 6.5

SYSTEM_PROMPT = (
    "You are a video editing assistant. Given script sections with target durations and "
    "candidate footage clips, select which candidates to use for each section. "
    "Selection criteria: (1) semantic closeness between the candidate's search query and the "
    "section's script content, (2) total selected footage duration should be roughly 100-150% "
    "of the section's target duration — having extra material is better than too little. "
    f"Still images count as {PHOTO_CLIP_SEC} seconds each. "
    "Respond with JSON only, no explanation."
)

PROMPT_TEMPLATE = """以下はYouTube動画の各セクションと、収集済みの素材候補一覧です。
各セクションで使う素材を選んでください。

{sections_text}

出力形式（JSONのみ）:
{{
  "sections": [
    {{"section": "セクション名", "candidate_ids": ["id1", "id2", ...]}}
  ]
}}
"""


def _section_durations(script: dict, tts: dict | None) -> dict[str, float]:
    """セクションごとの目標尺（音声尺）を返す。tts.jsonがあれば実測、無ければ文字数から概算。"""
    durations: dict[str, float] = {}
    tts_by_line = {}
    if tts:
        tts_by_line = {a["line_id"]: a for a in tts.get("audio_files", [])}
    for line in script.get("lines", []):
        sec = line.get("section") or "main"
        entry = tts_by_line.get(line.get("id"))
        if entry and entry.get("duration_sec"):
            d = float(entry["duration_sec"]) + float(line.get("pause_after_sec") or 0)
        else:
            d = len(line.get("text", "")) / CHARS_PER_SEC + float(line.get("pause_after_sec") or 0)
        durations[sec] = durations.get(sec, 0.0) + d
    return {k: round(v, 1) for k, v in durations.items()}


def _build_prompt(draft: dict, script: dict, durations: dict[str, float]) -> str:
    lines_by_section: dict[str, list[str]] = {}
    for line in script.get("lines", []):
        lines_by_section.setdefault(line.get("section") or "main", []).append(line.get("text", ""))

    blocks = []
    for s in draft.get("sections", []):
        name = s["section"]
        cands = s.get("candidates", [])
        if not cands:
            continue
        cand_lines = "\n".join(
            f"  - {c['candidate_id']} | {c['media_type']} | "
            f"{c.get('duration_sec', 0) or PHOTO_CLIP_SEC}s | query: {c.get('query', '')}"
            for c in cands
        )
        script_text = " ".join(lines_by_section.get(name, []))[:500]
        blocks.append(
            f"[{name}] 目標尺: {durations.get(name, 0)}秒\n"
            f"台本: {script_text}\n"
            f"候補:\n{cand_lines}"
        )
    return PROMPT_TEMPLATE.format(sections_text="\n\n".join(blocks))


async def auto_select(
    project_id: str,
    episode_number: int,
    draft: dict,
    model: Optional[str] = None,
) -> dict:
    """LLMによる候補自動選択。{"selections": [{"section", "candidate_ids"}], "targets": {...}} を返す。"""
    script = project_manager.get_episode_script(project_id, episode_number)
    if script is None:
        raise ValueError("approved script.json not found")

    ep_dir = project_manager.episode_dir(project_id, episode_number)
    tts = None
    if ep_dir is not None and (ep_dir / "tts.json").exists():
        tts = json.loads((ep_dir / "tts.json").read_text(encoding="utf-8"))

    durations = _section_durations(script, tts)
    raw = await llm_client.chat(
        _build_prompt(draft, script, durations),
        model=model,
        system=SYSTEM_PROMPT,
        max_tokens=4096,
    )
    parsed = query_generator._parse_llm_json(raw)

    # LLMが返したIDのうち、実在する候補のみ採用する
    valid_ids = {
        s["section"]: {c["candidate_id"] for c in s.get("candidates", [])}
        for s in draft.get("sections", [])
    }
    selections = []
    for s in parsed.get("sections", []):
        name = s.get("section")
        if name not in valid_ids:
            continue
        # 実在IDのみ・重複排除（LLMが同一IDを複数回返すことがある）
        ids = list(dict.fromkeys(
            cid for cid in s.get("candidate_ids", []) if cid in valid_ids[name]
        ))
        if ids:
            selections.append({"section": name, "candidate_ids": ids})
    return {"selections": selections, "targets": durations, "tts_based": tts is not None}
