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

# 背景モードのプロンプト断片。ここが本籍（panel_library_manager から参照される）。
#
# ⚠️ flat の色相指定には根拠がある。71枚で「背景色とキャラ色の衝突量」を測ると、
# 肌に近い色相（赤〜橙）の背景は緑〜シアンの **8.5倍** 衝突した
# （中央値 0.0051 vs 0.0006）。桃色背景×金髪で顔が欠けた実例はここに集中している。
# 対策は彩度を上げること（グリーンバック）ではない ── きつい色はフチへの色移りが増える。
# **パステルのまま色相だけ肌から離す**のが正解。cutout_engine は背景色を実測で
# 推定するので、特定のカラーコードを守らせる必要は無く、肌と衝突しないことだけが要件。
#
# ⚠️ 2026-09-05追記: 「肌から離れていれば何でもいい」ではなかった。sky blue/lavender
# （青紫系）は瞳の色（本キャラ群は青系・紫系の瞳が多い）とアニメ塗りの陰影トーンに衝突し、
# 抜け残り(leftover_ratio)が緑系の10倍以上に悪化することを実測（0.015〜0.023 vs 0.0000〜0.0018、
# 2キャラ・flat/ai両方式で再現）。青紫を候補から外しミント/グリーン系に絞ったところ
# leftover_ratioが0.0付近まで改善した。詳細は memory/pastel-hue-collides-with-eye-color。
BACKGROUND_MODES = {
    "scene": "in a simple anime-style background scene",
    "flat": ("plain solid pastel background, flat single color, no scenery, "
             "in a cool green pastel hue such as mint, seafoam, or sage green; "
             "never blue, indigo, violet, lavender, sky blue, peach, pink, cream, "
             "beige or any skin-like tone"),
    "transparent": "isolated subject on a plain white background",
}

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
        {"id": "thoughtful","label_ja": "物思い", "prompt": "thoughtful, pensive expression, hand on chin"},
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
        {"id": "muttering",    "label_ja": "つぶやき",   "prompt": "muttering to oneself, hand near mouth, quiet, looking down or aside"},
        {"id": "sweat_drop",   "label_ja": "冷や汗",     "prompt": "anime sweat drop, nervous, awkward"},
        {"id": "held_breath",  "label_ja": "息を呑む",   "prompt": "holding breath, frozen for a beat, wide-eyed stillness"},
        {"id": "clenched_fist","label_ja": "拳を握る",   "prompt": "clenched fist, quiet resolve"},
    ],
    "shot": [
        {"id": "face_closeup","label_ja": "顔アップ",     "prompt": "extreme close-up of the face"},
        {"id": "bust",        "label_ja": "バストアップ", "prompt": "bust shot, upper body from the chest up"},
        {"id": "waist_up",    "label_ja": "ウエストアップ","prompt": "waist-up shot"},
        {"id": "full_body",   "label_ja": "全身",         "prompt": "full body shot, head to toe"},
        {"id": "wide",        "label_ja": "引き（全景）", "prompt": "wide shot showing the full scene"},
        {"id": "profile",     "label_ja": "横顔",         "prompt": "profile view, side face, looking off to the side"},
        {"id": "eyes_only",   "label_ja": "瞳アップ",     "prompt": "extreme close-up on the eyes only, dramatic focal point"},
    ],
    "angle": [
        {"id": "eye_level",     "label_ja": "正面（目線）", "prompt": "eye-level shot, front view"},
        {"id": "three_quarter", "label_ja": "斜め",         "prompt": "three-quarter view, slight angle"},
        {"id": "low_angle",     "label_ja": "煽り（下から）","prompt": "low angle shot, camera looking up"},
        {"id": "high_angle",    "label_ja": "俯瞰（上から）","prompt": "high angle shot, camera looking down"},
        {"id": "dutch",         "label_ja": "傾き",         "prompt": "dutch angle, tilted frame, off-kilter"},
        {"id": "from_behind",   "label_ja": "背後から",     "prompt": "shot from behind the character, back view"},
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
        # 後方互換: 既存groupにデフォルトの項目(id)が無ければ末尾に追加（ユーザー編集は保持）
        for g in GROUPS:
            existing_ids = {item.get("id") for item in data.get(g, [])}
            for item in DEFAULT_PRESETS.get(g, []):
                if item["id"] not in existing_ids:
                    data[g].append(item)
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
    bg = BACKGROUND_MODES.get(background_mode, "")
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
