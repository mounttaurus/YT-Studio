"""Aロールの「同じ絵の繰り返し」を、選んだ後に検査する（`Docs/AROLL_DUPLICATE_CHECK_PLAN.md`）。

選ぶ仕組み（在庫からの割り当て・選択アルゴリズム）は変えない。ここは**選び終わった後**の
検査で、純粋関数（何も読まず・書かず・課金しない）。呼び出し側（`aroll_manager.duplicate_report`）が
実データを集めて渡す。

判定（絵の単位は**カット**＝同じ絵を共有する行の束。サブ行グループの共有は重複ではない）:
  exact … 同じ `(char_id, slot_id)` が別のカットに出る（話数の中なら距離は問わない）
  near  … 近い行（既定 ±30 行）の同じキャラの絵と、向きが同じで指紋距離が閾値未満
          （`cutout_candidates` の `too_close` と同じ物差し＝`repetitive_below`）

⚠️ **キーは必ず `(char_id, slot_id)`**。`slot_id` はキャラ内でしか一意でない
（別キャラの同名 slot を重複に数えない。D0 で実際にそれで数を膨らませていた）。

除外（直す対象にしない）:
  人が選んだ絵（`cutout_source=="user"`）／手直し済み（✋・呼び出し側が `protected_line_ids` で渡す）／
  実画像を持つ行（`status=="done"`＝差し替えるとパネル画像と食い違う）／キャラが1人に定まらない行
  （ナレーション・2ショット）／台本から外れた行（呼び出し側が渡さない）。
  守られた行どうしが同じ絵でも**直さない**（`conflicts` に数えて見せるだけ）。守られた行と守られていない
  行が重なる時は、**守られていない側**を直す対象にする（先に出た・後に出たは問わない）。
"""
from __future__ import annotations

DEFAULT_WINDOW = 30


def protection_reason(panels: list[dict], protected_line_ids: set[str]) -> str | None:
    """カットの行が一つでも守られていれば理由を返す（直す対象にしない）。"""
    if any(p.get("line_id") in protected_line_ids for p in panels):
        return "hand_edited"
    if any(p.get("cutout_source") == "user" for p in panels):
        return "user_picked"
    if any(p.get("status") == "done" for p in panels):
        return "has_image"
    return None


def collect_picture_cuts(cuts: list[dict], panels_by_id: dict[str, dict],
                         protected_line_ids: set[str]) -> list[dict]:
    """絵を持つカットだけを並べる（台本順）。各要素は検査の最小単位。

    pos … そのカットの先頭行の通し位置（カットをまたいだ「何行離れているか」に使う）。
    """
    out: list[dict] = []
    pos = 0
    for cut in cuts:
        line_ids = list(cut.get("line_ids") or [])
        members = [panels_by_id[l] for l in line_ids if l in panels_by_id]
        start = pos
        pos += len(line_ids)
        head = next((p for p in members if p.get("cutout_slot_id") and p.get("cutout_char_id")), None)
        if head is None:
            continue
        chars = [c for c in (head.get("characters") or []) if c]
        if len(chars) != 1:
            continue    # ナレーション・2ショットは対象外
        out.append({
            "cut_id": cut.get("cut_id"), "line_ids": line_ids, "pos": start,
            "head_line_id": head["line_id"], "char_id": head["cutout_char_id"],
            "slot_id": head["cutout_slot_id"], "emotion": (head.get("slot") or {}).get("emotion"),
            "pose": (head.get("slot") or {}).get("pose"),
            "emotion_tag": (head.get("slot") or {}).get("emotion_tag"),
            "protection": protection_reason(members, protected_line_ids),
        })
    return out


def compute_report(picture_cuts: list[dict], entry_of, distance_fn, orientation_fn,
                   near_threshold: float, window: int = DEFAULT_WINDOW) -> dict:
    """検査の本体（純粋関数）。

    picture_cuts: `collect_picture_cuts` の結果。
    entry_of(char_id, slot_id) -> 在庫の entry（無ければ None）。near の距離計算にだけ使う。
    distance_fn(fa, fb) / orientation_fn(entry): `cutout_selector` のものを渡す（同じ物差し）。
    """
    findings: dict[int, dict] = {}
    conflicts: list[dict] = []

    def mark(i: int, j: int, kind: str, dist: float | None) -> None:
        f = findings.setdefault(i, {"kind": "exact", "with": []})
        if kind == "exact":
            f["kind"] = "exact"      # exact は near より重い（優先して表示・直す）
        elif not f["with"]:
            f["kind"] = "near"
        other = picture_cuts[j]
        f["with"].append({"line_id": other["head_line_id"], "cut_id": other["cut_id"],
                          "kind": kind, **({"distance": round(dist, 3)} if dist is not None else {})})

    n = len(picture_cuts)
    for a in range(n):
        ca = picture_cuts[a]
        for b in range(a + 1, n):
            cb = picture_cuts[b]
            if cb["char_id"] != ca["char_id"]:
                continue
            kind, dist = None, None
            if cb["slot_id"] == ca["slot_id"]:
                kind = "exact"
            elif cb["pos"] - ca["pos"] <= window:
                ea, eb = entry_of(ca["char_id"], ca["slot_id"]), entry_of(cb["char_id"], cb["slot_id"])
                if ea and eb and orientation_fn(ea) == orientation_fn(eb):
                    d = distance_fn(ea.get("fingerprint"), eb.get("fingerprint"))
                    if d < near_threshold:
                        kind, dist = "near", d
            if kind is None:
                continue
            # 直す側: 後ろ（b）が守られていなければ b。守られていれば前（a）。両方守られていたら見せるだけ
            if not cb["protection"]:
                mark(b, a, kind, dist)
            elif not ca["protection"]:
                mark(a, b, kind, dist)
            else:
                conflicts.append({"line_ids": [ca["head_line_id"], cb["head_line_id"]], "kind": kind})

    items = []
    for i in sorted(findings):
        c, f = picture_cuts[i], findings[i]
        items.append({
            "line_id": c["head_line_id"], "line_ids": c["line_ids"], "cut_id": c["cut_id"],
            "char_id": c["char_id"], "slot_id": c["slot_id"], "emotion": c["emotion"],
            "kind": f["kind"], "with": f["with"],
        })
    return {
        "window": window, "near_threshold": near_threshold,
        "items": items, "conflicts": conflicts,
        "summary": {
            "cuts_with_picture": n,
            "protected_cuts": sum(1 for c in picture_cuts if c["protection"]),
            "items": len(items),
            "exact": sum(1 for it in items if it["kind"] == "exact"),
            "near": sum(1 for it in items if it["kind"] == "near"),
            "lines": sum(len(it["line_ids"]) for it in items),
            "conflicts": len(conflicts),
        },
    }


