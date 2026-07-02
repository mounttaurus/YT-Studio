"""
紙芝居パネル生成の構造化入力 → 英語プロンプト断片のプリセット辞書。
shared/imagegen/panel_presets.json に外出しし、ユーザーが項目を追加・編集できる。
無ければデフォルトを書き出す（style_manager と同じ方針）。

構造: { group: [ {"id": str, "label_ja": str, "prompt": str}, ... ] }
group = emotion | pose | shot | angle | scene
"""
import json
import os
from pathlib import Path

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
PRESETS_FILE = SHARED_DIR / "imagegen" / "panel_presets.json"

DEFAULT_PRESETS = {
    "emotion": [
        {"id": "neutral",   "label_ja": "通常",   "prompt": "neutral expression"},
        {"id": "happy",     "label_ja": "嬉しい", "prompt": "happy, smiling, bright eyes"},
        {"id": "sad",       "label_ja": "悲しい", "prompt": "sad, downcast eyes"},
        {"id": "excited",   "label_ja": "興奮",   "prompt": "excited, sparkling eyes, energetic"},
        {"id": "serious",   "label_ja": "真剣",   "prompt": "serious, firm expression, focused"},
        {"id": "question",  "label_ja": "疑問",   "prompt": "puzzled, slight head tilt, questioning look"},
        {"id": "angry",     "label_ja": "怒り/激情", "prompt": "angry, furrowed brows, intense expression"},
        {"id": "surprised", "label_ja": "驚き",   "prompt": "surprised, wide eyes, open mouth"},
        {"id": "shy",       "label_ja": "照れ",   "prompt": "blushing, shy smile"},
        {"id": "troubled",  "label_ja": "困惑",   "prompt": "troubled, worried expression"},
    ],
    "pose": [
        {"id": "talking",     "label_ja": "話している", "prompt": "mouth open, talking, light hand gesture"},
        {"id": "thinking",    "label_ja": "考えている", "prompt": "hand on chin, thoughtful pose"},
        {"id": "looking_up",  "label_ja": "見上げる",   "prompt": "looking up"},
        {"id": "looking_down","label_ja": "見下ろす",   "prompt": "looking down"},
        {"id": "pointing",    "label_ja": "指差し",     "prompt": "pointing finger forward"},
        {"id": "arms_crossed","label_ja": "腕組み",     "prompt": "arms crossed"},
        {"id": "waving",      "label_ja": "手を振る",   "prompt": "waving one hand"},
        {"id": "presenting",  "label_ja": "提示",       "prompt": "presenting with an open hand"},
        {"id": "standing",    "label_ja": "立ち（自然）","prompt": "standing naturally, relaxed"},
    ],
    "shot": [
        {"id": "face_closeup","label_ja": "顔アップ",     "prompt": "extreme close-up of the face"},
        {"id": "bust",        "label_ja": "バストアップ", "prompt": "bust shot, upper body from the chest up"},
        {"id": "waist_up",    "label_ja": "ウエストアップ","prompt": "waist-up shot"},
        {"id": "full_body",   "label_ja": "全身",         "prompt": "full body shot, head to toe"},
        {"id": "wide",        "label_ja": "引き（全景）", "prompt": "wide shot showing the full scene"},
    ],
    "angle": [
        {"id": "eye_level",     "label_ja": "正面（目線）", "prompt": "eye-level shot, front view"},
        {"id": "three_quarter", "label_ja": "斜め",         "prompt": "three-quarter view, slight angle"},
        {"id": "low_angle",     "label_ja": "煽り（下から）","prompt": "low angle shot, camera looking up"},
        {"id": "high_angle",    "label_ja": "俯瞰（上から）","prompt": "high angle shot, camera looking down"},
    ],
    "scene": [
        {"id": "solo",         "label_ja": "単独",     "prompt": "single character alone"},
        {"id": "two_shot",     "label_ja": "対面（2人）","prompt": "two characters facing each other, conversation"},
        {"id": "over_shoulder","label_ja": "肩越し",    "prompt": "over-the-shoulder composition"},
    ],
}

GROUPS = ("emotion", "pose", "shot", "angle", "scene")


def load_presets() -> dict:
    if not PRESETS_FILE.exists():
        save_presets(DEFAULT_PRESETS)
        return dict(DEFAULT_PRESETS)
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        # 後方互換: 既存ファイルに無いgroupはデフォルトで補完（破壊しない）
        changed = False
        for g in GROUPS:
            if g not in data:
                data[g] = DEFAULT_PRESETS[g]
                changed = True
        if changed:
            save_presets(data)
        return data
    except Exception:
        return dict(DEFAULT_PRESETS)


def save_presets(presets: dict) -> None:
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRESETS_FILE.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")


def fragment(group: str, item_id: str) -> str:
    """group内のidに対応する英語プロンプト断片を返す（無ければ空文字）。"""
    if not item_id:
        return ""
    for item in load_presets().get(group, []):
        if item["id"] == item_id:
            return item["prompt"]
    return ""


def build_panel_prompt(
    appearance_prompt: str, style_prefix: str,
    *, emotion_id="", pose_id="", shot_id="", angle_id="", scene_id="",
    background_mode="flat", extra_prompt="",
) -> str:
    """構造化入力を1本の英語プロンプトに組み立てる。順序は画角→ポーズ→表情→構図→背景。"""
    bg = {
        "scene": "in a simple anime-style background scene",
        "flat": "plain solid pastel background, flat single color, no scenery",
        "transparent": "isolated subject on a plain white background",
    }.get(background_mode, "")
    parts = [
        style_prefix.strip().rstrip(","),
        appearance_prompt.strip(),
        fragment("shot", shot_id),
        fragment("angle", angle_id),
        fragment("pose", pose_id),
        fragment("emotion", emotion_id),
        fragment("scene", scene_id),
        bg,
        extra_prompt.strip(),
    ]
    return ", ".join(p for p in parts if p)


def slug(*ids: str) -> str:
    """生成ファイル名用の短いスラッグ（既存 next_filename の expression に渡す）。"""
    return "-".join(i for i in ids if i) or "panel"
