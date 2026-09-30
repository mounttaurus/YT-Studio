"""「LLMの案を採用」（Docs/LINE_WORKBENCH_PLAN.md §4-1・D8-①・W2）── ドラフト → 正本 の差分と採用。

LLM が書いた案（全文生成・再生成・シリーズ生成・`regenerate-lines`・外部取込み）は、今どおり
**ドラフトにしか書かれない**。それを正本（script.json）へ入れるのが「採用」で、**行ごとの差分として
見せてから**入れる。`regenerate-lines` は `line_id` を保つので、採用した行が「未確定」になるだけ
（確定は director 側の `confirmations.json`＝指紋の比較で導出）。承認後に `regenerate-lines` を使うと
ドラフトだけが変わって正本・TTS と黙ってずれる、という穴（§2-2）はこの採用を通すことで塞ぐ。

**採用の種類（`classify`）**
- `initial` … 正本がまだ無い（最初の承認）。ドラフトをそのまま正本にする
- `replace` … 全文の再生成（`generated_at` がドラフトと正本で違う）。**`line_id` が振り直される**ので、
  採用すると音声・絵の紐付けがすべて切れる。明示（`replace_all=true`）が要る
- `lines`   … 行単位の差（`regenerate-lines`・外部取込み等）。行を選んで採用できる（新規・削除も）
- `none`    … 差が無い

純粋関数（ファイルI/Oなし）。呼び出し側（routes）が読み書きとステータス更新を担う。
"""
from __future__ import annotations

import copy
from typing import Optional

from app.core import subline_manager

INITIAL, REPLACE, LINES, NONE = "initial", "replace", "lines", "none"


def _lines(doc: Optional[dict]) -> list[dict]:
    return list((doc or {}).get("lines", []))


def _diff_keys(a: dict, b: dict) -> list[str]:
    """同じ行の2つの版で値が違う項目（並び順 `order` は位置情報なので数えない）。"""
    keys = (set(a) | set(b)) - {"order"}
    return sorted(k for k in keys if a.get(k) != b.get(k))


def classify(draft: dict, script: Optional[dict]) -> str:
    if script is None:
        return INITIAL
    g_d, g_s = draft.get("generated_at"), script.get("generated_at")
    # 全文生成は generated_at を新しく打つ（regenerate-lines・承認・行操作は変えない）
    if (g_d or g_s) and g_d != g_s:
        return REPLACE
    return LINES if _differing(draft, script) else NONE


def _differing(draft: dict, script: dict) -> dict[str, str]:
    """{line_id: 'changed'|'new'|'removed'}（ドラフトの並び → 正本にだけある行の順）。"""
    d_by = {l.get("id"): l for l in _lines(draft)}
    s_by = {l.get("id"): l for l in _lines(script)}
    out: dict[str, str] = {}
    for lid, dl in d_by.items():
        if lid not in s_by:
            out[lid] = "new"
        elif _diff_keys(dl, s_by[lid]):
            out[lid] = "changed"
    for lid in s_by:
        if lid not in d_by:
            out[lid] = "removed"
    return out


def proposal(draft: dict, script: Optional[dict]) -> dict:
    """採用の前に見せる、行ごとの差分。何も変えない。"""
    kind = classify(draft, script)
    d_by = {l.get("id"): l for l in _lines(draft)}
    s_by = {l.get("id"): l for l in _lines(script)}
    rows = []
    if kind in (LINES, REPLACE):
        for lid, change in _differing(draft, script).items():
            before, after = s_by.get(lid), d_by.get(lid)
            rows.append({
                "line_id": lid, "change": change,
                "fields": _diff_keys(before, after) if change == "changed" else [],
                "before": before, "after": after,
            })
    warnings = []
    if kind == REPLACE:
        warnings.append("全文の再生成の案です（line_id が振り直されています）。採用すると、音声・絵の紐付け"
                        "（line_id で結び付いたもの）がすべて切れます。採用するには replace_all=true が要ります")
    return {
        "kind": kind, "has_script": script is not None,
        "requires_replace_all": kind == REPLACE,
        "counts": {c: sum(1 for r in rows if r["change"] == c) for c in ("changed", "new", "removed")},
        "lines": rows, "warnings": warnings,
    }


