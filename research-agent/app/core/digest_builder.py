"""
核：取り込み済みソース → 読み込みポイント整理 → 目標尺に応じた構成 → 1本のラフ台本。

Gemini の長文脈に全ソースを丸ごと載せ、低温度で「要点抽出＋尺別構成＋ラフ台本」を一度に作らせる。
出力は JSON（reading_points / outline / rough_script / title_suggestions）。
rough_script は scripting-agent へ渡す実体、それ以外は研究の来歴(research.json)。
"""
import json
import re
from datetime import datetime, timezone

from app.core import llm_client, project_manager

SYSTEM = (
    "あなたは動画制作のリサーチ編集者です。与えられた複数の素材（ドキュメント・記事・"
    "書き起こし）を読み込み、要点を整理して、脚本家がそのまま執筆に入れる『ラフ台本』を作ります。"
    "あなたは確定したセリフは書きません（それは脚本家の仕事）。構成・要点・根拠・語り口の方向性を示します。"
    "必ず素材に基づき、推測で事実を作りません。出力は指定のJSONのみ。"
)


def _duration_guidance(sec: int) -> str:
    if sec <= 120:
        return (
            f"目標尺は約{sec}秒（短尺）。要点を絞り込み、3ビート構成"
            "（フック→本題1〜2点→締め）に圧縮する。冗長な背景説明は捨て、最も刺さる読み込みポイントだけ残す。"
        )
    if sec <= 420:
        return (
            f"目標尺は約{sec}秒（標準）。導入＋本論3〜4章＋まとめ。各章に要点と根拠を割り当て、"
            "尺配分(target_sec)を合計が目標尺に近づくよう決める。"
        )
    return (
        f"目標尺は約{sec}秒（長尺）。導入＋本論5章以上＋まとめの章立てで深掘りする。"
        "各章に小見出しと複数の読み込みポイント・根拠を割り当て、章間の流れ（つなぎ）も示す。"
    )


def _build_sources_block(sources: list[dict]) -> str:
    parts = []
    for s in sources:
        text = (s.get("text") or "").strip()
        if not text and s.get("url"):
            text = f"(本文未取得・参照URL: {s['url']})"
        header = f"[{s['id']}] 種別:{s.get('kind')} タイトル:{s.get('title')}"
        if s.get("url"):
            header += f" URL:{s['url']}"
        parts.append(f"{header}\n{text}")
    return "\n\n===== ソース区切り =====\n\n".join(parts)


def _strip_json(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 最初の { から最後の } まで
    a, b = raw.find("{"), raw.rfind("}")
    if a != -1 and b != -1 and b > a:
        return raw[a:b + 1]
    return raw


def _render_rough_script(parsed: dict, target_sec: int) -> str:
    """JSONの構成を、人が読める rough_script.txt 本文へ展開する。"""
    lines: list[str] = []
    titles = parsed.get("title_suggestions") or []
    if titles:
        lines.append("# タイトル案")
        lines.extend(f"- {t}" for t in titles)
        lines.append("")
    lines.append(f"# ラフ台本（目標尺 約{target_sec}秒）")
    overview = parsed.get("overview")
    if overview:
        lines.append(overview)
    lines.append("")
    for sec in parsed.get("outline") or []:
        head = f"## {sec.get('order')}. {sec.get('section')}"
        if sec.get("target_sec"):
            head += f"（約{sec['target_sec']}秒）"
        lines.append(head)
        if sec.get("beat"):
            lines.append(f"狙い: {sec['beat']}")
        for pt in sec.get("points") or []:
            lines.append(f"- {pt}")
        if sec.get("tone"):
            lines.append(f"語り口: {sec['tone']}")
        if sec.get("sources"):
            lines.append(f"根拠: {', '.join(sec['sources'])}")
        lines.append("")
    body = parsed.get("rough_script")
    if body:
        lines.append("# 本文ドラフト（脚本家への素案）")
        lines.append(body)
    return "\n".join(lines).strip()


async def build(
    project_id: str,
    target_duration_sec: int = 300,
    model: str | None = None,
    extra_instruction: str | None = None,
) -> dict:
    sources = project_manager.load_sources(project_id)
    if not sources:
        raise ValueError("ソースが1件もありません。先にドキュメント/テキスト/URLを取り込んでください。")

    prompt = f"""以下の素材から、目標尺に合わせたラフ台本を作ってください。

## 尺の指示
{_duration_guidance(target_duration_sec)}

## 追加指示
{extra_instruction or "（特になし）"}

## 素材（[S1]等のIDで引用すること）
{_build_sources_block(sources)}

## 出力フォーマット（このJSONだけを出力。コメント/前置き禁止）
{{
  "title_suggestions": ["タイトル案を2〜3個"],
  "overview": "動画全体の主旨を2〜3文で",
  "reading_points": [
    {{"text": "重要な読み込みポイント", "sources": ["S1","S2"]}}
  ],
  "outline": [
    {{"order": 1, "section": "導入", "beat": "この章の狙い", "target_sec": 30,
      "points": ["話す要点"], "tone": "語り口の方向性", "sources": ["S1"]}}
  ],
  "rough_script": "章立てに沿った本文ドラフト（確定セリフではなく素案。話者割りは未確定でよい）"
}}
"""

    raw, fallback_used = await llm_client.chat(
        prompt,
        model=model,
        system=SYSTEM,
        temperature=0.4,
        max_tokens=8192,
    )

    try:
        parsed = json.loads(_strip_json(raw))
    except json.JSONDecodeError:
        # 構造化に失敗しても生成テキストは活かす（rough_script に丸ごと入れる）
        parsed = {"title_suggestions": [], "overview": "", "reading_points": [],
                  "outline": [], "rough_script": raw}

    rough_text = _render_rough_script(parsed, target_duration_sec)
    project_manager.save_rough_script(project_id, rough_text)

    research = {
        "schema_version": project_manager.RESEARCH_SCHEMA_VERSION,
        "project_id": project_id,
        "status": "done",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_duration_sec": target_duration_sec,
        "engine": {
            "model": model or llm_client.DEFAULT_MODEL,
            "fallback_used": fallback_used,
        },
        "sources": [
            {"id": s["id"], "kind": s.get("kind"), "title": s.get("title"),
             "url": s.get("url"), "chars": s.get("chars", 0), "added_at": s.get("added_at")}
            for s in sources
        ],
        "reading_points": parsed.get("reading_points", []),
        "outline": parsed.get("outline", []),
        "rough_script_chars": len(rough_text),
    }
    project_manager.save_research(project_id, research)

    return {"research": research, "rough_script": rough_text, "fallback_used": fallback_used}
