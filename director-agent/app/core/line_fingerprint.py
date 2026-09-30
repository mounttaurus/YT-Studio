"""行の指紋と差分（Docs/LINE_WORKBENCH_PLAN.md §4-2・§7-1）。

**行の指紋関数はここ1つ。** W1 は「操作の前後で何が変わったか」（窓口の影響の導出）に使い、
W2 の行の確定（`confirmations.json`）と、将来の翻訳の「訳が古いか」の判定が同じ関数を使う
（訳の各行に「訳した時点の原文の指紋」を残せば、原文を直した行の訳だけが古いと分かる）。
対象の項目は §4-2 の固定リスト。**項目を足す時は `FINGERPRINT_FIELDS` だけを直す**
（呼び出し側に項目名を持たせない＝黙って比較から漏れる穴を作らない。memory
`manifest-rebuild-fixed-keys-drop-new-fields` と同じ轍）。

純粋関数（ファイルI/Oなし）。
"""
from __future__ import annotations

import hashlib
import json

FINGERPRINT_FIELDS = ("id", "text", "speaker_id", "section", "parent_line_id",
                      "emotion", "speed", "pause_after_sec")


def _norm(field: str, value):
    """保存のゆれ（1 と 1.0・欠落と null・欠落の感情）を同じ値に寄せる。"""
    if field == "parent_line_id":
        return value or None
    if field in ("speed", "pause_after_sec"):
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return None
    if field == "emotion":
        return value or "neutral"
    return value if value is not None else ""


def line_fields(line: dict, fields: tuple = FINGERPRINT_FIELDS) -> dict:
    return {f: _norm(f, line.get(f)) for f in fields}


def line_fingerprint(line: dict, fields: tuple = FINGERPRINT_FIELDS) -> str:
    """行の指紋（`fields` の値だけから作る。`order` などの位置情報は含めない）。"""
    payload = json.dumps(line_fields(line, fields), ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def changed_fields(before: dict, after: dict, fields: tuple = FINGERPRINT_FIELDS) -> list[str]:
    """同じ行の前後で値が変わった項目名（`id` は同一行の比較なので除く）。"""
    a, b = line_fields(before, fields), line_fields(after, fields)
    return [f for f in fields if f != "id" and a[f] != b[f]]


def diff_lines(before: list[dict], after: list[dict], fields: tuple = FINGERPRINT_FIELDS) -> dict:
    """台本の行配列の前後の差分。窓口の影響の文面と後処理の入力になる。

    - new_ids: 後にだけある行（分割の後半・挿入・サブ行追加・Undoで戻った行）
    - removed_ids: 前にだけある行（削除・結合で吸収された行・分割のUndo）
    - changed: {line_id: [変わった項目]}（両方にある行）
    - moved_ids: 両方にある行のうち、並びの位置が変わった行
    """
    b_ids = [l.get("id") for l in before]
    a_ids = [l.get("id") for l in after]
    b_set, a_set = set(b_ids), set(a_ids)
    b_by = {l.get("id"): l for l in before}
    a_by = {l.get("id"): l for l in after}

    changed = {}
    for lid in a_ids:
        if lid in b_set:
            fs = changed_fields(b_by[lid], a_by[lid], fields)
            if fs:
                changed[lid] = fs

    common_b = [i for i in b_ids if i in a_set]
    common_a = [i for i in a_ids if i in b_set]
    moved = set()
    for i, j in zip(common_b, common_a):
        if i != j:
            moved.update((i, j))
    moved_ids = sorted(moved, key=a_ids.index)

    return {
        "new_ids": [i for i in a_ids if i not in b_set],
        "removed_ids": [i for i in b_ids if i not in a_set],
        "changed": changed,
        "moved_ids": moved_ids,
    }
