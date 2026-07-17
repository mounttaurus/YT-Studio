"""
2パス生成エンジン。
PASS1: LLMでドラフト生成（長尺はチャンク分割生成）
PASS2: script_validator でパース・検証・自動修正
"""
from typing import Optional

from app.core import llm_client, style_registry, script_validator

SCRIPT_SYSTEM_PROMPT = "あなたはYouTube動画の脚本専門家です。指定されたフォーマットを厳密に守って出力してください。"

# 長尺チャンク生成: 1チャンクに含む構成セクション数の目安
LONG_CHUNK_SECTION_SIZE = 3
# チャンク分割生成を行う最低セクション数（これ未満は一発生成）
LONG_CHUNK_MIN_SECTIONS = 4


async def generate(
    input_content: str,
    style_id: str,
    project_id: str,
    model: Optional[str] = None,
    feedback: Optional[str] = None,
    extra_instruction: Optional[str] = None,
    target_line_count: Optional[int] = None,
) -> tuple[dict, list[str]]:
    """
    台本を生成する。
    Returns: (script_json, warnings)

    extra_instruction: フィードバックとは別に、生成時にプロンプトへ追加する指示
                       （シリーズ生成時の継続性指示などに使用）。
    target_line_count: 指定時はこの話だけスタイル既定の行数を上書きする（line_count_mode を
                       fixed に昇格＝プロンプト/検証の双方へ反映）。未指定ならスタイル既定のまま。
    """
    style = style_registry.get_style(style_id)
    if style is None:
        raise ValueError(f"スタイル '{style_id}' が見つかりません")

    # 行数の話単位オーバーライド（スタイル本体は変更しない＝浅コピーに対してのみ適用）。
    if target_line_count is not None:
        style = {**style, "target_line_count": int(target_line_count), "line_count_mode": "fixed"}

    model = model or llm_client.get_default_model()

    content_mode = style.get("content_mode", "long")
    structure = style.get("structure", [])

    use_chunked = content_mode == "long" and len(structure) >= LONG_CHUNK_MIN_SECTIONS

    if use_chunked:
        lines = await _generate_long_chunked(style, input_content, model, feedback, extra_instruction)
    else:
        # 通常の一発生成（ショート / セクション数が少ない長尺）
        prompt = style_registry.build_prompt(style, input_content)
        if extra_instruction:
            prompt += f"\n\n{extra_instruction}"
        if feedback:
            prompt += f"\n\n## 前回のフィードバック\n{feedback}\n上記のフィードバックを反映して修正してください。"

        raw_output = await llm_client.chat(
            prompt=prompt,
            model=model,
            system=SCRIPT_SYSTEM_PROMPT,
        )
        lines = script_validator.parse_llm_output(raw_output, style)

    # PASS 2: パース・検証・自動修正
    lines, warnings = script_validator.validate_and_fix(lines, style)
    script_json = script_validator.build_script_json(lines, style, project_id, model)

    return script_json, warnings


# ─── 長尺チャンク分割生成 ─────────────────────────────────────────────

