"""切り抜き在庫から1行分の画像を選ぶ（指紋ベース）。

Aロールの「選択」の本体。**slot(emotion/shot/angle)一致では選ばない。**
実測でスロット軸は「使い回しに見えるか」をほとんど説明しなかったため
（emotion 0.01 / angle 0.04 / shot 0.33 に対し**指紋距離 0.72**。Spearman）。
検証と決定の本籍は `psassist/Docs/CHARACTER_CUTOUT_PLAN.md` §9〜§11。

2つの軸で役割を分ける:
  emotion  = **適格性**。悲しい行に笑顔を当てない。**変種数を決める軸ではない**
  指紋距離 = **変化**。直近の行と近すぎる絵を選ばない

選べなかったら `None` を返す＝**それが「新規生成すべき」の合図**。
在庫から無理に選ばない（ワンパターンの発生源になる）。

⚠️ numpy は使わない（scrapping-agent の依存は pillow まで）。候補は1キャラ100件規模、
   距離は 64bit の popcount と 256byte の平均絶対差なので純Pythonで足りる。
"""
import json
import os
from pathlib import Path

from app.core import character_manager, panel_library_manager

OVERRIDES_NAME = "character_overrides.json"

# 較正できていない環境でも動くための既定値（正は character_overrides.json の thresholds）
DEFAULT_THRESHOLDS = {
    "repetitive_below": 0.073,  # これ未満＝使い回しに見える（完成コマで較正・§10-6）
    "max_uses": 3,              # 生涯の使用回数上限（times_used累計・1話内ではない）
    "recent_window": 5,         # 直近何行を「近く」とみなすか（§10-7でK=4が違反0の窓）
}


# --------------------------------------------------------------------- 指紋の距離


def _hamming_ratio(a: str, b: str) -> float:
    """dhash（hex）のハミング距離を 0..1 で。"""
    x = int(a, 16) ^ int(b, 16)
    return bin(x).count("1") / (len(a) * 4)


def _l1_ratio(a: str, b: str) -> float:
    """shape_rel（hexのuint8列）の平均絶対差を 0..1 で。"""
    ba, bb = bytes.fromhex(a), bytes.fromhex(b)
    if not ba or len(ba) != len(bb):
        return 1.0
    return sum(abs(p - q) for p, q in zip(ba, bb)) / (len(ba) * 255.0)


def distance(fa: dict, fb: dict) -> float:
    """採用した指紋 `dhash+shape_rel/1` の距離（0=同一, 1=最大）。

    2信号の等重み平均。**どちらも寸法を捨てたポーズ・構図の記述子**で、
    「寄り引きの違いは別物の理由にならない」というユーザーの判定理由と機構が一致する。
    顔・色（identity）は使わない ── 同じキャラは常に同じ顔なので反復の判定に効かない。
    """
    if not fa or not fb:
        return 1.0
    try:
        return (_hamming_ratio(fa["dhash"], fb["dhash"]) + _l1_ratio(fa["shape_rel"], fb["shape_rel"])) / 2
    except (KeyError, ValueError):
        return 1.0


# --------------------------------------------------------------------- 自己申告


def overrides_file() -> Path:
    return character_manager.CHARACTERS_DIR / OVERRIDES_NAME


def load_overrides() -> dict:
    """ユーザーの自己申告（`shared/characters/character_overrides.json`）。

    ⚠️ キーは `char_id/slot_id`。slot_id はキャラ内でしか一意でない（§11-5b）。
    """
    f = overrides_file()
    if not f.exists():
        return {"thresholds": {}, "overrides": {}}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return {"thresholds": d.get("thresholds") or {}, "overrides": d.get("overrides") or {}}
    except (OSError, json.JSONDecodeError):
        return {"thresholds": {}, "overrides": {}}


def thresholds() -> dict:
    t = dict(DEFAULT_THRESHOLDS)
    t.update({k: v for k, v in load_overrides()["thresholds"].items() if k in DEFAULT_THRESHOLDS})
    return t


def _ref(char_id: str, slot_id: str) -> str:
    return "%s/%s" % (char_id, slot_id)


# --------------------------------------------------------------------- 選択


def _pose_conflicts(want: str | None, have: str | None) -> bool:
    """アクション（pose）が**両側に値がある時だけ**制約になる。

    ⚠️ 「要求された pose と一致すること」を課してはいけない。実測（2026-08-27）:
    要求側は現行モデルで 54/54 行が pose を答えるのに対し、**在庫側は194件中185件が
    None**（pose を取りこぼしていた頃に作られた資産のため）。一致を要求すると
    在庫がほぼ全滅し、再利用そのものが止まる。

    そこで「分からないものは制約にしない・分かっていて食い違う時だけ弾く」に倒す。
    今日は何も弾かないが、pose を持つ在庫が増えるにつれて自然に効き始める。
    """
    return bool(want and have and want != have)


