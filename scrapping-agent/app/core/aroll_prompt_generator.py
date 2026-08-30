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

from app.core import llm_client, panel_presets
from app.core.query_generator import _parse_llm_json, group_lines_by_section

# 演技スロット（2026-08-19新規）: 画像再利用の照合キーに使う4軸。
# 語彙は panel_presets.py（🎬紙芝居タブと共有）から動的に読む＝ハードコードしない。
SLOT_AXES = ("emotion", "pose", "shot", "angle")

SYSTEM_PROMPT = (
    "You are a storyboard artist and prompt engineer for a manga-style YouTube video. "
    "For each dialogue line you write ONE image-generation prompt describing a single manga panel: "
    "the featured character(s) by name, their emotion, pose, and camera shot (close-up / bust shot / "
    "waist-up / wide shot). "
    "Camera angle matters a lot for manga energy: don't default to eye-level. Mix in low-angle shots "
    "(camera looking up at the character — dramatic, powerful) and high-angle / bird's-eye shots (camera "
    "looking down — vulnerable, overview); use one of these strong angles every few panels, not only at "
    "emotional peaks. "
    "Do NOT describe or invent a background, location or setting — panels are composited onto a plain "
    "background separately, so describe only the character(s) and the shot. "
    "The image model also receives labeled reference images of the characters, so refer to characters "
    "clearly — never describe hair, face or clothing. "
    "CRITICAL for multi-character panels: never write a character's actual name in the prose. Instead, "
    "write their char_id wrapped in square brackets, e.g. [002], exactly as given in the character "
    "list, every time you refer to them (subject, object, possessive — all of it). Do not translate, "
    "romanize, or spell out the name yourself; you do not need to know it. The real display name will "
    "be substituted in for you afterward, programmatically, from the exact same source that labels the "
    "reference images — so this is the only way to guarantee the label and the prose always match. If "
    "you write the name yourself instead of the [char_id] tag, spelling drift between calls WILL cause "
    "the image model to draw the wrong face for that character, especially when shown from behind or "
    "at a distance where facial features cannot anchor the identity. "
    "Vary shots and angles across consecutive lines like a real manga page. "
    "Never include text, letters or speech bubbles in the image. "
    "In addition to the prose prompt, classify each panel into a 'slot' using ONLY the given "
    "vocabulary IDs for emotion/pose/shot/angle (pick the closest match; this is used to detect and "
    "reuse visually similar panels later, so classify honestly — do not default to the same slot for "
    "every panel just because it is convenient). "
    "Prompts must be in English. Respond with JSON only, no explanation."
)

PROMPT_TEMPLATE = """以下はYouTube動画の台本の1章分です。各セリフ行ごとに、マンガの1コマとして描く画像生成プロンプトを作ってください。

登場キャラクター（characters には char_id を使うこと）:
{characters_block}

ルール:
- "characters" は映すキャラの char_id を1〜2人。基本はその行の話者。会話の掛け合いで聞き手のリアクションや対面カット(two shot)が効果的な行では2人にする。
- "prompt" は英語。表情・ポーズ・ショットを含める。髪型・服装・顔立ちは書かない（参照画像が担保する）。
- 【最重要】本文中でキャラに言及する時、名前を書いてはいけない。必ず char_id を角括弧で囲んだ
  タグ（例: [002]）を使うこと（主語・目的語・所有格すべて）。名前はこちらで機械的に差し込むため、
  表記（英語/日本語/ローマ字）を一切気にしなくてよい・知らなくてよい。もし名前を自分で書くと、
  呼び方が回によって揺れて（例:「ルカ」/"Luka"/"Ruka"）参照画像のラベルと食い違い、画像モデルが
  別人の顔を描いてしまう（特に後ろ姿など顔で判別できない構図で顕著）。1人しか登場しないコマでも
  タグ形式で統一してよい。
- 背景・場所・シチュエーションは一切描写しない（合成時に単色背景を自動で付与するため）。
- アングルは正面(eye-level)に偏らせない。煽り(low-angle、見上げる)・俯瞰(high-angle、見下ろす)を数コマに1回は積極的に混ぜ、感情の起伏が大きい行ほど強いアングルを使う。
- 傾き(dutch angle)・瞳アップ(eyes-only close-up)は「ここぞ」という感情のピーク（衝撃の事実・驚きの展開等）だけに使うこと。毎コマ使うと逆に単調になるため、通常の相槌・説明シーンはバスト/ウエストアップ中心でよい。
- 吹き出しを後から載せるため、キャラを画面の片側に寄せて余白を作る構図指示を入れてよい。**顔・瞳が画面いっぱいに来るショット（顔アップ・瞳アップ）を選んだ時は、この余白指示を必須にすること**（顔で画面が埋まると吹き出しを置く場所が無くなるため）。画像内に文字・吹き出しは絶対に描かせない。
- 連続する行で同じ構図を繰り返さない（ショット/アングルを切り替える）。
- "slot" は下記の語彙のidだけを使って分類すること（判定できない軸はキー自体を省略してよい。無理に埋めない）:
{slot_vocab_block}
{extra}
台本（章: {section}）:
{lines_text}

出力形式（JSONのみ・全行分を line_id 順に。prompt内のキャラ言及は必ず[char_id]タグで、下の
"002"/"001"は例なので実際に登場する char_id に置き換えること）:
{{
  "panels": [
    {{"line_id": "line_001", "characters": ["002", "001"],
      "slot": {{"emotion": "serious", "pose": "talking", "shot": "bust", "angle": "low_angle"}},
      "prompt": "[002], serious expression, leaning forward and looking at [001]. [001] is listening intently. Bust shot, low-angle."}}
  ]
}}
"""

