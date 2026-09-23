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

from app.core import character_manager, panel_library_manager, shot_meter

OVERRIDES_NAME = "character_overrides.json"

# 較正できていない環境でも動くための既定値（正は character_overrides.json の thresholds）
DEFAULT_THRESHOLDS = {
    "repetitive_below": 0.073,  # これ未満＝使い回しに見える（完成コマで較正・§10-6）
    "max_uses": 3,              # 生涯の使用回数上限（times_used累計・1話内ではない）
    "recent_window": 5,         # 直近何行を「近く」とみなすか（§10-7でK=4が違反0の窓）
    # ⚠️ **合成距離ではなくポーズだけを見る閾値**（2026-09-06追加・下の pose_distance 参照）。
    # ユーザーが「似すぎ」と指摘した実データの shape_rel 実測が 0.070 / 0.165 / 0.187 だったので、
    # 「0.20未満は人の目に同じポーズ」と読む。本番64行での実測は下の plan_episode に記録。
    "pose_near": 0.20,
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


def pose_distance(fa: dict, fb: dict) -> float:
    """**ポーズだけ**の距離（`shape_rel` 単体）。0=同じ姿勢, 1=最大。

    ⚠️ `distance()` と使い分ける。合成距離は `dhash`（フレーム全体の明暗＝**寄り引きに敏感**）を
    半分含むため、「同じポーズを寄りと引きで撮った2枚」を**別物と判定してしまう**。
    実データ（2026-09-06・ユーザーが「似すぎ」と指摘した3枚）:

        031×040  合成 0.199 ＝ dhash 0.328 ＋ shape_rel **0.070**
        039×040  合成 0.309 ＝ dhash 0.453 ＋ shape_rel **0.165**
        031×039  合成 0.390 ＝ dhash 0.594 ＋ shape_rel **0.187**

    合成では 0.318（＝「明らかに別物」の較正中央値）を超える組すらあるのに、人の目には
    同じポーズだった。`shape_rel` は bbox に正規化済み＝位置と寸法を捨てて形だけを見るので、
    こちらが「見た目が同じか」の正しい物差しになる（[[fingerprint-identity-vs-repetition]]
    の「識別に効く信号≠反復に効く信号」と同じ構図）。
    """
    if not fa or not fb:
        return 1.0
    try:
        return _l1_ratio(fa["shape_rel"], fb["shape_rel"])
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


# --------------------------------------------------------------------- 向き

# ⚠️ **指紋は向きを見られない**（2026-09-20 目視で発見・[[shape-rel-cannot-see-facing-direction]]）。
# `shape_rel` は bbox 正規化した 16×16 のシルエットで、正面と真横は「頭＋肩の塊」として
# 似た形になる。実測: 完全な横顔と正面の顔アップの pose_distance が **0.039〜0.155**＝
# 閾値0.20を下回り「同じ絵」と判定されていた（目視では明確に別カット）。
# 左右反転の非対称度で代用できないかも試したが失敗（真横0.130／正面0.131）。
#
# 向きの情報は**ラベルにしか無い**ので、ラベルから素直に読む。新規生成はラベルが正確
# （profile_left が実際に横顔になることを目視確認済み）。ラベルの無い旧在庫は "front" 扱い
# ＝従来どおりの判定になるので、後方互換で壊れない。
#
# ⚠️ **真横(profile_*)と斜め45度(facing_*)は別クラスにする**（2026-09-20 目視で判断）。
# 真横は遠目の目が見えず輪郭だけ、斜めは両目が見えて体だけ振れている ── 別カットとして読める。
# 同じ "left" に畳むと互いを「近すぎ」と潰し合い、せっかくの変化が使えなくなる。
_ORIENTATION_BY_POSE = {
    "profile_left": "left_profile", "facing_left": "left_3q",
    "profile_right": "right_profile", "facing_right": "right_3q",
}


def orientation(entry: dict | None) -> str:
    """その絵が**どちらを向いているか**。

    "front" / "left_3q" / "left_profile" / "right_3q" / "right_profile" / "back"。

    保存済みの `facing` があればそれを使う（`psassist/Docs/CHARACTER_CUTOUT_PLAN.md` §4 で
    予約されていたフィールド。`facing_source` に出どころが入る）。無ければラベルから導く
    ── `angle="from_behind"` は背面なので pose より優先する。

    ⚠️ **`facing` を正本にする**のは、後から視覚モデルや手タグで**上書きできる**ようにするため。
    ラベル由来の値は `facing_source="llm"` で入っているので、精度が足りなければそこだけ直せる。
    """
    e = entry or {}
    stored = e.get("facing")
    if stored:
        return stored
    if (e.get("angle") or "") == "from_behind":
        return "back"
    return _ORIENTATION_BY_POSE.get(e.get("pose") or "", "front")


def _looks_same(a: dict, b: dict, fn, threshold: float) -> bool:
    """a と b が「同じ絵に見える」か。**向きが違えば無条件で別物**として扱う。

    ⚠️ ここを距離だけで判定すると、真横の在庫が「直前の正面と近い」として弾かれ、
    ユーザーが求めた向きの変化を**選定アルゴ自身が潰す**（上記の実測）。
    """
    if orientation(a) != orientation(b):
        return False
    return fn(a.get("fingerprint"), b.get("fingerprint")) < threshold


# --------------------------------------------------------------------- 選択


def _pose_conflicts(want: str | None, have: str | None) -> bool:
    """アクション（pose）が**両側に値がある時だけ**制約になる。

    ⚠️ 「要求された pose と一致すること」を課してはいけない。実測（2026-08-27）:
    要求側は現行モデルで 54/54 行が pose を答えるのに対し、**在庫側は194件中185件が
    None**（pose を取りこぼしていた頃に作られた資産のため）。一致を要求すると
    在庫がほぼ全滅し、再利用そのものが止まる。

    そこで「分からないものは制約にしない・分かっていて食い違う時だけ弾く」に倒す。
    今日は何も弾かないが、pose を持つ在庫が増えるにつれて自然に効き始める。

    ⚠️ **向きのポーズ（profile_*/facing_*）はここで比べない**（2026-09-20）。あれは
    アクションではなく**体の向き**で、担当軸は `facing`／カメラプランの方。
    ここで弾いていたせいで、向きを増やすために買った在庫が**候補に入る前に消えて**いた
    （実測: カメラプランが back や left_profile を希望しても 62カット中60カットが正面）。
    在庫の pose 被覆が上がるほど悪化する性質だったので、軸の取り違えとして直す。
    """
    if want in _ORIENTATION_BY_POSE or have in _ORIENTATION_BY_POSE:
        return False
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


def plan_episode(char_of_line: list[tuple[str | None, dict | None]],
                 want_shots: list[str | None] | None = None) -> list[dict]:
    """話数まるごとの割当を試算する（ドライラン。times_used は増やさない）。

    char_of_line: 台本の並び順に [(char_id, slot), ...]。2人写り等は (None, _) を渡す。
      slot は行が要求する演技（`aroll.json` の `panels[].slot`）。emotion が適格性の主軸で、
      pose は「在庫側にも値があって食い違う時だけ」制約になる（_pose_conflicts）。

    返り値の `entry` が None の行が**新規生成すべき行**。予算のつまみは
    「この行数のうち何枚を実際に生成するか」であって、モードの選択ではない（§7-4）。

    **本番64行での実測（2026-09-06・`20260905_001_missing_scientists` ep01）**
    指標は「同キャラの隣接行で `pose_distance` < 0.20」＝人の目に同じポーズに見える組:

        旧アルゴ（重複対策なし）      ルカ 3/3組  アオイ  7/19組  在庫充当 64/64
        画角ラベル回避だけ入れた版    ルカ 1/3組  アオイ 15/18組  在庫充当 62/64  ← 悪化
        pose_distance の段構え（現行）ルカ 0/3組  アオイ  2/18組  在庫充当 62/64

    在庫充当は変わらない＝**追加課金なしで見た目の反復だけが減る**。段の使用実績は
    1段目47行・2段目15行・3段目以降0行で、再使用への退避は一度も要らなかった。
    """
    th = thresholds()
    ov = load_overrides()["overrides"]
    used: dict[str, int] = {}
    assigned: list[dict] = []
    plan = []
    for i, (char_id, slot) in enumerate(char_of_line):
        slot = slot or {}
        # カメラプラン（U2）が欲しがった段。ソフト制約として _select_from へ渡す
        want = (want_shots or [None] * len(char_of_line))[i]
        # 段だけの文字列でも、{"shot","facing"} でも受ける（呼び出し側の移行を楽にする）
        want_shot = want.get("shot") if isinstance(want, dict) else want
        want_facing = want.get("facing") if isinstance(want, dict) else None
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
        prev = assigned[-1] if assigned and assigned[-1] else None
        # 試算中の消費を上限判定ごと反映する（含めないと同じ絵を無限に使えてしまう）
        cands = candidates(char_id, emotion, ov, used, pose=pose)
        # ⚠️ **1話の中では同じ絵を二度使わない**（2026-09-06・ユーザー判断）。
        # max_uses は「生涯の」上限（既定3）なので、17行離れた再使用を素通りさせていた
        # （本番 line_009 と line_026 に同一 slot_id。recent_window=5 の窓の外だった）。
        # 在庫は1キャラ140枚規模あるので、1話ぶんを賄うのに使い回す必要が無い。
        fresh = [e for e in cands if not used.get(_ref(char_id, e["slot_id"]))]
        # ユーザー提案の2段構え（2026-09-06）: **似ている絵は「同じ絵」として扱い、
        # 候補が尽きた時だけ緩める。** 事前クラスタリングは採らなかった ── 単連結だと
        # 芋づる式に併合され、3枚を同群にできる閾値ではルカ142枚中119枚が1グループへ
        # 崩壊して「別グループから選ぶ」が機能しなくなる（実測）。代わりに
        # **選ぶ瞬間に直近と pose_distance で比べる**ことで同じ意図を崩壊なしに実現する。
        tiers = [(fresh, False, ""),
                 (fresh, True, "・⚠️直近とポーズが近い（他に候補が無い）"),
                 (cands, False, "・⚠️この話で再使用（未使用の在庫では選べなかった）"),
                 (cands, True, "・⚠️再使用かつポーズも近い（在庫が尽きた）")]
        entry, why, note = None, "", ""
        for pool, allow, tier_note in tiers:
            entry, why = _select_from(pool, recent, th, prev, allow_pose_near=allow,
                                      want_shot=want_shot, want_facing=want_facing)
            if entry:
                note = tier_note
                break
        if entry:
            used[_ref(char_id, entry["slot_id"])] = used.get(_ref(char_id, entry["slot_id"]), 0) + 1
            why += note
        plan.append({"entry": entry, "reason": why, "char_id": char_id,
                     "emotion": emotion, "pose": pose})
        assigned.append(entry or {})
    return plan


def _tags(e: dict | None) -> tuple[str, str]:
    """並びの単調さを見るためのタグ（画角）。感情は行が決めるので含めない。"""
    e = e or {}
    return (e.get("shot") or "", e.get("angle") or "")


def _select_from(cands: list[dict], recent: list[dict], th: dict,
                 prev: dict | None = None, *,
                 allow_pose_near: bool = True,
                 want_shot: str | None = None,
                 want_facing: str | None = None) -> tuple[dict | None, str]:
    """閾値を**固い制約**として使い、通ったものの中から**最も使われていない**1枚を選ぶ。

    ⚠️ 距離を最大化してはいけない。閾値を超えていれば「気にならない」のであって、
    距離0.9が距離0.15より良いわけではない（判定データの中央値は
    2=気にならない 0.241 / 3=明らかに別物 0.318 で、0.15 も既に不満の外）。
    距離を目的関数にすると同じ絵に偏る ── 実際それで57枚が未使用のまま19枚が
    上限に達した。背景の行ごと自動割当と同じ「使用回数最小優先＋直近回避」に揃える
    （[[aroll-background-per-line-manga-convention]]）。

    prev: 直前の行に割り当てた entry。**同じ画角(shot/angle)が隣り合うのを後回しにする**
      ためだけに使う（2026-09-06 追加）。⚠️ **ソフト制約**にすること。ハードにすると
      在庫が尽きた行が新規生成へ落ちて課金が増える。

    allow_pose_near: False なら「直近と**ポーズが**近すぎる絵」を候補から外す
      （ユーザー提案の1段目）。True で同じ候補集合を制限なしに見る（2段目）。

    ⚠️ **画角ラベルだけを避けても見た目は変わらない。** 2026-09-06、`_tags` のソフト制約
    だけを入れた版を本番64行で測ったところ、アオイの「同キャラ隣接で shape_rel<0.20」が
    **7/19組 → 15/18組へ悪化**した ── ラベルさえ違えば良いので、**同じポーズで別ラベルの絵**を
    積極的に選んでしまう。`pose_distance` による段構え（下記）と**必ず併用**すること。
    """
    if not cands:
        return None, "適格な在庫が無い"
    pose_near = th.get("pose_near", DEFAULT_THRESHOLDS["pose_near"])
    # ⚠️ **向きが違う相手とは比べない**（_looks_same 参照）。距離だけで見ると
    # 真横の在庫が「直前の正面と近い」として落ち、向きの変化が起きなくなる。
    scored = []
    for e in cands:
        near = min((distance(e.get("fingerprint"), r.get("fingerprint"))
                    for r in recent if orientation(e) == orientation(r)), default=1.0)
        scored.append((near, e))
    ok = [(n, e) for n, e in scored if n >= th["repetitive_below"]]
    if not ok:
        return None, "最良候補も直近と近すぎる（距離 %.3f）" % max(n for n, _ in scored)
    if not allow_pose_near:
        ok = [(n, e) for n, e in ok
              if not any(_looks_same(e, r, pose_distance, pose_near) for r in recent)]
        if not ok:
            return None, "直近とポーズが近すぎる候補しか無い（閾値 %.2f）" % pose_near
    prev_tags = _tags(prev) if prev else None
    prev_pose = (prev or {}).get("pose")

    prev_orientation = orientation(prev) if prev else None

    def monotony(e: dict) -> int:
        """直前とどれだけ「同じに見えるか」の点数（0-2・小さいほど良い）。

        `pose_distance` はシルエット（16x16のbbox正規化マスク）しか見ないので、
        **輪郭の内側**の違い（顎に手／腕組み／手を振る）を検出できない。
        ラベルがある絵に限ってはそこを補える ── ただし付与率はルカ22%・アオイ13%（実測）
        なので、**これは補助信号であって主軸にはならない**。

        ⚠️ **向きが違えば単調ではない**（2026-09-20）。正面の次に真横が来るのは
        むしろ狙いどおりの変化なので、画角ラベルが同じでも減点しない。
        """
        if prev_orientation and orientation(e) != prev_orientation:
            return 0
        score = 1 if prev_tags and _tags(e) == prev_tags else 0
        pose = e.get("pose")
        if prev_pose and pose and pose == prev_pose:
            score += 1
        return score

    def off_plan(e: dict) -> int:
        """カメラプランの希望（段・向き）から外れている点数（0-3）。U2・**ソフト制約**。

        ⚠️ ハードにしない。その段の在庫が無い行が新規生成へ落ちて課金が増える。
        段は**実測**で見る（ラベルは実物と31〜47%しか一致しない）。

        ⚠️ **向きを段より重く数える**（重み2対1）。段だけを見ていた版も、段と向きを
        同点にした版も、**62カット中60カットが正面**になった ── 「段も向きも完全一致」の
        在庫が稀なので両方が同点になり、結局 times_used で決まっていた。
        向きの方が視覚的な差が大きく、かつ在庫が希少（383枚中62枚）なので、
        意図的に優先しないと永久に眠る（2026-09-20 実測）。
        """
        off = 0
        if want_shot and shot_meter.effective_shot(e) != want_shot:
            off += 1
        if want_facing and orientation(e) != want_facing:
            off += 2
        return off

    # カメラプランの段 → 直前と被らない → 使用回数が少ない → PS済みを優先（同点の時だけ・
    # Docs/CUTOUT_PS_PRIMARY_PLAN.md P2） → 直近から遠い → slot_id
    near, best = min(ok, key=lambda x: (
        off_plan(x[1]), monotony(x[1]), x[1].get("times_used", 0),
        0 if x[1].get("cutout_method") == "ps_select_subject" else 1,
        -x[0], x[1].get("slot_id", "")))
    why = "距離 %.3f・使用 %d回" % (near, best.get("times_used", 0))
    if prev_tags and _tags(best) == prev_tags:
        why += "・⚠️直前と同じ画角（他に候補が無い）"
    if prev_pose and best.get("pose") == prev_pose:
        why += "・⚠️直前と同じポーズラベル"
    return best, why


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
