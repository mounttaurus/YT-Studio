"""在庫の健全性監査（`Docs/STOCK_LABEL_ACCURACY_PLAN.md` §4-7・L6）。**読み取りのみ・無料**。

目的は「話数を回してから気付く」のではなく「**回す前に分かる**」こと。感情（系統）ごとに
「自動選択で使える枚数」と「1話の要求行数」を並べ、足りない感情（**薄いプール**）を警告する。

- **使える枚数** ＝ 主タグの系統がその感情の絵（同じ系統の受け皿を含む＝選択の段 0・2）で、
  `cutout_selector.candidates` と同じ適格条件（切り抜き＋指紋・現世代・承認済み・banned でない・
  `require_verified_label` が on なら確認済み）を満たし、生涯の使用上限（`max_uses`）に達していないもの。
  副タグでしか当たらない絵（段 1・3）は `sub_usable` に別に数え、薄いの判定には**入れない**
  （副タグは「主で選べない時の許容」であって、その感情の在庫ではない）。
- **要求** ＝ 話数を指定すればその話の `aroll.json` の行数（1人写り・孤立でない・感情のある行＝自動選択の対象と同じ）。
  指定しなければ見込み＝過去の全話の `aroll.json` の感情の割合 × 1話の行数の中央値
  （そのキャラの話数が `MIN_EPISODES` 未満なら全キャラ・全話で見込む）。
- **薄い** ＝ 使える枚数 < 要求行数（1話の中では同じ絵を二度使わないので、重複なしに1話を賄えない）。

数える場所はここ1箇所（API・director の audit・MCP・`scripts/stock_gap.py` が全部これを呼ぶ）。
適格の判定は `panel_library_manager.usable_as`・`cutout_selector.label_verified`／`effective_max_uses`／
`entry_tag` を使う＝選択と二重に書かない。
"""
from __future__ import annotations

import collections
import json
import math
import statistics
from typing import Iterable

from app.core import character_manager, cutout_selector, emotion_rubric, panel_library_manager, project_manager

MIN_EPISODES = 3   # これ未満しか話数の無いキャラは、全キャラ・全話の中央値で見込む


# --------------------------------------------------------------------- 要求（純粋関数）

def panel_demand(panels: Iterable[dict]) -> dict[str, collections.Counter]:
    """1話のコマ → {char_id: Counter(系統→行数)}。自動選択の対象になる行だけを数える
    （孤立でない・1人写り・感情あり。2人写りと感情未指定の行は在庫から自動では選ばれない）。

    ⚠️ 行数であってカット数ではない（3秒束ねは既定で無効＝ほぼ同じ。束ねが効く話では少し多めに出る）。
    """
    out: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for p in panels:
        chars = p.get("characters") or []
        emotion = (p.get("slot") or {}).get("emotion")
        if p.get("orphan") or len(chars) != 1 or not emotion:
            continue
        out[chars[0]][emotion] += 1
    return dict(out)


def own_uses(panels: Iterable[dict]) -> collections.Counter:
    """この話自身が `times_used` に積んだ分（`(char_id, slot_id)` → 回数）。

    話数を指定した監査では、自分の話で使った分を上限の判定から引く（でないと、割当済みの話を監査すると
    自分の消費で上限に達した絵が「使えない」と数えられ、薄さが過大に出る）。`selection_ab.py` と同じ数え方。
    """
    own: collections.Counter = collections.Counter()
    for p in panels:
        if p.get("cutout_slot_id") and p.get("cutout_char_id"):
            own[(p["cutout_char_id"], p["cutout_slot_id"])] += 1
    return own


def expected_demand(char_id: str, episodes: list[dict[str, collections.Counter]]) -> dict:
    """過去の話数から、そのキャラの1話あたりの要求行数を感情ごとに見込む。

    episodes: 話数ごとの `panel_demand` の結果。
    見込み＝感情の割合 × 1話の行数の中央値（切り上げ）。`peak` は過去の1話での最多（参考）。
    """
    mine = [ep[char_id] for ep in episodes if sum((ep.get(char_id) or {}).values())]
    if not mine:
        # 出演歴の無いキャラ（ナレーター・在庫を使わない役）に他人の要求を当てると、全感情が「薄い」と出て
        # 本物の警告が埋もれる。見込みは立てない（話数を指定した監査ならその話の行数で判定できる）。
        return {"basis": "none", "episodes": 0, "lines_per_episode": 0, "by_emotion": {}, "peak": {}}
    if len(mine) >= MIN_EPISODES:
        basis, basis_name = mine, "character"
    else:
        basis = [c for ep in episodes for c in ep.values() if sum(c.values())]
        basis_name = "all"
    if not basis:
        return {"basis": "none", "episodes": 0, "lines_per_episode": 0, "by_emotion": {}, "peak": {}}
    lines = statistics.median(sum(c.values()) for c in basis)
    total: collections.Counter = collections.Counter()
    for c in basis:
        total.update(c)
    n = sum(total.values())
    return {
        "basis": basis_name, "episodes": len(basis), "lines_per_episode": lines,
        "by_emotion": {f: math.ceil(lines * k / n) for f, k in total.items() if k},
        "peak": {f: max(c.get(f, 0) for c in basis) for f in total},
    }


