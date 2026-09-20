"""カット列に**寄り引き**を当てる（U2・`Docs/AROLL_UNIFIED_FLOW_PLAN.md` §11-3）。

## 役割分担

    emotion      = 台本（LLM）＝**意味**。悲しい行に笑顔を当てない
    shot / 向き  = ここ（機械）＝**リズム**。同じ話者が続く時に変化を作る

LLMを挟まないのは、文脈判断と機械の判断が衝突して**同じ画角が続く**恐れがあるため
（2026-09-14 ユーザー判断）。「決め」はランの最終カットに置く ── ラン内で感情が変わるのは
18組中1組、「！」は0件で、**LLMが付けた情報からは決めを読めない**ことを実測済み。

## 在庫を見て組む

⚠️ **理想の段を並べても意味がない。** 台本が要求した (emotion, shot) の31%は在庫に無く、
選定層が「近いもの」で代用していた（それがワンパターンの一因）。ここでは
**そのキャラ・その感情に実在する段**だけを候補にするので、希望と実物のズレが構造的に起きない。

⚠️ **段は実測で並べる**（`shot_meter.effective_shot`）。`shot` ラベルは実物と
31〜47%しか一致しない（2026-09-20 実測）。

⚠️ **ここが決めるのは「希望」であってハード制約ではない。** 返した段に在庫が無ければ
`cutout_selector` が従来どおり指紋で選ぶ。ハードにすると在庫が尽きた行が新規生成へ落ちて
課金が増える（[[pose-distance-not-composite-distance]] の教訓）。
"""
from __future__ import annotations

from app.core import cutout_selector, shot_meter

# 寄り(0) → 引き(4)。shot_meter.SCALE_ORDER と同じ並び。
SCALE = shot_meter.SCALE_ORDER


def available(entries: list[dict]) -> dict[str, list[dict]]:
    """在庫を「実測の段」で束ねる。段が分からないものは捨てずに ``unknown`` へ。"""
    out: dict[str, list[dict]] = {}
    for e in entries:
        shot = shot_meter.effective_shot(e)
        out.setdefault(shot if shot in SCALE else "unknown", []).append(e)
    return out


def _nearest(pool: dict[str, list[dict]], want: str) -> str | None:
    """欲しい段に在庫が無ければ**一番近い段**へ寄せる（無ければ None）。"""
    if want in pool:
        return want
    if want not in SCALE:
        return None
    i = SCALE.index(want)
    for d in range(1, len(SCALE)):
        for j in (i - d, i + d):
            if 0 <= j < len(SCALE) and SCALE[j] in pool:
                return SCALE[j]
    return None


def _scales(n_cuts: int, have: list[str], prev_shot: str | None) -> list[str]:
    """段（寄り引き）の並び。型は §11-3。"""
    tight, wide = have[0], have[-1]
    mid = have[len(have) // 2]
    if n_cuts <= 1:
        # 直前と同じ段を避ける（solo が続く＝話者が交互に喋る場面）
        if prev_shot and len(have) > 1:
            return [next(s for s in have if s != prev_shot)]
        return [mid]
    if n_cuts == 2:
        return [wide, tight]
    if n_cuts == 3:
        return [mid, wide, tight]
    out = [wide if i % 2 == 0 else mid for i in range(n_cuts - 1)]
    out.append(tight)   # 決めは一番寄り
    return out


def facings_in(entries: list[dict]) -> list[str]:
    """在庫に実在する向きを、**正面を先頭**にして並べる（正面が地の状態なので基準）。"""
    have = {cutout_selector.orientation(e) for e in entries}
    return (["front"] if "front" in have else []) + sorted(have - {"front"})


def plan_run(n_cuts: int, pool: dict[str, list[dict]], prev_shot: str | None = None,
             facings: list[str] | None = None,
             prev_facing: str | None = None) -> list[dict]:
    """同じ話者のラン（``n_cuts`` カット）に当てる **段と向き**の並びを返す。

    ⚠️ **段だけを計画してはいけない**（2026-09-20 実測）。段だけにしたところ、
    選定が段の一致を最優先した結果 **62カット中60カットが正面**になり、せっかく
    $1.84 で増やした向きの在庫が使われなかった。向きも**明示的に計画する**こと。

    向きは「直前と違うものへ順に回す」だけの素朴な回し方にする ── ラン内で
    正面→横→正面…と交互になり、在庫に無い向きは最初から候補に入らない。
    """
    have = [s for s in SCALE if s in pool]
    scales = _scales(n_cuts, have, prev_shot) if have else ["unknown"] * n_cuts

    ring = list(facings or ["front"])
    # 直前と同じ向きから始めない（ラン境界でも変化が出る）
    if prev_facing in ring and len(ring) > 1:
        i = (ring.index(prev_facing) + 1) % len(ring)
        ring = ring[i:] + ring[:i]
    out = []
    for i, s in enumerate(scales):
        out.append({"shot": s, "facing": ring[i % len(ring)]})
    return out


def plan_episode(cuts: list[dict], stock_by_char: dict[str, list[dict]],
                 char_of_cut: dict[str, str]) -> dict[str, dict]:
    """話数まるごとのカメラプラン。``{cut_id: {"shot", "reason"}}`` を返す。

    cuts: `cut_planner.plan_cuts` の出力（台本順）。
    stock_by_char: {char_id: そのキャラの適格在庫}。**感情での絞り込みは呼び出し側**
      （適格性の本籍は `cutout_selector.candidates`。ここで二重に定義しない）。
    char_of_cut: {cut_id: char_id}。キャラ未確定のカットは入れない。

    ⚠️ **ランの単位で組む。** カットを1つずつ見ると「前と違う段」しか言えず、
    寄り引きの*流れ*にならない。話者が変わるまでを1つのランとして扱う。
    """
    runs: list[list[dict]] = []
    for c in cuts:
        if runs and runs[-1][-1].get("speaker_id") == c.get("speaker_id") \
                and runs[-1][-1].get("section") == c.get("section"):
            runs[-1].append(c)
        else:
            runs.append([c])

    out: dict[str, dict] = {}
    prev_shot: str | None = None
    prev_facing: str | None = None
    for run in runs:
        char_id = char_of_cut.get(run[0]["cut_id"])
        entries = stock_by_char.get(char_id) or [] if char_id else []
        pool = available(entries)
        wants = plan_run(len(run), pool, prev_shot, facings_in(entries), prev_facing)
        for c, want in zip(run, wants):
            got = _nearest(pool, want["shot"]) if want["shot"] != "unknown" else None
            out[c["cut_id"]] = {
                "shot": got,
                "facing": want["facing"],
                "wanted": want["shot"],
                # 在庫に無くて寄せた時は理由を残す（§11-3「代用のしかたを直す」の観察用）
                "reason": ("%s（希望%s・在庫に無いので寄せた）" % (got, want["shot"])
                           if got and got != want["shot"] else (got or "在庫の段が不明")),
                "role": c.get("role"),
            }
            if got:
                prev_shot = got
            prev_facing = want["facing"]
    return out