# プロンプト生成LLMのチェーン（先頭から順に試す）。テキストのみなので無料枠で足りる。
DEFAULT_MODEL = os.getenv("AROLL_PROMPT_LLM", "gemini/gemini-2.5-flash")
FALLBACK_MODEL = os.getenv("AROLL_PROMPT_LLM_FALLBACK", "openrouter/openrouter/free")


# ---------------------------------------------------------------------------
# 演技スロット（2026-08-19新規）
# 語彙は panel_presets.json（🎬紙芝居タブと共有）。LLMには id 一覧を提示し、
# 応答が語彙外・欠損なら prompt 本文から正規表現で推定する（3段構え、詳細は
# Docs/AROLL_SLOT_REUSE_BRIEF.md §3-2）。
# ---------------------------------------------------------------------------

def _slot_vocab() -> dict[str, list[str]]:
    presets = panel_presets.load_presets()
    return {axis: [item["id"] for item in presets.get(axis, [])] for axis in SLOT_AXES}


def _slot_vocab_block(vocab: dict[str, list[str]]) -> str:
    return "\n".join(f"  - {axis}: {', '.join(vocab.get(axis, []))}" for axis in SLOT_AXES)


# 正規表現フォールバック分類器。実測(2026-08-19・本番403パネル)で shot/angle は100%、
# emotion は84%的中。先頭からマッチした最初のものを採用する（順序に意味がある）。
# アポストロフィを含む語は bird.?s.?eye のように任意1文字マッチで避ける。
_SHOT_PATTERNS = [
    # eyes_onlyはface_closeupより先に判定する: "extreme close-up on the eyes only" は
    # "close-up" を含むため、face_closeupを先にすると必ず取りこぼされる（同種の罠、
    # shy/happyの教訓を踏襲）。
    ("eyes_only",    re.compile(r"eyes? only|close-?up on (?:the |her |his |their )?eyes", re.I)),
    ("face_closeup", re.compile(r"extreme close-?up|close-?up", re.I)),
    ("bust",         re.compile(r"bust shot|bust-?up|chest up", re.I)),
    ("waist_up",     re.compile(r"waist-?up", re.I)),
    ("full_body",    re.compile(r"full body|head to toe", re.I)),
    ("wide",         re.compile(r"wide shot", re.I)),
    ("profile",      re.compile(r"profile view|side face|from the side", re.I)),
]
_ANGLE_PATTERNS = [
    ("low_angle",     re.compile(r"low[- ]angle|looking up at|from below", re.I)),
    ("high_angle",    re.compile(r"high[- ]angle|bird.?s.?eye|looking down at|from above|overhead", re.I)),
    ("three_quarter", re.compile(r"three-?quarter|3/4", re.I)),
    ("eye_level",     re.compile(r"eye-?level|front view|straight on", re.I)),
    ("dutch",         re.compile(r"dutch angle|tilted frame|off-kilter", re.I)),
    ("from_behind",   re.compile(r"from behind|back view", re.I)),
]
_EMOTION_PATTERNS = [
    ("angry",      re.compile(r"angry|furrow|scowl|glar|fierce|indignan", re.I)),
    ("surprised",  re.compile(r"surprised|shock|wide eyes|astonish|gasp", re.I)),
    ("sad",        re.compile(r"sad|sorrow|downcast|tear|melanchol|somber|grie", re.I)),
    # shyはhappyより先に判定する: panel_presets.pyのshyプリセット文言が
    # "blushing, shy smile" で "smil" を含むため、happyを先にすると必ず誤分類される
    # （実装検証時に発見。テスト目的で書いた文でも実プリセット文言そのままでも再現する）。
    ("shy",        re.compile(r"blush|shy|bashful|embarrass", re.I)),
    ("happy",      re.compile(r"happy|smil|grin|cheerful|delight|bright eyes|warm expression", re.I)),
    ("excited",    re.compile(r"excited|energetic|sparkl|enthusias|eager", re.I)),
    ("serious",    re.compile(r"serious|stern|firm|resolute|determin|focused|intense", re.I)),
    ("question",   re.compile(r"puzzl|question|curious|head tilt|quizzic|confus", re.I)),
    ("troubled",   re.compile(r"troubl|worri|anxious|uneas|conflicted|hesitan|nervous", re.I)),
    ("thoughtful", re.compile(r"thoughtful|pensive|contempl|reflect|hand on chin", re.I)),
    ("neutral",    re.compile(r"neutral|calm|composed", re.I)),
]