# --------------------------------------------------------------------- 在庫の集計（純粋関数）

def summarize(char_id: str, entries: list[dict], *, current_version: str, overrides: dict,
              thresholds: dict, demand: dict[str, int], peak: dict[str, int] | None = None,
              own: collections.Counter | None = None) -> dict:
    """1キャラの在庫を感情（系統）ごとに数え、要求と比べる。

    overrides: `character_overrides.json` の `overrides`（キーは `char_id/slot_id`）。
    demand: 系統 → 1話の要求行数。peak: 系統 → 過去の1話の最多（参考・判定には使わない）。
    own: その話自身の消費（話数を指定した監査のみ）。
    """
    own = own or collections.Counter()
    peak = peak or {}
    vocab = emotion_rubric.vocab()
    totals = collections.Counter(entries=len(entries))
    rows: dict[str, dict] = {}

    def row(fam: str) -> dict:
        return rows.setdefault(fam, {"emotion": fam, "label_ja": emotion_rubric.FAMILY_LABEL_JA.get(fam, fam),
                                     "primary": 0, "verified": 0, "usable": 0, "capped": 0, "with_pose": 0,
                                     "sub_usable": 0, "by_tag": collections.Counter()})

    slot_ids = set()
    for e in entries:
        slot_ids.add(e.get("slot_id"))
        if not panel_library_manager.usable_as(e)["cutout"]:
            totals["no_cutout"] += 1
            continue
        if e.get("appearance_version") != current_version:
            totals["stale"] += 1
            continue
        if e.get("review_status", "approved") != "approved":
            totals["pending"] += 1
            continue
        o = overrides.get("%s/%s" % (char_id, e.get("slot_id"))) or {}
        if o.get("banned"):
            totals["banned"] += 1
            continue
        if thresholds.get("exclude_full_body") and cutout_selector.body_scope(e, thresholds) == "full":
            totals["full_body"] += 1        # 全身級＝自動選定に出ない（明示した行・手動ピッカーのみ）
            continue
        if not thresholds.get("exclude_full_body") and cutout_selector.body_scope(e, thresholds) == "full":
            totals["full_body_warn"] += 1   # 警告だけ（選定には出る）。除外するのは exclude_full_body=true の時
        tag = cutout_selector.entry_tag(e)
        fam = e.get("emotion")
        if tag == emotion_rubric.FACE_HIDDEN:
            totals["face_hidden"] += 1      # 系統なし＝自動選択に出ない（§14 D3・手動ピッカーのみ）
            continue
        if fam not in vocab:
            totals["no_emotion"] += 1       # 感情が空・語彙外＝完全一致でしか選ばれないので死蔵
            continue
        totals["eligible"] += 1
        verified = cutout_selector.label_verified(e)
        if not verified:
            totals["unverified"] += 1
        cap = cutout_selector.effective_max_uses(o, thresholds)
        uses = max(0, e.get("times_used", 0) - own.get((char_id, e.get("slot_id")), 0))
        capped = cap is not None and uses >= cap
        selectable = not capped and (verified or not thresholds.get("require_verified_label"))

        r = row(fam)
        r["primary"] += 1
        r["by_tag"][tag] += 1
        r["verified"] += verified
        r["capped"] += capped
        r["usable"] += selectable
        r["with_pose"] += bool(e.get("pose"))
        for sf in {emotion_rubric.family_of(s) for s in e.get("emotion_sub_tags") or ()} - {fam, None}:
            row(sf)["sub_usable"] += selectable

    for fam in demand:
        row(fam)
    families = []
    for r in rows.values():
        need = int(demand.get(r["emotion"], 0))
        r["by_tag"] = dict(r["by_tag"].most_common())
        r["pose_rate"] = round(r["with_pose"] / r["primary"], 2) if r["primary"] else None
        r["demand"] = need
        r["peak"] = peak.get(r["emotion"])
        r["thin"] = r["usable"] < need
        # 参考: 過去の1話の最多で見ると足りない（見込みの平均では隠れる偏った回への備え。判定には使わない）
        r["thin_at_peak"] = r["peak"] is not None and r["usable"] < r["peak"]
        r["short"] = max(0, need - r["usable"])
        families.append(r)
    families.sort(key=lambda r: (-r["demand"], -r["primary"], r["emotion"]))

    orphans = sorted(k for k in overrides
                     if k.startswith(char_id + "/") and k.split("/", 1)[1] not in slot_ids)
    return {
        "char_id": char_id,
        "totals": {k: totals.get(k, 0) for k in ("entries", "eligible", "unverified", "no_emotion", "face_hidden", "full_body",
                                                   "full_body_warn", "pending", "stale", "banned", "no_cutout")},
        "families": families,
        "thin": [r["emotion"] for r in families if r["thin"]],
        "thin_at_peak": [r["emotion"] for r in families if r["thin_at_peak"]],
        "orphan_overrides": orphans,
    }