async def _generate_long_chunked(
    style: dict,
    input_content: str,
    model: str,
    feedback: Optional[str] = None,
    extra_instruction: Optional[str] = None,
) -> list[dict]:
    """
    長尺台本を構成セクションのグループ単位に分割し、複数回のLLM呼び出しで生成する。
    一発生成では LLM の出力トークン上限により台本が途中で切れてしまうため、
    パートごとに完結した呼び出しを行い、結合することで全体を完成させる。
    """
    structure = style.get("structure", [])
    groups = [
        structure[i:i + LONG_CHUNK_SECTION_SIZE]
        for i in range(0, len(structure), LONG_CHUNK_SECTION_SIZE)
    ]
    total_parts = len(groups)

    all_lines: list[dict] = []
    prev_summary = ""
    order_counter = 0

    for idx, group in enumerate(groups):
        is_first = idx == 0
        is_last = idx == total_parts - 1

        chunk_prompt = _build_chunk_prompt(
            style=style,
            input_content=input_content,
            group=group,
            part_index=idx + 1,
            total_parts=total_parts,
            is_first=is_first,
            is_last=is_last,
            prev_summary=prev_summary,
        )
        if extra_instruction and is_first:
            chunk_prompt += f"\n\n{extra_instruction}"
        if feedback and is_first:
            chunk_prompt += (
                f"\n\n## 前回のフィードバック\n{feedback}\n"
                f"上記のフィードバックを台本全体に反映してください。"
            )
        # 最終パートにも継続性指示（次回予告禁止・完結など）を再掲する
        if extra_instruction and is_last and extra_instruction not in chunk_prompt:
            chunk_prompt += f"\n\n## シリーズ継続性に関する補足（このパートにも適用）\n{extra_instruction}"

        raw_output = await llm_client.chat(
            prompt=chunk_prompt,
            model=model,
            system=SCRIPT_SYSTEM_PROMPT,
        )
        chunk_lines = script_validator.parse_llm_output(raw_output, style)

        # order / id を全体通しの連番に振り直す
        for line in chunk_lines:
            order_counter += 1
            line["order"] = order_counter
            line["id"] = f"line_{order_counter:03d}"

        all_lines.extend(chunk_lines)

        # 次パート用の継続コンテキスト（直近の数行を会話の流れとして渡す）
        if chunk_lines:
            tail = chunk_lines[-3:]
            prev_summary = "\n".join(f"[{l['speaker_name']}]「{l['text']}」" for l in tail)

    return all_lines


def _build_chunk_prompt(
    style: dict,
    input_content: str,
    group: list[dict],
    part_index: int,
    total_parts: int,
    is_first: bool,
    is_last: bool,
    prev_summary: str,
) -> str:
    """長尺チャンク生成の1パート分のプロンプトを構築する。"""
    speakers_desc = style_registry.build_speakers_description(style)
    section_desc = "\n".join(
        f"- {sec['id']}（{sec['label']}）: {sec['description']}" for sec in group
    )
    section_ids = "/".join(sec["id"] for sec in group)

    parts = [
        "あなたはYouTube動画の脚本ライターです。",
        f"以下の情報をもとに、「{style['style_name']}」スタイルの台本のうち、"
        f"パート{part_index}/{total_parts}（担当セクション: {section_ids}）を作成してください。",
        "",
        "## キャラクター設定",
        speakers_desc,
        "",
        f"## このパートで担当する構成セクション（パート{part_index}/{total_parts}）",
        section_desc,
        "",
        "## 入力情報（台本全体で使う情報。このパートに関連する部分を中心に扱ってください）",
        input_content,
        "",
        "## 出力形式",
        "各セリフを以下の形式で出力してください：",
        "[SECTION:セクションID] [SPEAKER:話者ID] [EMOTION:感情] セリフ内容",
        "",
        "例：",
        f"[SECTION:{group[0]['id']}] [SPEAKER:{style['speakers'][0]['id']}] [EMOTION:neutral] セリフの内容がここに入ります。",
    ]

    if not is_first and prev_summary:
        parts += [
            "",
            "## 直前のパートの最後の数行（これまでの流れ）",
            prev_summary,
            "",
            "上記の続きとして自然につながるように書いてください。"
            "イントロや自己紹介を繰り返さず、話を蒸し返さないでください。",
        ]

    parts.append("")
    parts.append("## 注意事項（重要）")
    if is_first:
        parts += [
            "- このパートは台本の冒頭です。視聴者を引き込む導入から始めてください。",
            "- このパートの最後で、次のパートに自然につながる流れを作ってください（ただし「次回」を匂わせる締め方はしないでください。台本全体は1本の動画として完結させます）。",
        ]
    elif is_last:
        parts += [
            "- このパートは台本の最後です。**必ずまとめ・締めまで到達させ、台本全体を完結させてください**。",
            "- 文の途中で出力を止めないでください。",
            "- 「次回」「続きはまた今度」のような続編を匂わせる終わり方は禁止です。",
        ]
    else:
        parts += [
            "- このパートは台本の中間部分です。前のパートからの続きとして自然につなげてください。",
            "- このパート単体で各セクションの内容が完結するように書いてください（次のパートへの橋渡しも意識してください）。",
        ]

    parts += [
        f"- 担当セクション（{section_ids}）の説明にある行数の目安を踏まえつつ、内容を十分に深掘りしてください",
        "- 入力情報のうち、このパートのセクションに関連するトピック・数字・事実を省略しないでください",
        "- 各話者のトーンを忠実に守り、視聴者が飽きないようテンポよく進めてください",
        f"- このパートだけで合計15〜25行程度を目安に出力してください（多少前後しても構いません）",
    ]

    return "\n".join(parts)


