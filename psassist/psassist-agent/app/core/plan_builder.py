"""panel_plan.json の生成.

`aroll.json` ＋ 背景アーカイブ ＋ spec.py の実測仕様 から、Photoshop 側が
「考えずに実行できる」作業指示書を作る。**ここに全ロジックを置き、JSX は
無知能な実行係にする**（JSX 内でのデバッグは高コストなため）。

Photoshop を必要としないので、単体でテスト・レビューできる。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import kinsoku, spec

SCHEMA_VERSION = "1.2.0"  # 1.2.0: bubble.key_source を追加（記号ベースの行ごと形状上書き・S3）

# そのまま組めるが知らせておきたいもの（needs_attention にはしない）。
# ここを増やさないと、本当に見るべき件が助言に埋もれる。
ADVISORY_WARNINGS = frozenset(
    {
        "BUBBLE_OVERLAPS_FACE",  # 縮めても改善しない＝そのまま組む
        "LIGHT_MISMATCH_FIXED",  # 背景を差し替えて解決済み＝報告のみ
    }
)


# plan_builder.py → core → app → psassist-agent → psassist
PSASSIST_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
DEFAULT_BUBBLES_PSD = os.path.join(PSASSIST_ROOT, "assets", "bubbles.psd")


@dataclass
class Paths:
    """入出力の場所。特定のチャンネル・マシンに固定しない（env で差し替え可能）。"""

    episode_dir: str
    backgrounds_dir: str
    bubbles_psd: str
    out_dir: str

    @classmethod
    def from_env(cls) -> "Paths":
        """env から組み立てる。**ホスト固有の絶対パスを既定値に埋め込まない。**

        埋め込むと他のマシンでは黙って存在しない場所を見に行き、原因が分かりにくい。
        分からない値は既定を作らず、何を設定すべきかを言って落ちる方が速い。
        """
        ep = (os.environ.get("PSA_EPISODE_DIR") or "").strip()
        if not ep:
            raise RuntimeError(
                "PSA_EPISODE_DIR が未設定です。ルート .env に対象エピソードの絶対パスを"
                "設定してください（例: <HOST_SHARED_DIR>/projects/<project_id>/episodes/ep01）"
            )
        # 背景アーカイブは shared/ の下と決まっているので HOST_SHARED_DIR から導ける
        shared = (os.environ.get("HOST_SHARED_DIR") or "").strip()
        bgs = (os.environ.get("PSA_BACKGROUNDS_DIR") or "").strip()
        if not bgs and shared:
            bgs = os.path.join(shared, "backgrounds")
        if not bgs:
            raise RuntimeError(
                "背景アーカイブの場所が分かりません。ルート .env の HOST_SHARED_DIR か "
                "PSA_BACKGROUNDS_DIR のどちらかを設定してください"
            )
        return cls(
            episode_dir=ep,
            backgrounds_dir=bgs,
            # 既定はリポジトリ内の資産。コンテナでは compose が /app/assets を渡す
            bubbles_psd=(os.environ.get("PSA_BUBBLES_PSD") or "").strip() or DEFAULT_BUBBLES_PSD,
            out_dir=os.environ.get("PSA_OUT_DIR") or os.path.join(ep, "psassist"),
        )


# ------------------------------------------------------------------ 個別計算
def bubble_rect(side: str, w: int = spec.BUBBLE_W, h: int = spec.BUBBLE_H) -> list[int]:
    """バブルの矩形 [x0, y0, x1, y1] を画面位置から決める。"""
    if side == "left":
        x0 = spec.BUBBLE_MARGIN_X
    else:
        x0 = spec.CANVAS_W - spec.BUBBLE_MARGIN_X - w
    y0 = spec.BUBBLE_CENTER_Y - h // 2
    return [x0, y0, x0 + w, y0 + h]


def fit_text(text: str, kind: str, box_w: int, box_h: int) -> dict[str, Any]:
    """収まるフォントサイズと行分割を見積もる。

    最終的な収まりは JSX が実描画を測って決めるが、ここで見積もっておくと
    「人の判断が要るパネル」を Photoshop を起動する前に洗い出せる。
    """
    size = spec.FONT_SIZE
    while size >= spec.FONT_MIN:
        scale = size / spec.FONT_SIZE
        cpl = box_w / (spec.EST_CHAR_W * scale)
        lines = kinsoku.wrap(text, cpl)
        if len(lines) * (spec.EST_LINE_PITCH * scale) <= box_h:
            return {
                "size": round(size, 1),
                "lines": lines,
                "fits": True,
                "chars_per_line": round(cpl, 1),
            }
        size -= spec.FONT_STEP

    scale = spec.FONT_MIN / spec.FONT_SIZE
    cpl = box_w / (spec.EST_CHAR_W * scale)
    return {
        "size": spec.FONT_MIN,
        "lines": kinsoku.wrap(text, cpl),
        "fits": False,
        "chars_per_line": round(cpl, 1),
    }


def load_config(out_dir: str) -> dict[str, Any]:
    """プロジェクト単位の設定（psassist/config.json）。

    `time_of_day` は昼夜の使い分け。「隠し部屋でも陽光の入る不思議な構造」
    という設定なので昼背景は必要で、題材によって夜が適切な話数もある
    （例: 猫の秘密＝夜 / ニュース寄り＝昼）。組み合わせも指定できる。
    """
    path = os.path.join(out_dir, "config.json")
    cfg: dict[str, Any] = {"time_of_day": list(spec.DEFAULT_TIME_OF_DAY)}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    env = os.environ.get("PSA_TIME_OF_DAY")
    if env:
        cfg["time_of_day"] = [s.strip() for s in env.split(",") if s.strip()]
    return cfg


def save_config(out_dir: str, cfg: dict[str, Any]) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    return path


def load_measured_shots(out_dir: str) -> dict[str, str]:
    """マスクから実測したショットサイズ（scripts/measure_shot.py の出力）。

    ★`slot.shot`（LLMが付けたラベル）は信用できない。実測との一致は 38% で、
      とくに `face_closeup` は 59行と記録されているが**実際は2枚しかない**
      （164枚中1%）。背景の画角合わせをラベルに委ねると全部ずれるため、
      測れたものは実測値を優先する。測れない行だけラベルへフォールバック。
    """
    path = os.path.join(out_dir, "shot_measured.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {k: v["measured"] for k, v in data.items() if v.get("measured")}


def load_mask_stats(out_dir: str) -> dict[str, dict]:
    """batch_cutout.py が出したキャラ位置。左右の判定に使う（無ければ空）。"""
    path = os.path.join(out_dir, "mask_stats.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _library_cutout_path(panel: dict, backgrounds_dir: str) -> str | None:
    """`cutout_slot_id` が指すキャラ所有ライブラリの切り抜き実体（絶対パス）。

    Aロール側（scrapping-agent）が「この行はこの素材を使え」と書いた指定を読むだけ。
    **選択のロジックは持たない**（指紋での選び方は scrapping-agent の cutout_selector が本籍）。
    """
    slot_id = panel.get("cutout_slot_id")
    char_id = panel.get("cutout_char_id") or (panel.get("characters") or [None])[0]
    if not slot_id or not char_id:
        return None
    # shared/ の位置は backgrounds_dir から導ける（背景アーカイブは shared/backgrounds と
    # 決まっている。Paths.from_env と同じ前提）。新しい env を増やさない。
    shared = os.path.dirname(backgrounds_dir.rstrip("/\\"))
    return os.path.join(shared, "characters", char_id, "panel_library", "cutouts", "%s.png" % slot_id)


def _library_entry(panel: dict, backgrounds_dir: str, cache: dict[str, dict]) -> dict | None:
    """`cutout_slot_id` が指す在庫エントリ本体（``library.json`` 1件）。採寸(``mask``)を取るためだけに使う。

    ⚠️ **在庫の絵を貼る行は、採寸もこのエントリの ``mask`` を使うこと。**
    ``mask_stats.json``（``load_mask_stats``）はこの話数で**生成した**絵の採寸であって、
    在庫から選ばれた絵とは別物。混同すると、貼った絵と違う絵の顔位置でバブルの左右・
    キャラの移動量を決めることになる（2026-09-02実測: ep01 28行中11行(39%)で左右が
    逆になっていた。詳細 ``Docs/AROLL_PSASSIST_REFACTOR_PLAN.md`` §0-1）。

    ``mask`` を持たないエントリ（バックフィル前の在庫）では None を返す。
    呼び出し側は ``mask_stats.json`` へフォールバックせず、``spec.side_from_mask(None)``
    の既定経路（``NO_MASK`` 警告→話者既定の左右）に委ねること。

    cache: char_id → {slot_id: entry} のインデックス（1話数内で複数行が同じキャラを
    指すので、行ごとに ``library.json`` を読み直さない）。
    """
    slot_id = panel.get("cutout_slot_id")
    char_id = panel.get("cutout_char_id") or (panel.get("characters") or [None])[0]
    if not slot_id or not char_id:
        return None
    if char_id not in cache:
        shared = os.path.dirname(backgrounds_dir.rstrip("/\\"))
        path = os.path.join(shared, "characters", char_id, "panel_library", "library.json")
        idx: dict[str, dict] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                idx = {e["slot_id"]: e for e in json.load(fh).get("entries", []) if e.get("slot_id")}
        cache[char_id] = idx
    return cache[char_id].get(slot_id)


def load_overrides() -> dict[str, dict]:
    """ユーザーの実見に基づく背景の制約（assets/background_overrides.json）。

    アーカイブの `framing` は生成時の属性なので、写り込んだ物のスケール感までは
    表せない（例: 巨大な本が写る背景はバスト以上の引きで遠近感が破綻する）。
    全数精査は現実的でないため、違和感の報告を受けた都度ここへ追記する運用。

    ⚠️ **既定パスを `PSA_BUBBLES_PSD` から導かないこと。** その env は未設定でも
    `DEFAULT_BUBBLES_PSD` で動くため、ここだけ `"."` に落ちて**黙って空の
    overrides を返していた**（2026-08-30 修正）。リポ内の assets を直接見る。
    """
    path = os.environ.get("PSA_BG_OVERRIDES") or os.path.join(
        spec.ASSETS_DIR, "background_overrides.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("overrides", {})


def usable_for_shot(bg: dict, shot: str | None, overrides: dict[str, dict]) -> bool:
    ov = overrides.get(bg.get("bg_id", ""))
    if not ov:
        return True
    if ov.get("banned"):
        return False
    allowed = ov.get("allowed_shots")
    return not (allowed and shot and shot not in allowed)


def load_backgrounds(backgrounds_dir: str) -> dict[str, dict]:
    path = os.path.join(backgrounds_dir, "backgrounds.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {b["bg_id"]: b for b in data.get("backgrounds", [])}


# 感情 → 演出背景の相性。psych/comic は数が少ない（各4件）ので、
# 候補として**全部**を回す前提で優先順だけ決める。
# ⚠️ かつてここに「感情→演出背景のID」の対応表を持っていたが**削除した**（2026-08-24）。
#    YT-Studio 側に `EMOTION_TO_MOOD`（感情→mood）が既にあり、背景側も `mood` タグを
#    持っているのに、それを無視してIDを直接並べる表を作っていた＝1事実1ホーム違反。
#    いまは「感情 → mood → その mood を持つ背景」で引く（`pick_fx`）。
#    背景を1枚足したら mood を付けるだけで自動的に候補に入る。
FX_PER_PANEL = 3


# 演出背景（心理・コミック）を差し込む感情。**部屋背景が基本形**であり、
# 心理コミックはその「アクセント」なので、全行に無理に入れない。
# 感情タグが無い行・説明に徹する行（serious/neutral）には既定で付けない。
# ⚠️ 説明役のセリフは感情タグが付きにくいが、「衝撃の事実」や
#    視聴者への弔意など、部屋背景より心理背景が相応しい行はある。
#    そこは config.json の `force_accent` に line_id を並べて個別指定する。
ACCENT_EMOTIONS = frozenset(
    {"surprised", "angry", "excited", "question", "troubled", "sad", "thoughtful", "shy", "happy"}
)


def load_emotion_to_mood(backgrounds_dir: str) -> dict[str, list[str]]:
    """感情→mood の対応表。

    **正本は YT-Studio の `background_presets.py: EMOTION_TO_MOOD`**。
    そこから `shared/imagegen/background_presets.json` へ書き出されたものを読む
    （こちらで持つと二重管理になる）。読めない時は空＝アクセントを付けない。
    """
    path = os.path.join(os.path.dirname(backgrounds_dir.rstrip("/\\")), "imagegen",
                        "background_presets.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("emotion_to_mood", {})


def pick_fx(
    emotion: str | None,
    order: int,
    backgrounds: dict[str, dict],
    emo2mood: dict[str, list[str]],
    *,
    force: bool = False,
) -> list[str]:
    """セリフの心理状態に合う演出背景を選ぶ。

    心理コミック背景は**遠近感を無視できるのでどのショットにも使える**。
    したがって画角ではなく **`mood`（感情）で選ぶ**のが正しい軸。
    背景側の `mood` タグと、感情から引いた mood 集合の重なりで照合する。
    """
    if not force and (emotion or "") not in ACCENT_EMOTIONS:
        return []  # 説明に徹する行にはアクセントを入れない
    want = set(emo2mood.get(emotion or "", []))
    if not want:
        return []
    pool = [
        b
        for b in backgrounds.values()
        if b.get("category") in ("psych", "comic", "backdrop")
        and want & set(b.get("mood") or [])
    ]
    if not pool:
        return []
    # 一致する mood が多い順 → 使用回数が少ない順。同点は行ごとに回転させて散らす
    pool.sort(key=lambda b: (-len(want & set(b.get("mood") or [])), b.get("times_used", 0),
                             b["bg_id"]))
    start = (order or 0) % len(pool)
    return [pool[(start + i) % len(pool)]["bg_id"] for i in range(min(FX_PER_PANEL, len(pool)))]

def pick_eff(emotion: str | None, order: int, backgrounds: dict[str, dict],
             emo2mood: dict[str, list[str]]) -> list[str]:
    """集中線（`effect`）も同じく mood で引く。キャラの後ろ・背景の前に重ねる。

    「衝撃の事実」系の場面で使う素材なので、感情が強い行にだけ控えさせる
    （既定は非表示。ユーザーが目玉を入れて初めて効く）。
    """
    if (emotion or "") not in ACCENT_EMOTIONS:
        return []
    want = set(emo2mood.get(emotion or "", []))
    pool = [b for b in backgrounds.values()
            if b.get("category") == "effect" and want & set(b.get("mood") or [])]
    if not pool:
        return []
    pool.sort(key=lambda b: (-len(want & set(b.get("mood") or [])), b.get("times_used", 0),
                             b["bg_id"]))
    start = (order or 0) % len(pool)
    return [pool[(start + i) % len(pool)]["bg_id"] for i in range(min(2, len(pool)))]


def pick_location(
    bg_id: str | None,
    backgrounds: dict[str, dict],
    allowed_lights: set[str],
    char_dx: float | None,
    shot: str | None = None,
    overrides: dict[str, dict] | None = None,
) -> tuple[str | None, str]:
    """時間帯と光源の向きに合う場所背景を選ぶ。(bg_id, 選定理由)。

    ①割当済みが時間帯に合っていて光源も逆向きでなければ、それを使う
    ②合わなければ同じ spot の中から、時間帯に合い光源の一致するものへ差し替える
    ③それも無ければ時間帯だけ合わせる
    """
    cur = backgrounds.get(bg_id) if bg_id else None
    if cur is None or cur.get("category") != "location":
        return bg_id, "as_assigned"

    def ok_light(b: dict) -> bool:
        return not allowed_lights or b.get("light") in allowed_lights

    def agree(b: dict) -> float:
        return spec.light_agreement(char_dx, b.get("light_dx"))

    # 画角が合っていることを最優先にする。寄りのキャラに引きの背景、のような
    # 不整合はユーザーから「サイズが合わず不自然」と指摘された最大の要因。
    def ok_framing(b: dict) -> bool:
        return spec.framing_rank(shot, b.get("framing")) == 0

    if ok_light(cur) and agree(cur) >= 0 and ok_framing(cur):
        return bg_id, "as_assigned"

    ov = overrides or {}
    pool = [
        b
        for b in backgrounds.values()
        if b.get("category") == "location" and ok_light(b) and usable_for_shot(b, shot, ov)
    ]
    if not pool:
        return bg_id, "no_alternative"

    # ⚠️ spot は**絶対条件にしない**。定点によっては光源の向きが構造的に決まって
    #    しまう（例: window-seat は窓が必ず左＝light_dx が全て負）ため、spot に
    #    縛ると光源の矛盾が永久に解けない。優先順は 光源 → 同じ定点 → 使用回数。
    # 優先順: 画角 → 光源 → 同じ定点 → 使用回数
    best = min(
        pool,
        key=lambda b: (
            spec.framing_rank(shot, b.get("framing")),
            -agree(b),
            0 if b.get("spot") == cur.get("spot") else 1,
            b.get("times_used", 0),
        ),
    )
    if best["bg_id"] == bg_id:
        return bg_id, "as_assigned"
    if not ok_framing(cur):
        return best["bg_id"], "framing"
    if not ok_light(cur):
        return best["bg_id"], "time_of_day"
    return best["bg_id"], "light_direction"


def spare_locations(
    chosen: str | None,
    backgrounds: dict[str, dict],
    allowed_lights: set[str],
    char_dx: float | None,
    shot: str | None = None,
    overrides: dict[str, dict] | None = None,
    n: int = 2,
) -> list[str]:
    """部屋背景の予備を n 件返す（採用したものとは別の定点から選ぶ）。

    `light_dx` は「画像のどこが明るいか」で測るため、窓が画面中央寄りに描かれた
    絵などでは**機械の判定と人の見え方がずれる**ことがある（実例 line_014:
    明るいのは右だが、人が見れば光源は左の窓）。実害の無い程度のズレだが、
    そういう時に目玉の切替だけで直せるよう控えを積んでおく。
    """
    cur = backgrounds.get(chosen) if chosen else None
    pool = [
        b
        for b in backgrounds.values()
        if b.get("category") == "location"
        and b["bg_id"] != chosen
        and (not allowed_lights or b.get("light") in allowed_lights)
        and usable_for_shot(b, shot, overrides or {})
    ]
    if not pool:
        return []
    # 予備も画角を最優先で揃える（採用と同じ基準でないと差し替えた瞬間に破綻する）
    pool.sort(
        key=lambda b: (
            spec.framing_rank(shot, b.get("framing")),
            -spec.light_agreement(char_dx, b.get("light_dx")),
            b.get("spot") == (cur or {}).get("spot"),  # 採用と別の定点を優先
            b.get("times_used", 0),
        )
    )
    return [b["bg_id"] for b in pool[:n]]


def background_candidates(
    bg_id: str | None,
    backgrounds: dict[str, dict],
    backgrounds_dir: str,
    *,
    fx_ids: list[str] | None = None,
    overlay_ids: list[str] | None = None,
    spare_ids: list[str] | None = None,
) -> list[dict]:
    """第1候補＝割当済みの背景、第2以降＝感情に合う演出背景。

    JSX は全部をレイヤーとして置き、第1候補だけ表示する。ユーザーは目玉を
    切り替えるだけで差し替えられる（コミック＋心理の重ね使いもできる）。

    `effect`（集中線）は **キャラの後ろ・背景の前** に置く別枠なので、
    `overlay` フラグを立てて JSX に伝える。
    """
    ids: list[str] = []
    if bg_id:
        ids.append(bg_id)
    # 部屋背景の予備。光源の判定は「画像の明るい部分」に基づくので、窓が画面中央
    # 寄りにある絵など**機械と人の見え方がずれるパターン**では外すことがある。
    # そういう時に目玉を切り替えるだけで済むよう、控えを先に積んでおく。
    for cand in spare_ids or []:
        if cand not in ids:
            ids.append(cand)
    for cand in fx_ids or []:
        if cand not in ids:
            ids.append(cand)
    for cand in overlay_ids or []:
        if cand not in ids:
            ids.append(cand)

    out = []
    for i, bid in enumerate(ids):
        bg = backgrounds.get(bid)
        if bg is None:
            continue
        category = bg.get("category")
        out.append(
            {
                "bg_id": bid,
                "image": os.path.join(backgrounds_dir, bg["image"].replace("/", os.sep)),
                "category": category,
                "light": bg.get("light"),
                "light_dx": bg.get("light_dx"),
                # effect は透過素材。キャラの後ろ・背景の前に重ねる
                "overlay": category == "effect",
                "visible": i == 0,
                # 全カテゴリに乗せる＝どの候補を選んでもブラーと色調整が揃う
                "blur": spec.blur_for(category),
                "opacity": 100,
                "blend_mode": "normal",
            }
        )
    return out


# ------------------------------------------------------------------ 本体
def build(paths: Paths | None = None) -> dict[str, Any]:
    paths = paths or Paths.from_env()
    aroll_path = os.path.join(paths.episode_dir, "a_roll", "aroll.json")
    with open(aroll_path, encoding="utf-8") as fh:
        aroll = json.load(fh)
    backgrounds = load_backgrounds(paths.backgrounds_dir)
    masks = load_mask_stats(paths.out_dir)
    library_entry_cache: dict[str, dict] = {}
    cfg = load_config(paths.out_dir)
    overrides = load_overrides()
    # 話者ごとの既定バブルはチャンネル固有データ。import時ではなくここで読む
    #（build_plan.py は load_root_env() を import の後に呼ぶため）
    speaker_defaults = spec.load_speaker_defaults()
    measured_shots = load_measured_shots(paths.out_dir)
    emo2mood = load_emotion_to_mood(paths.backgrounds_dir)
    # 感情タグが無くても演出背景を入れたい行（説明台詞での「衝撃の事実」・弔意など）。
    # ここは機械では判定できないのでユーザーが line_id を指定する。
    force_accent = set(cfg.get("force_accent") or [])
    allowed_lights = spec.lights_for(cfg.get("time_of_day", []))

    panels: list[dict[str, Any]] = []
    for p in aroll.get("panels", []):
        text = (p.get("text") or "").strip()
        if not text:
            continue  # 空行はパネル対象外（DATA_SCHEMA §6d と同じ扱い）

        warnings: list[str] = []
        speaker = p.get("speaker_name")
        default = speaker_defaults.get(speaker, spec.FALLBACK_DEFAULT)
        if speaker not in speaker_defaults:
            warnings.append("UNKNOWN_SPEAKER")

        # 形状は文中記号（！/？）で行ごとに上書きできる（未設定の話者は既定のまま＝後方互換）。
        bubble_key, bubble_key_source = spec.bubble_key_for(default, text)
        shape = spec.BUBBLE_BY_KEY[bubble_key]
        # 左右はマスク（顔の位置）から決めるのが最良（82%）。マスクが無ければ
        # 話者別の最頻値へフォールバック（64%）。
        # ⚠️ 在庫の絵を貼る行（cutout_slot_id あり）は、貼る絵と同じ絵の mask を使う。
        # mask_stats.json はこの話数で生成した別の絵の採寸なので、ここでは使わない
        # （黙って流用すると貼った絵と違う顔位置でバブル左右が決まる。詳細は _library_entry）。
        lib_entry = _library_entry(p, paths.backgrounds_dir, library_entry_cache)
        mask = lib_entry.get("mask") if lib_entry is not None else masks.get(p["line_id"])
        side = spec.side_from_mask(mask)
        side_source = "mask"
        if side is None:
            side, side_source = default.side, "speaker_default"
            warnings.append("NO_MASK")
        # 重なりの解消は3段階。上の段ほど失うものが少ない。
        #   ① キャラを移動   … 何も失わない
        #   ② キャラを縮小   … キャラは小さくなるがバブルは全幅＝文字が大きいまま
        #   ③ バブルを縮小   … 文字が小さくなるので最後の手段
        offset_x, ov = spec.character_offset(mask, bubble_rect(side), side)
        char_scale = ov.get("scale", 1.0)
        if ov.get("resolved") and (offset_x != 0 or char_scale < 1.0):
            bw, overlaps_face = spec.BUBBLE_W, False  # ①か②で解決＝全幅で置ける
        else:
            offset_x, char_scale = 0, 1.0
            bw, overlaps_face = spec.bubble_width_for(mask, side)
        if overlaps_face:
            # 移動でもバブル縮小でも顔を守れなかった時だけユーザーへ回す。
            warnings.append("BUBBLE_OVERLAPS_FACE")
            if not ov.get("resolved", True):
                # 自動手段（移動・バブル縮小）を出し切っても重なりが残る。
                # キャラの縮小は不採用なので（切断面が露出するため）手作業へ。
                warnings.append("BUBBLE_OVERLAP_UNRESOLVED")
        rect = bubble_rect(side, w=bw)
        inner = spec.inner_rect(shape.kind, *rect)
        box_w, box_h = inner[2] - inner[0], inner[3] - inner[1]
        fit = fit_text(text, shape.kind, box_w, box_h)

        split = None
        if not fit["fits"]:
            warnings.append("TEXT_OVERFLOW")
            split = kinsoku.split_for_bubbles(text, spec.SPLIT_MAX_CHARS)
            if split is None:
                warnings.append("NO_SPLIT_POINT")

        # 画像は「可変値をファイル名に焼かない」規則に従い line_id で引く
        # 在庫から選ばれた切り抜きがあれば、それを使う（キャラ所有ライブラリの実体を指す）。
        # 無ければ従来どおり、この話数で生成した絵（psassist/cutout/panel_{line_id}.png）。
        # 背景は既にフルパスで渡しているので、キャラも同じ形にできる（bridge が絶対パスを尊重する）。
        lib_cut = _library_cutout_path(p, paths.backgrounds_dir)
        if lib_cut:
            img = lib_cut
            if not os.path.exists(img):
                warnings.append("LIBRARY_CUTOUT_NOT_FOUND")
        else:
            img = "panel_%s.png" % p["line_id"]
            img_path = os.path.join(paths.episode_dir, "a_roll", img)
            if not os.path.exists(img_path):
                warnings.append("PANEL_IMAGE_NOT_FOUND")

        bg_id = p.get("background_id")
        if bg_id and bg_id not in backgrounds:
            warnings.append("BACKGROUND_NOT_IN_ARCHIVE")
        elif not bg_id:
            warnings.append("NO_BACKGROUND_ASSIGNED")

        # 時間帯（プロジェクト設定）と光源の向きに合わせて場所背景を選び直す
        char_dx = (mask or {}).get("light_dx")
        slot = p.get("slot") or {}
        emotion = slot.get("emotion")
        # 実測を優先。測れなかった行だけ LLM のラベルを使う
        shot = measured_shots.get(p["line_id"]) or slot.get("shot")
        shot_source = "measured" if p["line_id"] in measured_shots else "slot_label"
        bg_id, bg_reason = pick_location(
            bg_id, backgrounds, allowed_lights, char_dx, shot, overrides
        )
        bg = backgrounds.get(bg_id) if bg_id else None
        if bg_reason == "light_direction":
            warnings.append("LIGHT_MISMATCH_FIXED")

        panels.append(
            {
                "line_id": p["line_id"],
                "order": p.get("order"),
                "speaker": speaker,
                "canvas": [spec.CANVAS_W, spec.CANVAS_H],
                "character": {
                    "image": img,
                    "cutout": True,
                    # 吹き出しは必ず読めなければならない。重なる場合はキャラを
                    # 反対側へ逃がす（マスクからの厳密計算。AI 不要）
                    "offset": [offset_x, 0],
                    # 移動で逃がせない時は縮小する。基点はバブルと反対側の下角。
                    "scale": char_scale,
                    "overlap": ov.get("overlap"),
                    "overlap_resolved": ov.get("resolved"),
                },
                "background": {
                    "bg_id": bg_id,
                    "bg_source": bg_reason,
                    "image": (
                        os.path.join(paths.backgrounds_dir, bg["image"].replace("/", os.sep))
                        if bg
                        else None
                    ),
                    "blur": spec.DEFAULT_BLUR_RADIUS,
                    "add_neutral_adjustment": True,  # 色調整はスライダーだけで済むよう先置き
                    "light_agreement": spec.light_agreement(
                        char_dx, (bg or {}).get("light_dx")
                    ),
                    # 第2候補以降も非表示レイヤーとして置く（目玉の切替で差し替え）
                    "candidates": background_candidates(
                        bg_id,
                        backgrounds,
                        paths.backgrounds_dir,
                        fx_ids=pick_fx(
                            emotion, p.get("order") or 0, backgrounds, emo2mood,
                            force=p["line_id"] in force_accent,
                        ),
                        overlay_ids=pick_eff(emotion, p.get("order") or 0, backgrounds, emo2mood),
                        spare_ids=spare_locations(
                            bg_id, backgrounds, allowed_lights, char_dx, shot, overrides
                        ),
                    ),
                },
                "bubble": {
                    "key": bubble_key,
                    "key_source": bubble_key_source,  # speaker_default / question / exclaim
                    "layer": shape.layer,
                    "kind": shape.kind,
                    "rect": rect,
                    "flip_h": spec.default_flip_h(bubble_key, side),
                    "flip_v": spec.FLIP_V_DEFAULT,
                    "side": side,
                    "side_source": side_source,
                },
                "text": {
                    "raw": text,
                    "lines": fit["lines"],
                    "font": spec.FONT_POSTSCRIPT,
                    "size": fit["size"],
                    "color": list(spec.FONT_COLOR_RGB),
                    "direction": spec.DEFAULT_DIRECTION,
                    "box": inner,
                    "split_suggestion": list(split) if split else None,
                },
                "layer_order": list(spec.LAYER_ORDER),
                # BUBBLE_OVERLAPS_FACE は助言（そのまま組めるし、縮めても改善しない）。
                # 作業を止める必要があるものだけ needs_attention にする。
                "status": (
                    "needs_attention"
                    if [w for w in warnings if w not in ADVISORY_WARNINGS]
                    else "ok"
                ),
                "warnings": warnings,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": aroll.get("project_id"),
        "episode": aroll.get("episode"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": asdict(paths),
        "config": cfg,
        "defaults": {
            "time_of_day": cfg.get("time_of_day"),
            "allowed_lights": sorted(allowed_lights),
            "bubble_size": [spec.BUBBLE_W, spec.BUBBLE_H],
            "bubble_center_y": spec.BUBBLE_CENTER_Y,
            "font": spec.FONT_POSTSCRIPT,
            "font_size": spec.FONT_SIZE,
            "font_min": spec.FONT_MIN,
            "blur": spec.DEFAULT_BLUR_RADIUS,
            "speakers": {k: asdict(v) for k, v in speaker_defaults.items()},
        },
        "panels": panels,
    }


def write(plan: dict[str, Any], out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "panel_plan.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=1)
    return path
