"""
紙芝居パネル生成の構造化入力 → 英語プロンプト断片のプリセット辞書。
shared/imagegen/panel_presets.json に外出しし、ユーザーが項目を追加・編集できる。
無ければデフォルトを書き出す（style_manager と同じ方針）。

構造: { group: [ {"id": str, "label_ja": str, "prompt": str}, ... ] }
group = emotion | pose | shot | angle | facing | scene

⚠️ **向きは `facing` が唯一の軸**（2026-09-23 新設・`Docs/FACING_AXIS_PLAN.md`）。
以前は pose の facing_left/facing_right/profile_left/profile_right・angle の from_behind・
shot の profile に向きが分散していた。それらは語彙から削除済み。左右は**画面の左右**
（視聴者から見た左右。キャラ自身の左右ではない。実物2枚を目視して確認済み）。
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
        # ⚠️ `hand on chin` を 2026-09-06 に削除した。**emotion は顔の表情だけを言い、
        # 手や体は pose に任せる**（そこが分業）。混ざっていたせいで
        # `pose=arms_chrossed` を指定しても「顎に手」が勝ってしまい、腕組みの絵が作れなかった
        # （実測：thoughtful×arms_crossed で生成した絵が全て顎に手だった）。
        # 顎に手のポーズが要る時は pose=`thinking`（"hand on chin, thoughtful pose"）を使う。
        {"id": "thoughtful","label_ja": "物思い", "prompt": "thoughtful, pensive expression"},
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
        # ⚠️ 向きの4値（facing_left/facing_right/profile_left/profile_right）は
        # 2026-09-23 に `facing` 軸へ移設して削除した（`Docs/FACING_AXIS_PLAN.md`）。
        # pose は**アクション**だけを言う。向きは build_panel_prompt の facing_id で指定する。
        # 旧在庫の pose にこれらの値が残っている場合の読み替えは LEGACY_FACING を参照。
    ],
    "shot": [
        {"id": "face_closeup","label_ja": "顔アップ",     "prompt": "extreme close-up of the face"},
        {"id": "bust",        "label_ja": "バストアップ", "prompt": "bust shot, upper body from the chest up"},
        {"id": "waist_up",    "label_ja": "ウエストアップ","prompt": "waist-up shot"},
        {"id": "full_body",   "label_ja": "全身",         "prompt": "full body shot, head to toe"},
        {"id": "wide",        "label_ja": "引き（全景）", "prompt": "wide shot showing the full scene"},
        # ⚠️ `profile`（横顔）は 2026-09-23 に削除した。画角ではなく向きの概念だったため
        # `facing` 軸（left_profile/right_profile）へ移設（`Docs/FACING_AXIS_PLAN.md`）。
        # shot × facing の組み合わせで同じ構図を表現できる。
        {"id": "eyes_only",   "label_ja": "瞳アップ",     "prompt": "extreme close-up on the eyes only, dramatic focal point"},
        # ⚠️ `face_closeup`（顔アップ）とは**別物**として 2026-09-06 に追加した。
        # face_closeup は "extreme close-up of the face" と書いてあるのに、実際の生成物は
        # **頭と肩が入ったバストアップ寄り**で、マンガの決めゴマで使う「顔が画面を埋める」
        # 構図が在庫に事実上ゼロだった（ルカ75枚を目視で確認）。ラベルと実物が食い違う例
        # （[[slot-shot-label-unreliable]] と同じ構図）。プロンプトで**切り取り位置を明示**して
        # 初めてその構図になる。既存の face_closeup は挙動を変えたくないので触っていない。
        {"id": "face_extreme", "label_ja": "顔ドアップ",
         "prompt": ("extreme facial close-up, the face fills the entire frame, "
                    "cropped above the eyebrows and below the chin, shoulders not visible, "
                    "eyes and mouth dominate the composition, dramatic manga panel framing")},
    ],
    "angle": [
        {"id": "eye_level",     "label_ja": "正面（目線）", "prompt": "eye-level shot, front view"},
        {"id": "three_quarter", "label_ja": "斜め",         "prompt": "three-quarter view, slight angle"},
        {"id": "low_angle",     "label_ja": "煽り（下から）","prompt": "low angle shot, camera looking up"},
        {"id": "high_angle",    "label_ja": "俯瞰（上から）","prompt": "high angle shot, camera looking down"},
        {"id": "dutch",         "label_ja": "傾き",         "prompt": "dutch angle, tilted frame, off-kilter"},
        # ⚠️ `from_behind`（背後から）は 2026-09-23 に削除した。カメラ位置の軸に向きが
        # 混入していたため `facing` 軸（back）へ移設（`Docs/FACING_AXIS_PLAN.md`）。
    ],
    "facing": [
        # ⚠️ 2026-09-23 新設。キャラの向き専用の軸（`Docs/FACING_AXIS_PLAN.md`）。
        # 左右は**画面の左右**（視聴者から見た左右）。ID は旧 pose/angle の値と同じにして
        # あるので、既存384件はそのままこの軸の値として読める（移行はslot_idを変えない）。
        {"id": "front",         "label_ja": "正面",         "prompt": "facing the viewer"},
        {"id": "left_3q",       "label_ja": "左斜め",
         "prompt": "body turned to the left, three-quarter view facing left, shoulders angled away from the camera"},
        {"id": "left_profile",  "label_ja": "左真横",
         "prompt": ("strict side profile facing left, the head seen fully from the side, "
                    "nose lips and chin drawn in clean silhouette, the far eye not visible")},
        {"id": "right_3q",      "label_ja": "右斜め",
         "prompt": "body turned to the right, three-quarter view facing right, shoulders angled away from the camera"},
        {"id": "right_profile", "label_ja": "右真横",
         "prompt": ("strict side profile facing right, the head seen fully from the side, "
                    "nose lips and chin drawn in clean silhouette, the far eye not visible")},
        {"id": "back",          "label_ja": "背面（後ろ姿）", "prompt": "shot from behind the character, back view"},
    ],
    "scene": [
        {"id": "solo",         "label_ja": "単独",     "prompt": "single character alone"},
        {"id": "two_shot",     "label_ja": "対面（2人）","prompt": "two characters facing each other, conversation"},
        {"id": "over_shoulder","label_ja": "肩越し",    "prompt": "over-the-shoulder composition"},
    ],
}

GROUPS = ("emotion", "pose", "shot", "angle", "facing", "scene")

# --------------------------------------------------------------------- 向きの旧値読み替え
#
# 向きが pose/angle/shot に混ざっていた頃（〜2026-09-23）の値 → 新 facing 軸での読み替え。
# ⚠️ **ここが唯一の本籍**（`Docs/FACING_AXIS_PLAN.md` §2-2）。cutout_selector・移行スクリプト・
# API入口・stock_gap/stock_yield は全部ここを import すること（コピーを作らない）。
LEGACY_FACING: dict[tuple[str, str], str] = {
    ("pose", "facing_left"): "left_3q",
    ("pose", "facing_right"): "right_3q",
    ("pose", "profile_left"): "left_profile",
    ("pose", "profile_right"): "right_profile",
    ("angle", "from_behind"): "back",
}

# 語彙から消した値を、元の軸でどう置き換えるか（None = 未設定に戻す）。
# pose の向き系4値は「動作」の情報を持たないので None に戻す（不明を制約にしない設計に合わせる）。
# angle.from_behind はカメラの高さが不明になるので中立の既定 eye_level。
# shot.profile は向きの左右が読み取れないので None（実測の段で埋め直す。移行スクリプト側の仕事）。
LEGACY_REPLACEMENT: dict[tuple[str, str], str | None] = {
    ("pose", "facing_left"): None,
    ("pose", "facing_right"): None,
    ("pose", "profile_left"): None,
    ("pose", "profile_right"): None,
    ("angle", "from_behind"): "eye_level",
    ("shot", "profile"): None,
}


def legacy_facing(pose: str | None, angle: str | None) -> str | None:
    """旧ラベル（pose/angle）から向きを読む。分からなければ None（呼び出し側が front 扱いにする）。

    優先順位は angle.from_behind（背面）を pose より先に見る ── 背面のポーズラベルは
    存在しないので、from_behind と向き系 pose が両方付いている entry では背面を優先する。
    """
    if (angle or "") == "from_behind":
        return LEGACY_FACING[("angle", "from_behind")]
    return LEGACY_FACING.get(("pose", pose or ""))


def load_presets() -> dict:
    if not PRESETS_FILE.exists():
        save_presets(DEFAULT_PRESETS)
        return dict(DEFAULT_PRESETS)
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        changed = False
        # 後方互換: 既存ファイルに無いgroupはデフォルトで補完（破壊しない）
        for g in GROUPS:
            if g not in data:
                data[g] = DEFAULT_PRESETS[g]
                changed = True
        # 向きの軸移設（2026-09-23）: 語彙から消した旧値を共有JSONからも取り除く。
        # ⚠️ 足すだけで消さない従来の補完ロジックだと、コードから消してもJSONに残った旧値が
        # UIの選択肢に出続ける（[[presets-live-in-code-and-shared-data]]）。ユーザーが自分で
        # 追加したidは触らない＝LEGACY_REPLACEMENTに載っているidだけを対象にする。
        for (axis, legacy_id) in LEGACY_REPLACEMENT:
            items = data.get(axis, [])
            kept = [i for i in items if i.get("id") != legacy_id]
            if len(kept) != len(items):
                data[axis] = kept
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
    *, emotion_id="", pose_id="", shot_id="", angle_id="", facing_id="", scene_id="",
    background_mode="flat", extra_prompt="",
) -> str:
    """構造化入力を1本の英語プロンプトに組み立てる。順序は画角→アングル→向き→ポーズ→表情→構図→背景。

    facing_id: 2026-09-23 新設。空文字なら断片が入らない（front を明示したい時は "front" を渡す。
    未指定のままだと angle が斜めでも「正面」として在庫に入ってしまう ── Docs/FACING_AXIS_PLAN.md Q3）。
    """
    bg = BACKGROUND_MODES.get(background_mode, "")
    parts = [
        style_prefix.strip().rstrip(","),
        appearance_prompt.strip(),
        fragment("shot", shot_id),
        fragment("angle", angle_id),
        fragment("facing", facing_id),
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