# ─── シリーズ生成 ─────────────────────────────────────────────────────
# 情報量の多いラフ台本を複数話に分割し、各話を独立した script_json として生成する。
# - content_mode == "short" のシリーズスタイル → 各話は完結型ショート動画
# - content_mode == "long"  のシリーズスタイル → 各話は前編/中編/後編のような長尺動画
# いずれも内部的には通常の generate() を再利用し、話数分だけ繰り返す。

import json
import re


async def generate_series(
    input_content: str,
    style_id: str,
    project_id: str,
    model: Optional[str] = None,
    episode_count: Optional[int] = None,
    user_instruction: Optional[str] = None,
) -> list[tuple[int, dict, list[str]]]:
    """
    シリーズ台本を生成する。
    Returns: [(episode_number, script_json, warnings), ...]
    """
    style = style_registry.get_style(style_id)
    if style is None:
        raise ValueError(f"スタイル '{style_id}' が見つかりません")

    model = model or llm_client.get_default_model()

    # Step 1: 入力情報を話数分の構成案に分割するプランを立てる
    plan = await _plan_series_episodes(input_content, style, model, episode_count, user_instruction)
    total = len(plan)

    results: list[tuple[int, dict, list[str]]] = []
    prev_recap = ""

    for ep in plan:
        number = ep["number"]
        is_first = number == 1
        is_last = number == total

        continuity = _build_series_continuity_instruction(ep, total, is_first, is_last, prev_recap)
        if user_instruction:
            continuity += f"\n\n## ユーザー追加指示\n{user_instruction}"

        ep_input = (
            input_content
            + f"\n\n## このエピソード（第{number}話「{ep['title']}」）で重点的に扱うべき内容\n{ep['summary']}\n"
            f"※ 入力情報全体の中から、上記に関連する部分を中心に扱ってください。"
        )

        script_json, warnings = await generate(
            input_content=ep_input,
            style_id=style_id,
            project_id=project_id,
            model=model,
            extra_instruction=continuity,
        )

        script_json.setdefault("metadata", {})["series"] = {
            "episode_number": number,
            "episode_title": ep["title"],
            "total_episodes": total,
        }

        results.append((number, script_json, warnings))

        # 次話用の継続コンテキスト（このエピソード末尾の数行を「前回の場面」として渡す）
        lines = script_json.get("lines", [])
        if lines:
            tail = lines[-3:]
            prev_recap = "\n".join(f"[{l['speaker_name']}]「{l['text']}」" for l in tail)

    return results