def _classify(text: str, patterns: list[tuple[str, re.Pattern]]) -> Optional[str]:
    for name, pat in patterns:
        if pat.search(text):
            return name
    return None


def derive_slot_from_prompt(prompt_text: str) -> dict:
    """演出文(英語prompt)から正規表現でslotを推定する。poseは分類器を持たないため常にNone
    （slot_keyには使わないため実害なし。詳細はブリーフ §2-2）。"""
    text = prompt_text or ""
    return {
        "emotion": _classify(text, _EMOTION_PATTERNS),
        "pose": None,
        "shot": _classify(text, _SHOT_PATTERNS),
        "angle": _classify(text, _ANGLE_PATTERNS),
    }


# slot_key を構成する3軸。欠けさせない。
# ⚠️ 語彙提示用の SLOT_AXES（24行目・pose を含む4軸）とは**別物**。同じ名前にすると
# 後勝ちで上書きされ、pose の語彙が LLM に提示されなくなる（実際に一度やった）。
KEY_AXES = ("emotion", "shot", "angle")


def resolve_slot(
    raw_slot: Optional[dict], prompt_text: str, vocab: dict[str, list[str]],
) -> tuple[Optional[dict], str]:
    """LLM由来のslotを**軸ごとに**検証し、欠けた軸だけ本文から正規表現で補う。

    Returns: (slot, slot_source) — "llm" | "mixed" | "derived" | "none"

    ⚠️ 以前は3軸が揃っていなければ **slot を丸ごと捨てて** 正規表現で引き直していた。
    ところがプロンプト側は LLM に「判定できない軸はキー自体を省略してよい」と
    指示しているので、**正直に一部だけ答えた出力が毎回捨てられていた**。

    実測（ep01 / 196行）:
      slot_source = llm 11 (6%) / derived 163 / none 22
      さらに正規表現の経路は pose を常に None にするため、
      **エピソード全体の pose 10件は、生き残った11行だけから来ていた**
      （＝「アクションの項目が無い」の正体は、受け取り側の取りこぼし）。

    軸ごとに採れば、LLM が答えた軸（特に正規表現では絶対に取れない pose）が残る。
    3軸のどれかがどちらの手段でも埋まらない時だけ None を返す（従来どおりの安全弁。
    slot_key に欠けた軸を混ぜない）。
    """
    raw = raw_slot if isinstance(raw_slot, dict) else {}
    derived = derive_slot_from_prompt(prompt_text)
    slot: dict[str, Optional[str]] = {}
    sources: set[str] = set()
    for axis in KEY_AXES:
        v = raw.get(axis)
        if v in vocab.get(axis, []):
            slot[axis], src = v, "llm"
        elif derived.get(axis):
            slot[axis], src = derived[axis], "derived"
        else:
            return None, "none"
        sources.add(src)

    pose = raw.get("pose")   # 正規表現は pose を出せないので LLM だけが供給源
    out = {"emotion": slot["emotion"], "pose": pose if pose in vocab.get("pose", []) else None,
           "shot": slot["shot"], "angle": slot["angle"]}
    if sources == {"llm"}:
        return out, "llm"
    if sources == {"derived"}:
        return out, "derived"
    return out, "mixed"


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


