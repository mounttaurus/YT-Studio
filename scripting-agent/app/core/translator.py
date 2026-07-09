"""
行保存翻訳（line-preserving translation）— 確定 script.json を対象言語へ翻訳する。

設計の要点（Docs/08_i18n.md §3b, §5）:
- 入力は確定 script.json のみ（draftは対象外）。
- line_id / order / speaker_id / section / emotion / speed / pause_after_sec は原本と同一のまま、
  text だけを翻訳する（行構造の維持が Aロールパネル・素材紐付け継承の前提）。
- LLM出力の行数が原本と一致しない場合はチャンクを半分に割ってリトライ（speaker-id-normalization の教訓:
  LLM出力を信用せず、機械的な検証と再割当で守る）。
- 翻訳結果は locales/{lang}/script.json に保存し、source_script_hash で鮮度を判定できるようにする。
"""
import logging
import re
from typing import Optional

from app.core import llm_client, project_manager

logger = logging.getLogger(__name__)

# プロンプトで言語名を明示するための表示名（未知のコードはそのまま渡す）
LANG_NAMES = {
    "en": "English",
    "es": "Spanish (Español)",
    "zh": "Chinese (中文)",
    "ko": "Korean (한국어)",
    "fr": "French (Français)",
    "de": "German (Deutsch)",
    "pt": "Portuguese (Português)",
    "id": "Indonesian (Bahasa Indonesia)",
    "vi": "Vietnamese (Tiếng Việt)",
    "th": "Thai (ภาษาไทย)",
    "ja": "Japanese (日本語)",
}

# 1回のLLM呼び出しで翻訳する最大行数。多すぎると行ズレ・脱落のリスクが上がる
CHUNK_SIZE = 25
# チャンク分割リトライの最小サイズ（これで失敗したらエラー）
MIN_CHUNK_SIZE = 4


def lang_display(lang: str) -> str:
    return LANG_NAMES.get(lang, lang)


def _build_prompt(numbered_lines: list[str], lang: str, instructions: Optional[str]) -> str:
    extra = f"\n追加の訳調指示: {instructions}" if instructions else ""
    return f"""あなたはYouTube動画台本の翻訳者です。以下の台本の各行を {lang_display(lang)} に翻訳してください。

ルール（厳守）:
- 出力は入力と同じ行数・同じ番号で、1行につき「番号: 訳文」の形式のみ。
- 話し言葉として自然に。書き言葉にしない。
- 原文と同程度の長さに保つ（音声化した時の尺を維持するため）。
- 固有名詞・数値は正確に保つ。
- 説明・前置き・コードブロックは一切出力しない。{extra}

台本:
{chr(10).join(numbered_lines)}"""


_LINE_RE = re.compile(r"^\s*(\d+)\s*[:：.．]\s*(.*)$")


def _parse_numbered_response(text: str, expected_count: int) -> Optional[list[str]]:
    """「番号: 訳文」形式の応答をパースする。expected_count と一致しなければ None。"""
    results: dict[int, str] = {}
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = _LINE_RE.match(raw)
        if m:
            num = int(m.group(1))
            # 同じ番号が複数出たら最初を優先（後続は説明等の可能性）
            if num not in results:
                results[num] = m.group(2).strip()
        elif results:
            # 番号無し行は直前の行の続き（LLMが長文を折り返した場合）として連結
            last = max(results)
            results[last] = (results[last] + " " + raw).strip()
    if len(results) != expected_count:
        return None
    # 番号は1..N を要求しているが、ズレていても個数一致なら順序で機械的に採用する
    return [results[k] for k in sorted(results)]


