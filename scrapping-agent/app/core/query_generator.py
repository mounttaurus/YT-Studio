"""
script.json のセクションごとに、素材検索用の英語キーワードをLLMで生成する。
結果は footage_draft.json の sections[].queries に保存される。
"""
import json
import re
from datetime import datetime, timezone
from typing import Optional

from app.core import llm_client

SYSTEM_PROMPT = (
    "You are an assistant that extracts stock-footage search queries from a Japanese video script. "
    "For each script section, output 2-4 short English search queries (1-3 words each) suitable for "
    "stock video/photo sites like Pexels. Focus on concrete visual subjects (objects, places, animals, "
    "actions), not abstract concepts. Respond with JSON only, no explanation."
)

PROMPT_TEMPLATE = """以下はYouTube動画の台本をセクションごとにまとめたものです。
各セクションの映像素材を探すための英語検索クエリを生成してください。

{extra}台本セクション:
{sections_text}

出力形式（JSONのみ）:
{{
  "sections": [
    {{"section": "セクション名", "summary": "そのセクションの映像イメージの一言要約（日本語）", "queries": ["query1", "query2", ...]}}
  ]
}}
"""


def group_lines_by_section(script: dict) -> list[dict]:
    """lines を section 出現順にグルーピングする。"""
    groups: list[dict] = []
    index: dict[str, dict] = {}
    for line in script.get("lines", []):
        sec = line.get("section") or "main"
        if sec not in index:
            g = {"section": sec, "line_ids": [], "texts": []}
            index[sec] = g
            groups.append(g)
        index[sec]["line_ids"].append(line.get("id"))
        index[sec]["texts"].append(line.get("text", ""))
    return groups


def _parse_llm_json(raw: str) -> dict:
    """LLM応答からJSON部分を取り出してパースする（コードフェンス対応）。"""
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"LLM応答にJSONが見つかりません: {raw[:200]}")
    return json.loads(text[start:end + 1])


async def generate_queries(
    script: dict,
    extra_prompt: Optional[str] = None,
    model: Optional[str] = None,
) -> list[dict]:
    """セクションごとの検索クエリリストを返す。

    Returns: [{"section", "line_ids", "summary", "queries"}, ...]
    """
    groups = group_lines_by_section(script)
    if not groups:
        return []

    sections_text = "\n\n".join(
        f"[{g['section']}]\n" + "\n".join(g["texts"]) for g in groups
    )
    extra = f"追加指示（最優先で考慮）: {extra_prompt}\n\n" if extra_prompt else ""

    raw = await llm_client.chat(
        PROMPT_TEMPLATE.format(extra=extra, sections_text=sections_text),
        model=model,
        system=SYSTEM_PROMPT,
    )
    parsed = _parse_llm_json(raw)

    llm_sections = {s.get("section"): s for s in parsed.get("sections", [])}
    result = []
    for g in groups:
        s = llm_sections.get(g["section"], {})
        result.append({
            "section": g["section"],
            "line_ids": g["line_ids"],
            "summary": s.get("summary", ""),
            "queries": [q for q in s.get("queries", []) if isinstance(q, str) and q.strip()],
        })
    return result


def build_draft(
    project_id: str,
    episode: int,
    sections: list[dict],
    extra_prompt: Optional[str] = None,
) -> dict:
    """footage_draft.json の初期構造を組み立てる。candidates/selectedは後続Stepで埋まる。"""
    return {
        "schema_version": "1.0.0",
        "project_id": project_id,
        "episode": episode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "extra_prompt": extra_prompt or "",
        "sections": [
            {**s, "candidates": [], "selected": []} for s in sections
        ],
    }