async def _plan_series_episodes(
    input_content: str,
    style: dict,
    model: str,
    episode_count: Optional[int] = None,
    user_instruction: Optional[str] = None,
) -> list[dict]:
    """入力情報をシリーズの話数構成案に分割する（LLMによるプランニング呼び出し）。

    user_instruction（ユーザーが直接指定したテーマ等）は、入力情報(リサーチ/ラフ台本)が無い/薄い
    場合でも構成案に反映されるよう、ここで input_content に明示的に合成する。
    """
    content_mode = style.get("content_mode", "long")
    if user_instruction:
        input_content = input_content + f"\n\n## ユーザー指定のテーマ・指示\n{user_instruction}"

    if episode_count:
        count_instruction = f"必ずちょうど{episode_count}話に分割してください。"
    elif content_mode == "short":
        count_instruction = (
            "入力情報の分量に応じて、3〜6話程度に分割してください"
            "（各話は単体で楽しめる完結型のショート動画になります）。"
        )
    else:
        count_instruction = (
            "入力情報の分量に応じて、2〜3話程度（前編・中編・後編、または前編・後編）に分割してください"
            "（各話は1本の長時間動画として完結します）。"
        )

    plan_prompt = (
        "あなたはYouTubeシリーズ動画の構成プランナーです。\n"
        "以下の入力情報を、シリーズ動画として配信するための話数構成案に分割してください。\n\n"
        f"## 入力情報\n{input_content}\n\n"
        f"## 分割方針\n{count_instruction}\n"
        "- 各話は独立したテーマ・トピックの範囲を担当し、内容が重複しすぎないようにしてください\n"
        "- 全体を通して見たときに、入力情報の主要なトピックが網羅されるようにしてください\n"
        "- 各話のタイトルと、その話で扱う内容の要約（3〜5文程度）を作成してください\n\n"
        "## 出力形式\n"
        "以下のJSON配列のみを出力してください（説明文・コードブロック記号など余計なものは一切含めないでください）：\n"
        '[{"number": 1, "title": "話のタイトル", "summary": "この話で扱う内容の要約"}, '
        '{"number": 2, "title": "話のタイトル", "summary": "..."}]\n'
    )

    raw = await llm_client.chat(
        prompt=plan_prompt,
        model=model,
        system="あなたはシリーズ構成プランナーです。指示されたJSON配列のみを出力してください。前置きや説明は一切不要です。",
    )
    plan = _parse_series_plan(raw)
    return plan


def _parse_series_plan(raw: str) -> list[dict]:
    """LLMが返したシリーズ構成案（JSON）をパースする。失敗時は単話扱いにフォールバックする。"""
    text = raw.strip()
    # ```json ... ``` のようなコードブロック記号を除去
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        data = json.loads(text)
        if isinstance(data, list) and len(data) > 0:
            plan = []
            for i, ep in enumerate(data):
                if not isinstance(ep, dict):
                    continue
                plan.append({
                    "number": int(ep.get("number", i + 1)),
                    "title": str(ep.get("title") or f"第{i + 1}話"),
                    "summary": str(ep.get("summary") or ""),
                })
            if plan:
                # number で昇順に並べ直し、欠番があれば振り直す
                plan.sort(key=lambda e: e["number"])
                for i, ep in enumerate(plan):
                    ep["number"] = i + 1
                return plan
    except Exception:
        pass

    # フォールバック: プランニングに失敗した場合は単話として扱う
    return [{"number": 1, "title": "第1話", "summary": ""}]


def _build_series_continuity_instruction(
    ep: dict,
    total: int,
    is_first: bool,
    is_last: bool,
    prev_recap: str,
) -> str:
    """シリーズの各話に挿入する継続性指示（あらすじ・次回予告など）を構築する。"""
    lines = [
        "## シリーズ構成について（重要・必読）",
        f"この台本はシリーズ動画の第{ep['number']}話/{total}話「{ep['title']}」です。",
        f"この話で扱うべき内容: {ep['summary']}",
    ]

    if is_first:
        lines += [
            "",
            "### この話（第1話）の方針",
            "- シリーズの第1話です。初めて見る視聴者でも理解できる導入から始めてください。",
            "- この話自体は完結させつつ、最後に次回（第2話）への興味を引く一言を添えてください"
            "（内容を言い切らず、続きが気になる形で終えてください）。",
        ]
    elif is_last:
        lines += [
            "",
            "### この話（最終話）の方針",
            "- シリーズの最終話です。",
            "- 冒頭で「前回までのあらすじ」を簡潔に振り返ってください。前回の最後の場面は以下の通りです：",
            f"  {prev_recap or '（前回の情報なし）'}",
            "- 動画の最後はシリーズ全体の締めくくりとして完結させてください（次回予告は不要・禁止です）。",
        ]
    else:
        lines += [
            "",
            f"### この話（第{ep['number']}話・中間話）の方針",
            "- 冒頭で「前回までのあらすじ」を簡潔に振り返ってください。前回の最後の場面は以下の通りです：",
            f"  {prev_recap or '（前回の情報なし）'}",
            "- この話自体は完結させつつ、最後に次回への興味を引く一言を添えてください。",
        ]