def candidates(char_id: str, emotion: str | None, ov: dict | None = None,
               extra_uses: dict[str, int] | None = None,
               allow_unknown_emotion: bool = False,
               pose: str | None = None) -> list[dict]:
    """適格な在庫を返す（適格性＝キャラ・世代・承認・感情・banned・使用回数上限）。

    extra_uses: `char_id/slot_id` → 試算中に消費した回数。**上限判定に必ず含める**。
    含め忘れると、試算の中で同じ絵を無限に使い回せてしまう（実際に一度そのバグを出した）。

    allow_unknown_emotion: 行の emotion が未指定（実データで196行中22行）の時に
      全候補を適格とするか。**自動割当では False**（感情の制約が外れると悲しい行に
      笑顔を当てうる。指紋は「新鮮さ」しか見ないので止められない）。
      **手動ピッカーでは True**＝人が見て選ぶなら制約は要らない。
      自動は仕様不足で止まり、人は越えられる、という分担。
    """
    ov = ov if ov is not None else load_overrides()["overrides"]
    extra_uses = extra_uses or {}
    th = thresholds()
    if emotion is None and not allow_unknown_emotion:
        return []
    current = panel_library_manager.appearance_version(char_id)
    out = []
    for e in panel_library_manager.load_index(char_id).get("entries", []):
        # 適格の条件は「透過PNGと指紋を持つこと」であって kind ではない。
        # psassist が取り込んだ194枚は kind="cutout"（image を持たない）だが、
        # 生成時に背景を抜くようになってからの entry は kind="panel" のまま
        # cutout と fingerprint を併せ持つ（同じ絵なので entry を分けない）。
        # ⚠️ 条件の本籍は panel_library_manager.usable_as（find_current と同じ定義を使う＝
        #    二重に書いて片方だけ直す事故を防ぐ）。
        if not panel_library_manager.usable_as(e)["cutout"]:
            continue
        if e.get("appearance_version") != current:
            continue  # 世代違い＝外見が変わっている。混ぜると衣装が途中で変わる
        if e.get("review_status", "approved") != "approved":
            continue
        o = ov.get(_ref(char_id, e["slot_id"])) or {}
        if o.get("banned"):
            continue
        allowed = o.get("allowed_emotions")
        if allowed and emotion and emotion not in allowed:
            continue  # ラベルが実際の表情とずれている分の人手補正
        elif not allowed and emotion and e.get("emotion") != emotion:
            continue
        if _pose_conflicts(pose, e.get("pose")):
            continue  # 分かっていて食い違う時だけ弾く（詳細は _pose_conflicts）
        cap = o.get("max_uses", th["max_uses"])
        uses = e.get("times_used", 0) + extra_uses.get(_ref(char_id, e["slot_id"]), 0)
        if cap is not None and uses >= cap:
            continue  # 生涯上限。離れて出ても総回数が多いとワンパターンになる
        out.append(dict(e, times_used=uses) if uses != e.get("times_used", 0) else e)
    return out


def select(char_id: str, emotion: str | None, recent: list[dict],
           ov: dict | None = None) -> tuple[dict | None, str]:
    """1行分を選ぶ。(entry, 理由) を返す。**選べなければ (None, 理由)＝新規生成の合図**。

    recent: 直近に割り当てた entry のリスト（新しい順でも古い順でもよい。窓は呼び出し側で切る）。

    選び方は背景の行ごと自動割当と同じ原理（[[aroll-background-per-line-manga-convention]]）:
    **直近から最も遠い**ものを選び、同点なら**使用回数の少ない**ものを選ぶ。
    ただし最良候補でも直近との距離が閾値未満なら**選ばない**（在庫が無いと判断する）。
    """
    if emotion is None:
        return None, "感情が未指定の行（自動割当はしない。手動ピッカーからは選べる）"
    cands = candidates(char_id, emotion, ov)
    if not cands:
        return None, "適格な在庫が無い（感情=%s・世代/承認/上限/banned で全て除外）" % emotion
    return _select_from(cands, recent, thresholds())