def thin_message(name: str, r: dict) -> str:
    """「アオイ thoughtful（物思い） 13行／在庫10枚」の形（audit・cutout_plan の警告で同じ文面を使う）。"""
    sub = f"・副タグで受けられる{r['sub_usable']}枚" if r.get("sub_usable") else ""
    return f"{name} {r['emotion']}（{r['label_ja']}） {r['demand']}行／在庫{r['usable']}枚{sub}"


# --------------------------------------------------------------------- 実データを読む口

def _name(char_id: str) -> str:
    c = character_manager.read_character(char_id) or {}
    return c.get("name") or char_id


def _all_episode_demands() -> list[dict[str, collections.Counter]]:
    out = []
    for f in sorted(project_manager.PROJECTS_DIR.glob("*/episodes/*/a_roll/aroll.json")):
        try:
            panels = json.loads(f.read_text(encoding="utf-8")).get("panels", [])
        except (OSError, ValueError):
            continue
        d = panel_demand(panels)
        if d:
            out.append(d)
    return out


def _summarize_char(char_id: str, demand: dict[str, int], peak: dict | None = None,
                    own: collections.Counter | None = None) -> dict:
    res = summarize(
        char_id, panel_library_manager.load_index(char_id).get("entries", []),
        current_version=panel_library_manager.appearance_version(char_id),
        overrides=cutout_selector.load_overrides()["overrides"], thresholds=cutout_selector.thresholds(),
        demand=demand, peak=peak, own=own)
    res["name"] = _name(char_id)
    res["thin_messages"] = [thin_message(res["name"], r) for r in res["families"] if r["thin"]]
    return res


def character_health(char_id: str, episodes: list | None = None) -> dict:
    """1キャラの監査。要求は過去の全話からの見込み（`expected_demand`）。"""
    exp = expected_demand(char_id, episodes if episodes is not None else _all_episode_demands())
    res = _summarize_char(char_id, exp["by_emotion"], exp["peak"])
    res["demand_basis"] = {k: exp[k] for k in ("basis", "episodes", "lines_per_episode")}
    res["thresholds"] = {k: cutout_selector.thresholds()[k] for k in ("max_uses", "require_verified_label")}
    return res


def all_health() -> dict:
    """在庫を持つ全キャラの監査（見込みの要求で）。"""
    eps = _all_episode_demands()
    chars = sorted(p.parent.parent.name for p in
                   character_manager.CHARACTERS_DIR.glob("*/panel_library/library.json"))
    items = [character_health(c, eps) for c in chars]
    return {"characters": items, "thin_messages": [m for it in items for m in it["thin_messages"]]}


def full_body_lines(panels: list[dict]) -> list[dict]:
    """この話で、頼んでいないのに**全身級の絵**が当たっている行（警告用・2026-10-07）。

    部屋の背景は全身を受ける画角が無く、bust 用の背景に全身を置くと小さく立つだけの絵になる。選定は黙って外さない
    （`exclude_full_body` は既定 false）ので、人が見て直せるよう行を挙げる。台本かユーザーが全身（wide/full_body）を
    明示した行は意図どおりなので挙げない（`aroll_manager` の allow_full_body と同じ条件）。
    """
    th = cutout_selector.thresholds()
    entries: dict[str, dict] = {}
    out = []
    for p in panels:
        cid, sid = p.get("cutout_char_id"), p.get("cutout_slot_id")
        if p.get("orphan") or not cid or not sid:
            continue
        hs = p.get("slot") or {}
        if p.get("slot_source") in ("user", "script") and hs.get("shot") in ("wide", "full_body"):
            continue
        if cid not in entries:
            entries[cid] = {e.get("slot_id"): e for e in panel_library_manager.load_index(cid).get("entries", [])}
        e = entries[cid].get(sid)
        if e is not None and cutout_selector.body_scope(e, th) == "full":
            out.append({"line_id": p.get("line_id"), "char_id": cid, "slot_id": sid})
    return out


def episode_health(panels: list[dict]) -> dict:
    """1話の監査。要求はその話の `aroll.json` の行数（話数を回す前の事前警告に使う）。"""
    own = own_uses(panels)
    items = [_summarize_char(cid, dict(c), own=own) for cid, c in sorted(panel_demand(panels).items())]
    for it in items:
        it["demand_basis"] = {"basis": "episode"}
    thin = [{"char_id": it["char_id"], "name": it["name"], "emotion": r["emotion"], "label_ja": r["label_ja"],
             "demand": r["demand"], "usable": r["usable"], "sub_usable": r["sub_usable"],
             "message": thin_message(it["name"], r)}
            for it in items for r in it["families"] if r["thin"]]
    return {"characters": items, "thin": thin, "full_body_lines": full_body_lines(panels)}