# ─── 直し（選び直し・生成に回す）の計画 ────────────────────────────────────────

RECENT_WINDOW = 5   # ポーズの近さを避ける小さい窓（`cutout_selector` の `recent_window` と同じ）


def plan_fixes(picture_cuts: list[dict], items: list[dict], entry_of, select_fn,
               window: int = DEFAULT_WINDOW, recent_window: int = RECENT_WINDOW) -> list[dict]:
    """検査の指摘ごとに「どの絵へ替えるか」を決める。**純粋関数**（何も書かない）。

    items: `compute_report` の `items`（台本順）。picture_cuts: `collect_picture_cuts` の結果。
    select_fn(char_id, emotion, pose, used_refs, window_entries, recent, prev, current) -> (entry|None, 理由)
      ＝`cutout_selector.select_replacement`。

    指摘を台本順に1つずつ決め、**決めた絵を以降の指摘の「使用中」へ積む**（同じ絵へ2行を寄せない）。
    各行の action: reselect（在庫に替えの絵がある）／generate（無い＝生成が要る）／skip（自動では選べない）／
    keep（先に直した行が重なりの相手を手放した結果、今の絵のままで重複しなくなった・書かない）。
    """
    by_head = {c["head_line_id"]: i for i, c in enumerate(picture_cuts)}
    holder: dict[int, tuple[str, str]] = {i: (c["char_id"], c["slot_id"]) for i, c in enumerate(picture_cuts)}
    count: dict[str, int] = {}

    def ref(c, s):
        return f"{c}/{s}"

    for ch, sl in holder.values():
        count[ref(ch, sl)] = count.get(ref(ch, sl), 0) + 1

    out: list[dict] = []
    for it in items:
        i = by_head.get(it["line_id"])
        if i is None:
            continue
        c = picture_cuts[i]
        cur_ref = ref(*holder[i])
        count[cur_ref] -= 1            # 自分の今の絵は手放す側（他の行が持っていれば used に残る）
        used = {r for r, n in count.items() if n > 0}
        # 同じキャラの絵だけが比較相手（今の割当・決めた替えを含む）。直近5行は前後とも見る
        mates = [(j, picture_cuts[j]) for j in range(len(picture_cuts))
                 if j != i and picture_cuts[j]["char_id"] == c["char_id"]]
        near = [entry_of(*holder[j]) for j, p in mates if abs(p["pos"] - c["pos"]) <= window]
        recent = [entry_of(*holder[j]) for j, p in mates if abs(p["pos"] - c["pos"]) <= recent_window]
        prev = entry_of(*holder[i - 1]) if i > 0 else None     # 直前の絵（同じ画角が隣り合うのを後回しにする用）
        cur_entry = entry_of(*holder[i])
        # 細かいタグ（§14）は持っている行だけ渡す（タグを知らない選び方の差し替えも受けられる）
        kw = {"tag": c["emotion_tag"]} if c.get("emotion_tag") else {}
        entry, why = select_fn(c["char_id"], c["emotion"], c["pose"], used,
                               [e for e in near if e], [e for e in recent if e], prev, cur_entry, **kw)
        row = {"line_id": it["line_id"], "line_ids": it["line_ids"], "cut_id": it["cut_id"],
               "kind": it["kind"], "char_id": c["char_id"], "from_slot": c["slot_id"],
               "to_slot": None, "reason": why}
        if entry is not None and entry["slot_id"] == c["slot_id"]:
            # 先に直した行が重なりの相手を手放したので、今の絵のままで重複しなくなった（書かない）
            row.update(action="keep", to_slot=c["slot_id"], reason="先の行の差し替えで重なりが解消（今の絵のまま）")
            count[cur_ref] += 1
        elif entry is not None:
            row.update(action="reselect", to_slot=entry["slot_id"])
            holder[i] = (c["char_id"], entry["slot_id"])
            count[ref(*holder[i])] = count.get(ref(*holder[i]), 0) + 1
        else:
            row["action"] = "skip" if c["emotion"] is None else "generate"
            count[cur_ref] += 1        # 替えが無い行は今の絵のまま（以降の指摘から見て使用中のまま）
        out.append(row)
    return out
