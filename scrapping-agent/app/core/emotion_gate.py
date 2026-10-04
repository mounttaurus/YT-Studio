"""登録ゲート＝在庫に入る絵の表情を観察し、依頼と食い違えば観察を採る（`Docs/STOCK_LABEL_ACCURACY_PLAN.md` §4-5・L4・T12）。

再発防止の本体。今までは登録関数が渡された感情（＝生成の依頼）をそのまま書いていたので、誤ったラベルの絵が
在庫に積まれ続けた。入口ごとの扱い（`entrance`）:

| entrance | 入口 | 一致 | 食い違い | 判定不能 |
|---|---|---|---|---|
| aroll_generated | ① Aロールの生成物の自動登録（pending） | 依頼のまま | 主タグ＝観察・pending | pending |
| approve | ② 行の絵の承認時の登録（既定 approved） | approved | 主タグ＝観察・**pending** | pending |
| library | ③ 在庫の生成・バリアント | 呼び出し側の既定 | 主タグ＝観察・**pending** | pending |
| upload | ④ ユーザーのアップロード（pending） | pending | **ラベルは変えない**（人が選んだ）・観察は下書き | pending |

- **食い違った絵は捨てない**: 観察した感情の在庫として残す（question を頼んで serious が出たなら良い serious の絵）。
  依頼は `emotion_requested` に残る（レシピの的中率を測る＝L5）。観察の副タグに依頼の感情があれば、その段で届く。
- **失敗しても登録は止めない**（I6・fail-open）: 観察できなかった絵は `status: error|unchecked` のまま入口の既定で入る。
- ゲートが有効な時、呼び出し側は `initial_status()` で**一旦 pending で登録**してから `apply()` を呼ぶ
  （観察が終わる前に自動選択へ出さない）。無効（`EMOTION_VISION_MODEL` 空）なら何も変えない＝今と同じ。
- 書くのは `panel_library_manager.set_emotion_tags`（タグ・source="vision"）と `set_emotion_check`（観察・承認状態）だけ。
"""
from __future__ import annotations

import logging

from app.core import emotion_vision, panel_library_manager

log = logging.getLogger(__name__)

ENTRANCES = ("aroll_generated", "approve", "library", "upload")


def enabled() -> bool:
    return emotion_vision.enabled()


def initial_status(target: str) -> str:
    """登録時の review_status。ゲートが有効なら観察が終わるまで pending、無効なら入口の既定（target）。"""
    return "pending" if enabled() else target


def message(res: dict) -> str:
    """生成ログ・UI・MCP に出す一文（一致は空）。"""
    st = res.get("status")
    if st == "disagree":
        tail = "観察した感情の在庫として pending で登録" if res.get("adopted") else "ラベルはそのまま（人が選んだラベル）"
        return f"⚠️ 表情が依頼と違うかも（依頼={res.get('requested')}／観察={res.get('observed')}）。{tail}"
    if st == "ambiguous":
        return "⚠️ 表情を判定できませんでした（人が確認するまで在庫に出しません）"
    if st == "error":
        return f"ℹ️ 表情の観察に失敗しました（依頼のラベルのまま登録）: {res.get('error', '')[:120]}"
    return ""


async def apply(char_id: str, slot_id: str, *, entrance: str, target_status: str) -> dict:
    """登録直後の1枚を観察して、タグと承認状態を決める。結果の要約を返す（失敗しても例外を出さない）。

    target_status: その入口の既定の review_status（一致・観察失敗の時に使う）。
    """
    if entrance not in ENTRANCES:
        raise ValueError(f"unknown entrance: {entrance}")
    if not enabled():
        return {"status": "unchecked", "applied": False, "message": ""}
    entry = panel_library_manager.get_entry(char_id, slot_id)
    if entry is None:
        return {"status": "missing", "applied": False, "message": ""}
    requested = entry.get("emotion")
    try:
        check = await emotion_vision.observe_entry(char_id, entry, requested)
    except Exception as e:  # noqa: BLE001 — observe_entry は例外を出さない設計だが、念のため登録を止めない
        check = {"status": "error", "error": f"{type(e).__name__}: {e}", "requested": requested}
    st = check.get("status")

    adopted = False
    if st == "disagree" and entrance != "upload":
        try:
            panel_library_manager.set_emotion_tags(
                char_id, {slot_id: {"tag": check["primary"], "subs": check.get("secondary") or []}},
                source="vision", dry_run=False, backup=False, log=False)
            adopted = True
        except Exception as e:  # noqa: BLE001 — 検査済みの観察なので通常は起きない。起きたら依頼のまま止める
            log.warning("登録ゲート: 観察のタグを書けません %s/%s: %s", char_id, slot_id, e)
    final = target_status if st in ("agree", "unchecked", "error") else "pending"
    panel_library_manager.set_emotion_check(char_id, slot_id, check, review_status=final)

    res = {"status": st, "applied": True, "entrance": entrance, "requested": requested,
           "observed": check.get("primary"), "observed_family": check.get("family"),
           "adopted": adopted, "review_status": final, "model": check.get("model"),
           "cost_usd": check.get("cost_usd"), "error": check.get("error")}
    res["message"] = message(res)
    return res
