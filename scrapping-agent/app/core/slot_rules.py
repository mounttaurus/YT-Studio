"""台本の行から演技スロットをルールで決める（LLM を呼ばない）。

`Docs/AROLL_EMOTION_LOCAL_PLAN.md` §2-1 の R1 と §5（2026-10-01 決定 E3）。
在庫から絵を選ぶのに要るのは **emotion 1語だけ**（shot・angle は在庫選定では使われず、
寄り引き・向きは `camera_plan` が話者の連続を単位に決める）。本番8話・618コマの実測で、
この規則は LLM の slot と 3群（陽/陰/中）で85%一致し、陽↔陰の取り違えは 0.3%、
在庫に同じ感情がある行は 91%（LLM）→100% だった。

使い道: プロンプトの無い新しい行（挿入など）・LLM が失敗/拒否/未実行の行の穴埋め。
サブ行は親のコマを引き継ぐので、ここは通らない（`aroll_manager.fill_slots_without_llm`）。
"""

from __future__ import annotations

# 台本の感情語彙（script_validator.VALID_EMOTIONS）→ Aロールの感情語彙（panel_presets・11語）。
# 同じ語はそのまま。語彙が違うものだけ読み替える。
EMOTION_ALIAS = {
    "thinking": "thoughtful", "worried": "troubled", "curious": "question", "calm": "neutral",
    "shocked": "surprised", "confused": "question", "fear": "troubled", "scared": "troubled",
}
EMOTION_VOCAB = (
    "neutral", "happy", "sad", "excited", "serious", "question",
    "angry", "surprised", "shy", "troubled", "thoughtful",
)

# shot・angle は在庫選定では使われない（slot_key を欠けさせないための既定値）。
DEFAULT_SHOT = "bust"
DEFAULT_ANGLE = "eye_level"

_EXCITED_WORDS = ("すごい", "やった", "楽し", "嬉し", "最高")


def normalize_emotion(emotion: str | None) -> str:
    """台本の感情を Aロールの語彙へ。知らない語・空は neutral。"""
    e = (emotion or "neutral").strip().lower()
    e = EMOTION_ALIAS.get(e, e)
    return e if e in EMOTION_VOCAB else "neutral"


def rule_emotion(line: dict) -> str:
    """台本の感情＋記号。**neutral の行だけ**本文で上書きする（台本が明示した感情は尊重）。"""
    e = normalize_emotion(line.get("emotion"))
    if e != "neutral":
        return e
    text = line.get("text") or ""
    if "？" in text or "?" in text:
        return "question"
    if "！" in text or "!" in text:
        return "excited" if any(w in text for w in _EXCITED_WORDS) else "surprised"
    return "serious"   # 解説口調の平叙文（本番の多数派）


def rule_slot(line: dict) -> dict:
    """行（script.json の1行）から演技スロットを作る。pose は分類器を持たないので None。"""
    return {"emotion": rule_emotion(line), "pose": None, "shot": DEFAULT_SHOT, "angle": DEFAULT_ANGLE}
