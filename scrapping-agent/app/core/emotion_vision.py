"""視覚ラベラー＝在庫の絵の表情を**登録時に1回だけ**観察する（`Docs/STOCK_LABEL_ACCURACY_PLAN.md` §4-4・L4・T6）。

在庫の感情ラベルは「生成の依頼」であって観察ではない（目視で別の感情 21%）。登録の入口で顔を見て、
依頼と食い違えば観察を採る（登録ゲート `emotion_gate`）。**選択時には呼ばない**（選択はラベルを信じる）。

- 入力: 切り抜きから顔を切り出した画像（頭部の bbox・短辺 512px 以上・灰色の背景）。腰上以下の構図は
  全体を渡すと顔が小さく判定が荒れる（一次ラベル付けでも顔を切り出さないと読めなかった）。
- 物差し: `emotion_rubric`（付録 A＝人・私・視覚モデルが同じ表で判定する）。細かいタグで答えさせ、系統は辞書から引く。
- 出力の検査: 主タグが辞書に無ければ1回だけ聞き直す→だめなら次のモデル→全部だめなら `error`。
  副タグの不正（隣接外・陽↔陰・3つ以上）は**その副タグだけ捨てる**（主タグが正しい観察を無駄にしない）。
- モデル: `.env` の `EMOTION_VISION_MODEL`（カンマ区切りで順に試す）。**空なら無効**＝公開リポの利用者・
  鍵の無い環境では観察しない（`unchecked`）。どのモデルを使うかは較正（`scripts/emotion_calibrate.py`）で決める。
- 失敗しても登録は止めない（不変条件 I6・fail-open）。結果は `status: error` として残る。
- ⚠️ 外部 API へ画像を送る（Anthropic・Google・OpenRouter）。本番で有効にするのは較正に合格してから（G4）。
"""
from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime, timezone

from PIL import Image

from app.core import emotion_rubric, llm_client, panel_library_manager

FACE_MIN_SIDE = 512
STATUS_VALUES = ("agree", "disagree", "ambiguous", "error", "unchecked")


def models() -> list[str]:
    """観察に使うモデル（順に試す）。`EMOTION_VISION_MODEL` が空なら [] ＝無効。"""
    return [m.strip() for m in os.getenv("EMOTION_VISION_MODEL", "").split(",") if m.strip()]


def enabled() -> bool:
    return bool(models())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------- 入力（顔の切り出し）