def _group_violations(doc: dict) -> set:
    """サブ行のグループ規則（I2: 連続・同じ話者・同じセクション）を破っているグループ。"""
    bad = set()
    lines = doc.get("lines", [])
    for pid in {l.get("parent_line_id") for l in lines if l.get("parent_line_id")}:
        idx = [i for i, l in enumerate(lines) if l.get("parent_line_id") == pid]
        members = [lines[i] for i in idx]
        contiguous = idx == list(range(idx[0], idx[0] + len(idx)))
        same = len({(m.get("speaker_id"), m.get("section")) for m in members}) == 1
        if not (contiguous and same):
            bad.add(pid)
    return bad


def adopt(draft: dict, script: Optional[dict], line_ids: Optional[list[str]] = None,
          replace_all: bool = False) -> tuple[dict, dict]:
    """ドラフトを正本へ採用する。(採用後の正本, 結果) を返す。呼び出し側が保存する。

    ValueError = 採用できない（理由を人が読める文字列で）。`draft`・`script` は書き換えない。
    """
    kind = classify(draft, script)
    if kind == NONE:
        raise ValueError("採用する差がありません（ドラフトと正本は同じです）")

    if kind in (INITIAL, REPLACE):
        if kind == REPLACE and not replace_all:
            raise ValueError("全文の再生成の案です（line_id が振り直されています）。採用すると音声・絵の紐付けが"
                             "すべて切れます。replace_all=true を付けて明示してください")
        if line_ids is not None:
            raise ValueError("全文の採用では行を選べません（line_ids を外してください）")
        new = copy.deepcopy(draft)
        new.setdefault("metadata", {})
        new["metadata"]["checked_by_director"] = True
        new["metadata"]["check_passed"] = True
        d_ids = {l.get("id") for l in _lines(draft)}
        s_ids = {l.get("id") for l in _lines(script)}
        return new, {"kind": kind, "adopted": [], "inserted": [], "removed": [],
                     "replaced_all": True, "line_count": len(d_ids),
                     "kept_ids": sorted(d_ids & s_ids)}

    diffs = _differing(draft, script)
    wanted = list(diffs) if line_ids is None else list(dict.fromkeys(line_ids))
    unknown = [i for i in wanted if i not in diffs]
    if unknown:
        raise ValueError(f"差のない行、または存在しない行は採用できません: {', '.join(unknown)}")

    new = copy.deepcopy(script)
    before_bad = _group_violations(new)
    d_lines = _lines(draft)
    adopted, inserted, removed = [], [], []

    for lid in wanted:                                   # ① 書き換え（並び順は正本のまま）
        if diffs[lid] == "changed":
            dl = next(l for l in d_lines if l.get("id") == lid)
            cur = next(l for l in new["lines"] if l.get("id") == lid)
            order = cur.get("order")
            cur.clear()
            cur.update(copy.deepcopy(dl))
            cur["order"] = order
            adopted.append(lid)
    for i, dl in enumerate(d_lines):                     # ② 新規（ドラフトの並びで、正本にある直前の行の後ろへ）
        lid = dl.get("id")
        if lid in wanted and diffs[lid] == "new":
            present = {l.get("id") for l in new["lines"]}
            anchor = next((d_lines[j].get("id") for j in range(i - 1, -1, -1)
                           if d_lines[j].get("id") in present), None)
            subline_manager.insert_line_after(new, anchor, copy.deepcopy(dl))
            inserted.append(lid)
    for lid in wanted:                                   # ③ 削除
        if diffs[lid] == "removed":
            subline_manager.remove_line_from_doc(new, lid)
            removed.append(lid)

    subline_manager.renumber_lines(new)
    subline_manager.merge_subline_seq(new, [draft, script])
    broken = _group_violations(new) - before_bad
    if broken:
        raise ValueError("この採用だと、サブ行のグループが壊れます（グループの行は連続・同じ話者・同じセクション）: "
                         f"{', '.join(sorted(broken))}。グループの行をまとめて採用してください")
    new.setdefault("metadata", {})["checked_by_director"] = True
    new["metadata"]["check_passed"] = True
    return new, {"kind": LINES, "adopted": adopted, "inserted": inserted, "removed": removed,
                 "replaced_all": False}