# 2人以上のコマでキャラ名の表記(英訳/ローマ字化)がLLMの呼び出しごとに揺れて参照画像ラベルと
# 食い違い、取り違えの原因になっていた（実例: 本番で"ルカ"↔"Luka"/"Ruka"混在。詳細は
# Docs/AROLL_SLOT_REUSE_BRIEF.md §7）。「名前をそのまま使え」という自然文指示だけでは実測で
# 守られなかった（LLMが文体判断で自発的に翻訳/転写してしまう）ため、名前を書かせず
# [char_id] タグで指させ、こちらで機械的に置換する（LLMは翻訳しようがない不透明トークン）。
_CHAR_TAG_RE = re.compile(r"\[([A-Za-z0-9_\-]+)\]")


def substitute_char_tags(text: str, known_chars: dict[str, dict]) -> str:
    """prompt本文の [char_id] タグを表示名へ機械的に置換する（既知char_id以外はタグのまま残す）。"""
    def repl(m: re.Match) -> str:
        name = known_chars.get(m.group(1), {}).get("name")
        return name if name else m.group(0)
    return _CHAR_TAG_RE.sub(repl, text or "")


def _normalize_characters(
    raw: list, known_chars: dict[str, dict], speaker_char_id: str,
) -> list[str]:
    """LLM出力のキャラ列をchar_idへ正規化する（名前ゆらぎ対応・最大2人）。

    known_chars: {char_id: {"name": ...}}（**描けるキャラだけ**が入っている）

    ★**話者は機械的に先頭へ固定する**（2026-08-30）。従来はLLMの出力を採用し、
      1人も解決できなかった時だけ話者へフォールバックしていたため、**LLMが聞き手
      だけを返すと話者不在のコマが作れてしまった**。静止画マンガでは「喋っている
      人物が写っていない」表現は成立しないので、ここは自然文の指示に頼らず機械で担保する
      （memory ``aroll-two-shot-subchar-failure``: 自然文指示は信用しない）。

    ★2人目は演出の選択なので残す。対面カット/リアクションの2ショットは意図した設計。

    ★話者が ``known_chars`` に居ない = **その話者は描けない**（声だけのキャラ）。
      その場合は空配列を返す ＝ ナレーション（キャラ無し・背景のみのコマ）。
      ここで別のキャラを代役に立てない ── 喋っていない人物が写ることになるため。
    """
    if speaker_char_id and speaker_char_id not in known_chars:
        return []  # ナレーション。代役を立てない

    out: list[str] = []
    if speaker_char_id:
        out.append(speaker_char_id)  # 話者は必ず映る
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
    vocab = _slot_vocab()

    prompt = PROMPT_TEMPLATE.format(
        characters_block=characters_block, section=section,
        lines_text=lines_text, extra=extra, slot_vocab_block=_slot_vocab_block(vocab),
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
        speaker_char = speaker.get("character_id") or ""
        p = by_line.get(lid)
        if p is None:
            warnings.append(f"[{section}] LLM応答に {lid} が無いためスキップ")
            continue
        chars = _normalize_characters(p.get("characters"), known_chars, speaker_char)
        text = str(p.get("prompt", "")).strip()
        if not text:
            warnings.append(f"[{section}] {lid} のpromptが空のためスキップ")
            continue
        # [char_id]タグを表示名へ機械的に置換（LLMがタグ指示を守らず直接名前を書いた場合は
        # マッチするタグが無いため何もしない＝下の警告チェックがそのケースを拾う）
        text = substitute_char_tags(text, known_chars)
        if len(chars) >= 2:
            # 置換後もなお名前が見当たらない＝タグも使わず・指示どおりの名前も書かなかった
            # ケース。表記ゆれで参照画像との対応付けが崩れている可能性が高い（実例: 本番で
            # "ルカ"を"Ruka"と独自に英訳し取り違え発生。詳細はDocs/AROLL_SLOT_REUSE_BRIEF.md §7）。
            missing_names = [
                known_chars[cid]["name"] for cid in chars
                if known_chars.get(cid, {}).get("name") and known_chars[cid]["name"] not in text
            ]
            if missing_names:
                warnings.append(
                    f"[{section}] {lid}: 本文にキャラ名 {missing_names} の表記が見当たりません"
                    "（[char_id]タグを使わず名前を直接書いた可能性・取り違えの恐れ）"
                )
        slot, slot_source = resolve_slot(p.get("slot"), text, vocab)
        result[lid] = {
            "characters": chars, "prompt": text,
            "slot": slot, "slot_source": slot_source,
        }
    return result, warnings