def face_crop(entry: dict, rgba: Image.Image) -> Image.Image:
    """頭部を中心に正方形で切り出し、灰色に合成する（顔アップ系は全体）。

    コンタクトシート（`scripts/emotion_sheets.py`）と視覚ラベラーで同じ切り出しを使う＝人と機械が同じ顔を見る。
    頭部の位置は登録時に測った `mask`（`cutout_engine.analyze_alpha`）から取る。
    """
    m = entry.get("mask") or {}
    bb = m.get("bbox") or [0, 0, rgba.width, rgba.height]
    hx = m.get("head_bbox_x") or [bb[0], bb[2]]
    if entry.get("shot") in ("face_closeup", "face_extreme"):
        box = (bb[0], bb[1], bb[2], bb[3])
    else:
        side = int(max(hx[1] - hx[0], 1) * 1.5)
        cx = (hx[0] + hx[1]) // 2
        box = (cx - side // 2, bb[1], cx + side // 2, bb[1] + side)
    box = (max(0, box[0]), max(0, box[1]), min(rgba.width, box[2]), min(rgba.height, box[3]))
    c = rgba.crop(box)
    bg = Image.new("RGBA", c.size, (200, 200, 200, 255))
    bg.alpha_composite(c)
    return bg.convert("RGB")


def face_png(char_id: str, entry: dict) -> bytes:
    """在庫の1枚 → 視覚モデルに渡す顔の PNG（短辺 FACE_MIN_SIDE 以上に拡大）。切り抜きが無ければ ValueError。"""
    rel = entry.get("cutout")
    if not rel:
        raise ValueError("切り抜きが無い絵は観察できません")
    path = panel_library_manager.library_dir(char_id) / rel
    rgba = Image.open(path).convert("RGBA")
    face = face_crop(entry, rgba)
    short = min(face.size)
    if short < FACE_MIN_SIDE:
        k = FACE_MIN_SIDE / max(short, 1)
        face = face.resize((round(face.width * k), round(face.height * k)), Image.LANCZOS)
    buf = io.BytesIO()
    face.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------- 物差し（プロンプト）

# 指示文の版。v1＝ルーブリックの表だけ。v2＝人の判定（金標準）と食い違いが集中した境界の4組に目安を足した版
# （2026-10-03 較正1回目: 観察が人より強く読む一方向の外れ＝neutral→serious 33・surprised→question 17・
#  serious→angry 16・sad→troubled 12 が外れの6割）。目安は金標準の調整用の半分だけを見て書く（検証用は見ない）。
PROMPT_VERSION = os.getenv("EMOTION_VISION_PROMPT", "v2")
BOUNDARY_NOTES_V2 = (
    "## Boundaries people draw (follow these when two families are close)\n"
    "- neutral vs serious: anime faces are often drawn with straight brows and a closed mouth by default — that is "
    "NEUTRAL. Choose serious only with clear tension: brows lowered or drawn together, lips pressed tight, AND a fixed, "
    "intent stare. If unsure between neutral and serious, choose neutral.\n"
    "- serious vs angry: angry needs visible anger: brows sharply slanted down toward the center with a furrow, a glare, "
    "clenched or bared teeth, or shouting. Lowered brows with a closed mouth alone are serious.\n"
    "- surprised vs question: wide-open eyes with raised brows (often an open mouth) are SURPRISED, even with a head "
    "tilt or a sweat drop. question is a puzzled, probing look WITHOUT the wide-eyed shock: uneven brows, slight squint "
    "or tilt, a small 'huh?' mouth.\n"
    "- sad vs troubled: downcast or teary eyes, drooping mouth corners, a dejected look are SAD. troubled is anxious or "
    "flustered: worried eyes, wavy mouth, sweat drops, with no dejection. Tears or drooping mouth corners -> sad.\n\n"
)


def build_prompt(rubric: dict | None = None, version: str | None = None) -> str:
    """ルーブリック（系統の判定基準）と細かいタグの辞書を入れた指示文。人の確認と同じ表を使う。
    version: 指示文の版（`PROMPT_VERSION`）。較正で版どうしを比べる。"""
    r = rubric or emotion_rubric.load_rubric()
    version = version or PROMPT_VERSION
    tag_rows = ["| tag | family | visual cues |", "|---|---|---|"]
    for t, spec in emotion_rubric.tags(r).items():
        tag_rows.append("| %s | %s | %s |" % (t, spec.get("family") or "-", spec.get("cues", "")))
    return (
        "You label the facial expression of an anime-style character in a stock illustration.\n"
        "The image is a crop around the head. Judge ONLY from the face (brows, eyes, mouth, gaze, head tilt, "
        "blush, sweat, tears). Hands and body pose are NOT emotion cues.\n\n"
        "## Families (emotion groups) and how to tell them apart\n" + emotion_rubric.as_prompt_table(r) + "\n\n"
        "## Tags (pick from this list; each tag belongs to one family)\n" + "\n".join(tag_rows) + "\n\n"
        + (BOUNDARY_NOTES_V2 if version == "v2" else "")
        + "Rules:\n"
        "- `tag`: the single best tag. Use `face_hidden` if the face is not visible (back view, covered).\n"
        "- `sub_tags`: 0-2 other tags that also fit. Their family must be the same as the main tag's family or "
        "listed in its 'allowed secondary'. Never mix positive (happy/excited/shy) with negative (sad/angry/troubled).\n"
        "- `none_fit`: true if no tag fits the face at all.\n"
        "- `confidence`: 0.0-1.0.\n\n"
        "Answer with JSON only, no prose:\n"
        '{"tag": "...", "sub_tags": ["..."], "confidence": 0.0, "none_fit": false, '
        '"cues": {"brows": "...", "eyes": "...", "mouth": "...", "gaze": "..."}, "note": "..."}'
    )


def parse(text: str, rubric: dict | None = None) -> dict:
    """応答の JSON を検査して {tag, family, sub_tags, dropped_sub_tags, confidence, none_fit, cues, note} に。

    主タグが辞書に無い・JSON が読めない時は ValueError（呼び出し側が聞き直す）。
    副タグは検査を通るものだけ残す（主タグの観察まで捨てない）。
    """
    r = rubric or emotion_rubric.load_rubric()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("JSON がありません")
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON が読めません: {e}")
    tag = str(d.get("tag") or "").strip()
    known = emotion_rubric.tags(r)
    if tag not in known:
        raise ValueError(f"辞書に無いタグです: {tag!r}")
    subs, dropped = [], []
    for s in d.get("sub_tags") or []:
        s = str(s).strip()
        if not s or s == tag or s in subs:
            continue
        if len(subs) < emotion_rubric.MAX_SECONDARY and not emotion_rubric.validate_tags(tag, subs + [s], r):
            subs.append(s)
        else:
            dropped.append(s)
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence"))))
    except (TypeError, ValueError):
        conf = None
    return {"tag": tag, "family": emotion_rubric.family_of(tag, r), "sub_tags": subs, "dropped_sub_tags": dropped,
            "confidence": conf, "none_fit": bool(d.get("none_fit")),
            "cues": d.get("cues") if isinstance(d.get("cues"), dict) else {}, "note": str(d.get("note") or "")[:300]}


# --------------------------------------------------------------------- 観察

class ObserveError(ValueError):
    """1モデルでの観察の失敗。失敗までに使った費用を持つ（較正の予算に数えるため）。"""

    def __init__(self, msg: str, cost_usd: float = 0.0):
        super().__init__(msg)
        self.cost_usd = cost_usd


async def observe_image(png: bytes, model: str, rubric: dict | None = None, version: str | None = None) -> dict:
    """1モデルで1枚を観察する。主タグが読めなければ1回だけ聞き直す。費用（USD）を `cost_usd` に積む。"""
    r = rubric or emotion_rubric.load_rubric()
    prompt = build_prompt(r, version)
    cost, last = 0.0, None
    for _ in range(2):
        try:
            text, c = await llm_client.chat_vision(prompt, png, model)
        except Exception as e:  # noqa: BLE001 — 拒否・時間切れ・鍵なし
            raise ObserveError(f"{model}: {type(e).__name__}: {str(e)[:200]}", cost)
        cost += c or 0.0
        try:
            out = parse(text, r)
            out.update(model=model, cost_usd=cost)
            return out
        except ValueError as e:
            last = e
    raise ObserveError(f"{model}: {last}", cost)


def classify(obs: dict, requested: str | None) -> str:
    """観察を依頼（系統）と比べた結果。**主タグの系統が依頼と同じ時だけ agree**（副タグでの一致は agree にしない
    ＝選択の基準を緩めない。食い違った絵は観察の主タグで登録し直し、副タグで依頼の感情に届く）。"""
    if obs.get("none_fit"):
        return "ambiguous"
    if not requested:
        return "ambiguous"
    return "agree" if obs.get("family") == requested else "disagree"


async def observe_entry(char_id: str, entry: dict, requested: str | None = None) -> dict:
    """在庫の1枚を観察して `emotion_check` を作る（書かない。書くのは `emotion_gate`）。

    requested: 比べる依頼（系統）。省略時は entry の `emotion`。
    無効（モデル未設定）なら `unchecked`、全モデル失敗なら `error`（理由つき）。
    """
    r = emotion_rubric.load_rubric()
    base = {"rubric": r.get("version"), "tags_version": emotion_rubric.TAGS_VERSION, "prompt": PROMPT_VERSION, "at": _now(),
            "requested": requested if requested is not None else entry.get("emotion")}
    ms = models()
    if not ms:
        return {**base, "status": "unchecked", "reason": "EMOTION_VISION_MODEL が未設定"}
    try:
        png = face_png(char_id, entry)
    except Exception as e:  # noqa: BLE001 — 画像が読めないだけで登録は止めない
        return {**base, "status": "error", "error": f"顔を切り出せません: {type(e).__name__}: {e}"}
    errors, spent = [], 0.0
    for m in ms:
        try:
            obs = await observe_image(png, m, r)
        except Exception as e:  # noqa: BLE001 — 次のモデルへ（拒否・時間切れ・鍵なし・形式違反）
            spent += getattr(e, "cost_usd", 0.0)
            errors.append(str(e)[:200])
            continue
        spent += obs.get("cost_usd") or 0.0
        return {**base, "status": classify(obs, base["requested"]), "primary": obs["tag"],
                "family": obs["family"], "secondary": obs["sub_tags"], "confidence": obs["confidence"],
                "none_fit": obs["none_fit"], "cues": obs["cues"], "note": obs["note"],
                "model": m, "cost_usd": round(spent, 6), **({"errors": errors} if errors else {})}
    return {**base, "status": "error", "error": " / ".join(errors)[:600], "cost_usd": round(spent, 6)}