def plan_episode(char_of_line: list[tuple[str | None, dict | None]]) -> list[dict]:
    """話数まるごとの割当を試算する（ドライラン。times_used は増やさない）。

    char_of_line: 台本の並び順に [(char_id, slot), ...]。2人写り等は (None, _) を渡す。
      slot は行が要求する演技（`aroll.json` の `panels[].slot`）。emotion が適格性の主軸で、
      pose は「在庫側にも値があって食い違う時だけ」制約になる（_pose_conflicts）。

    返り値の `entry` が None の行が**新規生成すべき行**。予算のつまみは
    「この行数のうち何枚を実際に生成するか」であって、モードの選択ではない（§7-4）。
    """
    th = thresholds()
    ov = load_overrides()["overrides"]
    used: dict[str, int] = {}
    assigned: list[dict] = []
    plan = []
    for char_id, slot in char_of_line:
        slot = slot or {}
        emotion, pose = slot.get("emotion"), slot.get("pose")
        if not char_id:
            plan.append({"entry": None, "reason": "キャラ未確定（2人写り等）"})
            assigned.append({})
            continue
        if emotion is None:
            plan.append({"entry": None, "reason": "感情が未指定（自動割当はしない）",
                         "char_id": char_id, "emotion": None})
            assigned.append({})
            continue
        recent = [a for a in assigned[-th["recent_window"]:] if a]
        # 試算中の消費を上限判定ごと反映する（含めないと同じ絵を無限に使えてしまう）
        entry, why = _select_from(
            candidates(char_id, emotion, ov, used, pose=pose), recent, th)
        if entry:
            used[_ref(char_id, entry["slot_id"])] = used.get(_ref(char_id, entry["slot_id"]), 0) + 1
        plan.append({"entry": entry, "reason": why, "char_id": char_id,
                     "emotion": emotion, "pose": pose})
        assigned.append(entry or {})
    return plan


def _select_from(cands: list[dict], recent: list[dict], th: dict) -> tuple[dict | None, str]:
    """閾値を**固い制約**として使い、通ったものの中から**最も使われていない**1枚を選ぶ。

    ⚠️ 距離を最大化してはいけない。閾値を超えていれば「気にならない」のであって、
    距離0.9が距離0.15より良いわけではない（判定データの中央値は
    2=気にならない 0.241 / 3=明らかに別物 0.318 で、0.15 も既に不満の外）。
    距離を目的関数にすると同じ絵に偏る ── 実際それで57枚が未使用のまま19枚が
    上限に達した。背景の行ごと自動割当と同じ「使用回数最小優先＋直近回避」に揃える
    （[[aroll-background-per-line-manga-convention]]）。
    """
    if not cands:
        return None, "適格な在庫が無い"
    scored = []
    for e in cands:
        near = min((distance(e.get("fingerprint"), r.get("fingerprint")) for r in recent), default=1.0)
        scored.append((near, e))
    ok = [(n, e) for n, e in scored if n >= th["repetitive_below"]]
    if not ok:
        return None, "最良候補も直近と近すぎる（距離 %.3f）" % max(n for n, _ in scored)
    # 使用回数が少ない順 → 同数なら直近から遠い順 → slot_id で決定的に
    near, best = min(ok, key=lambda x: (x[1].get("times_used", 0), -x[0], x[1].get("slot_id", "")))
    return best, "距離 %.3f・使用 %d回" % (near, best.get("times_used", 0))


def nearest_in_stock(char_id: str, fingerprint: dict, *, limit: int = 5,
                     exclude_slot_id: str = "") -> list[dict]:
    """在庫の中で**指紋が近い順**に並べて返す（近い＝見た目が似ている）。

    ⚠️ **2026-08-29まで、これは受け入れ検査 `accept_new` だった**（近すぎる新規画像を
    アーカイブから弾く）。撤去した理由は `panel_library_manager._generate_and_measure`
    の docstring にある ── 指紋は意図的に寸法を捨てるので、バストアップと顔アップが
    「似ている」と判定され、実測で本番在庫の15%（うち25枚は使用中）を落としていた。

    **弾くのをやめて、人に見せる道具に変えた。** 「あまりに似ている絵」は人が並べて見て
    削除すればよい。閾値 `repetitive_below` は**選択**（`candidates` の使い回し判定）では
    今も現役だが、ここでは**並べ替えにしか使わない**（`too_close` は目安の色付け用）。

    ⚠️ 比較相手は `candidates()` と同じく**現世代（appearance_version 一致）だけ**。
    世代違いはもう配られない在庫なので、似ていても意味がない。
    """
    th = thresholds()
    current = panel_library_manager.appearance_version(char_id)
    out = []
    for e in panel_library_manager.load_index(char_id).get("entries", []):
        if not (e.get("fingerprint") or {}).get("dhash"):
            continue  # 指紋の無い entry（背景込みのパネルだけ）は比較対象にならない
        if e.get("appearance_version") != current:
            continue
        if exclude_slot_id and e.get("slot_id") == exclude_slot_id:
            continue
        d = distance(fingerprint, e.get("fingerprint"))
        out.append({
            "slot_id": e.get("slot_id"), "distance": round(d, 4),
            "emotion": e.get("emotion"), "shot": e.get("shot"), "angle": e.get("angle"),
            "times_used": e.get("times_used", 0),
            "review_status": e.get("review_status", "approved"),
            # 目安の色付け用。**弾くための判定ではない**（選択側の閾値を流用しているだけ）
            "too_close": d < th["repetitive_below"],
        })
    out.sort(key=lambda x: x["distance"])
    return out[:limit]