# ─── 行単位の再生成 ─────────────────────────────────────────────────────
# 台本全体を作り直すregenerate()と異なり、指定した行だけをLLMに書き直させ、
# それ以外の行（順序・話者・セクション含む）には一切触れない。台本全文を再生成
# コンテキストとして渡すため文脈は保たれる（飛び飛びの行選択でも成立する）。

LINE_ID_PATTERN = re.compile(
    r"\[LINE_ID:(?P<id>\S+)\]\s*\[EMOTION:(?P<emotion>[^\]]+)\]\s*(?P<text>.+)"
)


async def regenerate_lines(
    project_id: str,
    episode_number: int,
    line_ids: list[str],
    feedback: str,
    model: Optional[str] = None,
) -> tuple[dict, list[str], list[str]]:
    """既存ドラフトの指定行だけをLLMで書き直す（他行は変更しない）。

    Returns: (更新後のscript_json, warnings, 実際に書き換えられたline_idのリスト)
    """
    from app.core import project_manager

    draft = project_manager.read_draft(project_id, episode_number)
    if draft is None:
        raise ValueError(f"第{episode_number}話のドラフトが見つかりません")

    lines = draft.get("lines", [])
    by_id = {l["id"]: l for l in lines}
    missing = [lid for lid in line_ids if lid not in by_id]
    if missing:
        raise ValueError(f"指定された行が見つかりません: {', '.join(missing)}")

    model = model or llm_client.get_default_model()

    # スタイルが分かれば話者トーンを渡す（import_script由来の台本はstyle_idを持たないことがあるため任意）。
    style_id = draft.get("metadata", {}).get("style")
    style = style_registry.get_style(style_id) if style_id else None
    speaker_desc = ""
    if style:
        speaker_desc = "\n".join(
            f"- {sp['id']}（{sp.get('name', '')}）: {sp.get('tone', '')}"
            for sp in style.get("speakers", [])
        )

    script_dump = "\n".join(
        f"[LINE_ID:{l['id']}] [SPEAKER:{l['speaker_id']}] [SECTION:{l.get('section', '')}] {l['text']}"
        for l in lines
    )

    prompt_parts = [
        "以下はYouTube動画台本の全文です（話の流れを理解するために渡します。"
        "指定されたline_id以外の行は絶対に変更しないでください）。",
        "",
        script_dump,
        "",
    ]
    if speaker_desc:
        prompt_parts += [f"## 話者のトーン\n{speaker_desc}", ""]
    prompt_parts += [
        f"## 書き換え対象\n次のline_idの行だけを、下記の指示に従って書き直してください: {', '.join(line_ids)}",
        "",
        f"## 指示\n{feedback}",
        "",
        "## 出力形式\n対象の行だけを、次の形式で出力してください（他の行は一切出力しない。"
        "話者・セクションは変更しない・書き直した行数は対象と同じにする）:",
        "[LINE_ID:xxx] [EMOTION:感情] セリフ内容",
        "EMOTIONは次のいずれか1語のみ使用してください（日本語の説明語や複合語は不可）: "
        + ", ".join(sorted(script_validator.VALID_EMOTIONS)),
    ]

    raw_output = await llm_client.chat(
        prompt="\n".join(prompt_parts), model=model, system=SCRIPT_SYSTEM_PROMPT,
    )

    rewritten: dict[str, dict] = {}
    for raw_line in raw_output.strip().splitlines():
        raw_line = raw_line.strip()
        m = LINE_ID_PATTERN.match(raw_line)
        if not m or m.group("id") not in by_id:
            continue
        emotion = m.group("emotion").lower()
        if emotion not in script_validator.VALID_EMOTIONS:
            emotion = "neutral"
        rewritten[m.group("id")] = {"text": m.group("text").strip(), "emotion": emotion}

    warnings: list[str] = []
    applied: list[str] = []
    for lid in line_ids:
        if lid in rewritten:
            by_id[lid]["text"] = rewritten[lid]["text"]
            by_id[lid]["emotion"] = rewritten[lid]["emotion"]
            applied.append(lid)
        else:
            warnings.append(f"{lid} はLLM出力に含まれておらず、書き換えられませんでした")

    return draft, warnings, applied

    return "\n".join(lines)