async def _translate_chunk(
    texts: list[str], lang: str, model: Optional[str], instructions: Optional[str],
) -> list[str]:
    """テキストのリストを翻訳して同じ長さのリストを返す。行数不一致は分割リトライ。"""
    numbered = [f"{i + 1}: {t}" for i, t in enumerate(texts)]
    prompt = _build_prompt(numbered, lang, instructions)
    response = await llm_client.chat(prompt, model=model, temperature=0.3, max_tokens=8192)
    parsed = _parse_numbered_response(response, len(texts))
    if parsed is not None:
        return parsed

    logger.warning("翻訳の行数不一致（期待%d行）。チャンクを分割してリトライ", len(texts))
    if len(texts) <= MIN_CHUNK_SIZE:
        # 最後の手段: 1行ずつ翻訳
        singles = []
        for t in texts:
            one = await llm_client.chat(
                _build_prompt([f"1: {t}"], lang, instructions),
                model=model, temperature=0.3, max_tokens=2048,
            )
            p = _parse_numbered_response(one, 1)
            if p is None:
                raise RuntimeError(f"1行翻訳でも形式不一致: {t[:50]}...")
            singles.append(p[0])
        return singles

    mid = len(texts) // 2
    left = await _translate_chunk(texts[:mid], lang, model, instructions)
    right = await _translate_chunk(texts[mid:], lang, model, instructions)
    return left + right


async def translate_title(title: str, lang: str, model: Optional[str]) -> str:
    """話タイトルを翻訳する（publish用）。失敗時は原題をそのまま返す（タイトルはブロッカーにしない）。"""
    if not title:
        return ""
    try:
        prompt = (
            f"次のYouTube動画エピソードのタイトルを {lang_display(lang)} に翻訳してください。"
            f"訳文のみを1行で出力（引用符・説明なし）。\n\nタイトル: {title}"
        )
        result = await llm_client.chat(prompt, model=model, temperature=0.3, max_tokens=256)
        first = result.strip().splitlines()[0].strip().strip('"「」『』')
        return first or title
    except Exception:
        logger.warning("タイトル翻訳に失敗（原題を使用）: %s", title, exc_info=True)
        return title


async def translate_episode(
    project_id: str,
    episode_number: int,
    lang: str,
    model: Optional[str] = None,
    instructions: Optional[str] = None,
) -> dict:
    """確定 script.json を行保存翻訳して locales/{lang}/script.json を書き出す。

    戻り値: {"locale_script": <保存したdict>, "title": <翻訳タイトル>, "line_count": N}
    呼び出し側（routes）が事前条件チェック（確定scriptの存在・force）と status 更新を行う。
    """
    script = project_manager.read_script(project_id, episode_number)
    if script is None:
        raise FileNotFoundError("確定 script.json がありません（先に台本を承認してください）")

    pj = project_manager.read_project(project_id)
    source_lang = pj.get("language", "ja")

    lines = script.get("lines", [])
    # 空行（テキスト無し）は翻訳対象から外し、位置を保って戻す
    idx_texts = [(i, l.get("text") or "") for i, l in enumerate(lines)]
    to_translate = [(i, t) for i, t in idx_texts if t.strip()]

    translated_map: dict[int, str] = {}
    for start in range(0, len(to_translate), CHUNK_SIZE):
        chunk = to_translate[start:start + CHUNK_SIZE]
        results = await _translate_chunk([t for _, t in chunk], lang, model, instructions)
        for (i, _), tr in zip(chunk, results):
            translated_map[i] = tr

    # 原本のディープコピーに text だけ差し替え（line_id等は原本と同一を保証）
    import copy
    locale_script = copy.deepcopy(script)
    for i, line in enumerate(locale_script.get("lines", [])):
        if i in translated_map:
            line["text"] = translated_map[i]
    locale_script["locale"] = lang
    locale_script["translated_from"] = source_lang
    locale_script["source_script_hash"] = project_manager.source_script_hash(script)

    # 話タイトルの翻訳（project.json episodes[].title を元にする）
    ep_title = ""
    for ep in pj.get("episodes", []):
        if ep.get("number") == episode_number:
            ep_title = ep.get("title", "")
            break
    translated_title = await translate_title(ep_title, lang, model)

    project_manager.save_locale_script(project_id, episode_number, lang, locale_script)
    return {
        "locale_script": locale_script,
        "title": translated_title,
        "line_count": len(lines),
        "translated_count": len(translated_map),
    }
