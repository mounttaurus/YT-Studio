"""
サブ行（1行に複数の絵を当てる・`Docs/SUBLINE_PLAN.md`）── S1: 台本側の土台。

サブ行は「普通の行」で、`parent_line_id` を持つだけの違い（I1）。この module は
行配列（`doc["lines"]`）を直接操作する純粋関数群で、FastAPI 依存もファイルI/Oも無い
（`app/api/routes.py` が draft/script の読み書きと HTTPException への変換を担う）。

**グループ ＝ 連続・同じ話者・同じセクション（I2）。** どの書き込み経路もこれを保つ：
壊れる操作は拒否する（`move_line_in_doc` のグループ越え）か、そこでグループを2つに
割る（`split_group_for_insertion`）。全ての行操作は最終的に `remove_line_from_doc` /
`insert_line_after` / `move_line_in_doc` を通るので、そこにグループ規則を集約する。

IDは使い回さない（I3）: サブ行は `{親のID}_s{n}`。`n` は `metadata.subline_seq` の
連番で、消しても減らさない（`allocate_subline_seq`）。
"""
from __future__ import annotations

import re
from typing import Optional

DEFAULT_SPLIT_LIMIT = 55  # 字（≈12秒。§0-2の実測 秒≈0.8+0.2×字から）

_SENTENCE_END_CHARS = "。！？"
_COMMA_CHAR = "、"


# ---------------------------------------------------------------------------
# 行配列の基本操作（既存の行CRUD・サブ行操作の両方が通る唯一の実装）
# ---------------------------------------------------------------------------

def renumber_lines(doc: dict) -> None:
    for i, l in enumerate(doc.get("lines", []), 1):
        l["order"] = i
    doc.setdefault("metadata", {})["line_count"] = len(doc.get("lines", []))


def next_line_id(*docs: Optional[dict]) -> str:
    """未使用の `line_NNN` を発番する（サブ行IDは `_s{n}` 接尾辞を持つため対象外）。"""
    mx = 0
    for doc in docs:
        if not doc:
            continue
        for l in doc.get("lines", []):
            m = re.match(r"line_(\d+)$", str(l.get("id", "")))
            if m:
                mx = max(mx, int(m.group(1)))
    return f"line_{mx + 1:03d}"


def group_members(doc: dict, parent_id: str) -> list[dict]:
    """`parent_id` を親に持つ行を並び順のまま返す（親自身も `parent_line_id==自分のid` で含む）。"""
    return [l for l in doc.get("lines", []) if l.get("parent_line_id") == parent_id]


def close_singleton_groups(doc: dict, parent_ids: set) -> list[str]:
    """グループが1行だけになったら `parent_line_id` を外す（普通の行に戻る・§5）。"""
    closed = []
    for pid in parent_ids:
        if not pid:
            continue
        members = group_members(doc, pid)
        if len(members) == 1:
            members[0]["parent_line_id"] = None
            closed.append(pid)
    return closed


def split_group_for_insertion(doc: dict, new_line_id: str) -> None:
    """新しい行を挿入した直後に呼ぶ。挿入位置が既存グループの内部で、挿入した行自身は
    そのグループの一員でない場合、そこでグループを2つに割る（I2）。

    ⚠️ 挿入した行がグループの正規の一員として作られた場合（分ける/追加の後半）は
    そもそも前後と同じ `parent_line_id` を持つので、ここは何もしない
    （`insert_line_after` から常に呼んでよい安全な設計）。
    """
    lines = doc.get("lines", [])
    idx = next((i for i, l in enumerate(lines) if l.get("id") == new_line_id), None)
    if idx is None or idx == 0 or idx + 1 >= len(lines):
        return
    new_line, prev_line, next_line = lines[idx], lines[idx - 1], lines[idx + 1]
    prev_pid, next_pid = prev_line.get("parent_line_id"), next_line.get("parent_line_id")
    if not prev_pid or prev_pid != next_pid:
        return  # 元々ひと続きのグループではなかった
    if new_line.get("parent_line_id") == prev_pid:
        return  # 新しい行も同じグループの一員として挿入された（分ける/追加）＝連続性は壊れていない

    new_pid = next_line["id"]
    j = idx + 1
    while j < len(lines) and lines[j].get("parent_line_id") == prev_pid:
        lines[j]["parent_line_id"] = new_pid
        j += 1
    close_singleton_groups(doc, {prev_pid, new_pid})


def insert_line_after(doc: dict, anchor_id: Optional[str], new_line: dict) -> None:
    """`anchor_id` の直後（Noneなら先頭）へ行を挿入する。order振り直し・sections同期・
    グループ連続性の維持（`split_group_for_insertion`）を一括して行う唯一の挿入経路。
    """
    dl = doc.setdefault("lines", [])
    if anchor_id is None:
        pos = 0
    else:
        idx = next((i for i, l in enumerate(dl) if l.get("id") == anchor_id), None)
        pos = (idx + 1) if idx is not None else len(dl)
    dl.insert(pos, dict(new_line))
    renumber_lines(doc)

    new_id = new_line["id"]
    placed = False
    for s in doc.get("sections") or []:
        ids = s.get("line_ids") or []
        if anchor_id is not None and anchor_id in ids:
            ids.insert(ids.index(anchor_id) + 1, new_id)
            placed = True
            break
    secs = doc.get("sections") or []
    if not placed and secs:
        if anchor_id is None:
            secs[0].setdefault("line_ids", []).insert(0, new_id)
        else:
            secs[-1].setdefault("line_ids", []).append(new_id)

    split_group_for_insertion(doc, new_id)


def remove_line_from_doc(doc: dict, line_id: str) -> bool:
    """`doc['lines']` から line_id を除去し、order振り直し＋sections同期＋グループの
    後始末（1行だけになったら parent_line_id を外す）をする。除去できたかを返す。
    """
    removed_line = next((l for l in doc.get("lines", []) if l.get("id") == line_id), None)
    before = len(doc.get("lines", []))
    doc["lines"] = [l for l in doc.get("lines", []) if l.get("id") != line_id]
    removed = len(doc["lines"]) < before
    if removed:
        renumber_lines(doc)
        for s in doc.get("sections") or []:
            if s.get("line_ids"):
                s["line_ids"] = [i for i in s["line_ids"] if i != line_id]
        parent_id = (removed_line or {}).get("parent_line_id")
        if parent_id:
            close_singleton_groups(doc, {parent_id})
    return removed


def move_line_in_doc(doc: dict, line_id: str, direction: str) -> Optional[str]:
    """doc内でline_idを隣接行と入れ替える（同一section・同一グループ内のみ）。
    order・sections.line_idsも同期する。成功時 None、失敗理由があればその文字列を返す。
    """
    lines = doc.get("lines", [])
    idx = next((i for i, l in enumerate(lines) if l.get("id") == line_id), None)
    if idx is None:
        return None  # このdocにその行が無いだけ＝正常（draft/script間の差分は許容）
    swap_idx = idx - 1 if direction == "up" else idx + 1
    if swap_idx < 0 or swap_idx >= len(lines):
        return "これ以上移動できません（先頭/末尾です）"
    if lines[idx].get("section") != lines[swap_idx].get("section"):
        return "セクションをまたぐ移動はできません"
    if (lines[idx].get("parent_line_id") or None) != (lines[swap_idx].get("parent_line_id") or None):
        return "グループをまたぐ移動はできません"

    lines[idx], lines[swap_idx] = lines[swap_idx], lines[idx]
    renumber_lines(doc)
    for s in doc.get("sections") or []:
        ids = s.get("line_ids") or []
        if line_id in ids:
            other_id = lines[idx]["id"] if lines[swap_idx]["id"] == line_id else lines[swap_idx]["id"]
            i1, i2 = ids.index(line_id), ids.index(other_id) if other_id in ids else None
            if i2 is not None:
                ids[i1], ids[i2] = ids[i2], ids[i1]
            break
    return None


def propagate_speaker_change(doc: dict, line_id: str, speaker_id: str, speaker_name: str) -> list[str]:
    """行の話者を変える。グループに属する行なら、グループ全体へ同じ変更を伝える
    （サブ行だけ話者を変える操作は受け付けない＝常に伝わる。§5）。変更した行idを返す。
    """
    line = next((l for l in doc.get("lines", []) if l.get("id") == line_id), None)
    if line is None:
        return []
    parent_id = line.get("parent_line_id")
    targets = group_members(doc, parent_id) if parent_id else [line]
    for l in targets:
        l["speaker_id"] = speaker_id
        l["speaker_name"] = speaker_name
    return [l["id"] for l in targets]


# ---------------------------------------------------------------------------
# サブ行の連番（I3: 消しても再利用しない）
# ---------------------------------------------------------------------------

def subline_id(parent_id: str, n: int) -> str:
    return f"{parent_id}_s{n}"


def peek_subline_seq(docs: list[Optional[dict]], parent_id: str) -> int:
    """draft/script等、複数docを通した現在のカウンタ最大値（未記録なら0）。"""
    return max(
        (int(((d.get("metadata") or {}).get("subline_seq") or {}).get(parent_id, 0)) for d in docs if d),
        default=0,
    )


def allocate_subline_seq(docs: list[Optional[dict]], parent_id: str, count: int) -> list[int]:
    """count個の連番を割り当て、渡した全docのカウンタを同じ値まで進める（I3）。
    グループの行が全部消えてもこのカウンタ自体はリセットしない（呼び出し側の責務＝
    このカウンタを消す経路を作らないこと）。
    """
    start = peek_subline_seq(docs, parent_id) + 1
    new_max = start + count - 1
    for d in docs:
        if d is None:
            continue
        seq = d.setdefault("metadata", {}).setdefault("subline_seq", {})
        seq[parent_id] = max(seq.get(parent_id, 0), new_max)
    return list(range(start, new_max + 1))


# ---------------------------------------------------------------------------
# 自動区切り（§9-1: 句点優先・最少分割＋均す・純粋関数）
# ---------------------------------------------------------------------------

def _cut_candidates(text: str, limit: int) -> tuple[list[int], set]:
    """句点候補（全文）と読点候補（上限を超える文の中だけ）を返す。"""
    n = len(text)
    sentence_ends = sorted({i + 1 for i, ch in enumerate(text) if ch in _SENTENCE_END_CHARS})
    sentence_ends = [pos for pos in sentence_ends if 0 < pos < n]
    bounds = [0, *sentence_ends, n]
    comma_candidates: set = set()
    for a, b in zip(bounds, bounds[1:]):
        if b - a > limit:
            comma_candidates.update(
                i + 1 for i in range(a, b) if text[i] == _COMMA_CHAR and 0 < i + 1 < n
            )
    return sorted(set(sentence_ends) | comma_candidates), comma_candidates


def propose_splits(text: str, limit: int = DEFAULT_SPLIT_LIMIT) -> list[dict]:
    """上限字数に収まるよう句点優先・最少分割＋均すで区切りを提案する（§9-1）。

    必要な数＝上限に収まる最少の数。同じ数の中で最長の片が一番短くなる位置を選ぶ。
    候補（句点・読点）が無ければ分けられない。読点で切った所は ``split_review`` を立てる。

    返り値: [{"start","end","text","split_review"}, ...]。既に上限内、または候補が
    無い（読点の無い長い1文）なら空リスト（＝手動でしか分けられない・§9-2）。
    """
    n = len(text)
    if n <= limit:
        return []
    candidates, comma_candidates = _cut_candidates(text, limit)
    if not candidates:
        return []

    positions = [0, *candidates, n]
    p = len(positions)
    inf = float("inf")
    dp = [[inf] * p for _ in range(p)]
    parent: list[list[Optional[int]]] = [[None] * p for _ in range(p)]
    dp[0][0] = 0
    for c in range(1, p):
        for i in range(c, p):
            for j in range(c - 1, i):
                if dp[c - 1][j] == inf:
                    continue
                cand = max(dp[c - 1][j], positions[i] - positions[j])
                if cand < dp[c][i]:
                    dp[c][i] = cand
                    parent[c][i] = j

    chosen_c = next(
        (c for c in range(1, p) if dp[c][p - 1] < inf and (dp[c][p - 1] <= limit or c == p - 1)),
        None,
    )
    if chosen_c is None:
        return []  # 到達不能（候補がある以上通常は起きない）

    seq = [positions[p - 1]]
    i, c = p - 1, chosen_c
    while c >= 1:
        j = parent[c][i]
        if j is None:
            return []
        seq.append(positions[j])
        i, c = j, c - 1
    seq.reverse()
    if len(seq) < 2:
        return []
    cuts = seq[1:-1]
    review_at = set(cuts) & comma_candidates

    return [
        {"start": s, "end": e, "text": text[s:e], "split_review": s in review_at or e in review_at}
        for s, e in zip(seq, seq[1:])
    ]


# ---------------------------------------------------------------------------
# 行操作（分ける・結合・追加・自動区切り適用）── §5・§9-3
# ---------------------------------------------------------------------------

def _docs_with_line(docs: list[Optional[dict]], line_id: str) -> list[dict]:
    return [d for d in docs if d and any(l.get("id") == line_id for l in d.get("lines", []))]


def _find_line(doc: dict, line_id: str) -> Optional[dict]:
    return next((l for l in doc.get("lines", []) if l.get("id") == line_id), None)


def split_line(docs: list[Optional[dict]], line_id: str, position: int) -> dict:
    """行Xを位置kで分ける（§5「分ける」）。前半はXのIDのまま、後半は新しいサブ行。

    Xが普通の行なら、この操作でXがグループの先頭（parent_line_id=X.id）になる。
    docsで見つかった全ドキュメント（draft・script）へ同じ変更を反映し、サブ行の
    連番はdocsを通した最大値+1から発番する（I3）。
    """
    holders = _docs_with_line(docs, line_id)
    if not holders:
        raise ValueError(f"line not found: {line_id}")
    ref = _find_line(holders[0], line_id)
    text = ref.get("text", "")
    if not (0 < position < len(text)):
        raise ValueError(f"position は 1〜{len(text) - 1} の範囲で指定してください（text長={len(text)}）")

    parent_id = ref.get("parent_line_id") or line_id
    (new_n,) = allocate_subline_seq(docs, parent_id, 1)
    new_id = subline_id(parent_id, new_n)

    front_text, back_text = text[:position], text[position:]
    for doc in holders:
        line = _find_line(doc, line_id)
        line["parent_line_id"] = parent_id
        line["text"] = front_text
        new_line = {
            "id": new_id,
            "order": 0,
            "speaker_id": line.get("speaker_id", ""),
            "speaker_name": line.get("speaker_name", ""),
            "text": back_text,
            "emotion": line.get("emotion", "neutral"),
            "speed": line.get("speed", 1.0),
            "pause_after_sec": line.get("pause_after_sec", 0.4),
            "section": line.get("section", "main"),
            "notes": "",
            "parent_line_id": parent_id,
        }
        insert_line_after(doc, line_id, new_line)

    return {"front_line_id": line_id, "back_line_id": new_id, "parent_line_id": parent_id}


def merge_lines(docs: list[Optional[dict]], first_id: str, second_id: str) -> dict:
    """隣り合う2行の本文をつなぐ（§5「結合」）。残るのは前の行(first_id)のID。

    話者・セクション・グループ（parent_line_id）が一致しない行は結合できない
    （結合後に1行として成立しなくなるため）。後の行は削除扱い（在庫等の後始末はS2）。
    """
    holders = _docs_with_line(docs, first_id)
    if not holders:
        raise ValueError(f"line not found: {first_id}")
    first_ref, second_ref = _find_line(holders[0], first_id), _find_line(holders[0], second_id)
    if second_ref is None:
        raise ValueError(f"line not found: {second_id}")
    lines = holders[0].get("lines", [])
    i1 = next(i for i, l in enumerate(lines) if l.get("id") == first_id)
    i2 = next(i for i, l in enumerate(lines) if l.get("id") == second_id)
    if i2 != i1 + 1:
        raise ValueError("結合できるのは隣り合う行だけです")
    if first_ref.get("speaker_id") != second_ref.get("speaker_id"):
        raise ValueError("話者が違う行は結合できません")
    if first_ref.get("section") != second_ref.get("section"):
        raise ValueError("セクションが違う行は結合できません")
    if (first_ref.get("parent_line_id") or None) != (second_ref.get("parent_line_id") or None):
        raise ValueError("グループが違う行は結合できません")

    for doc in docs:
        if not doc:
            continue
        f, s = _find_line(doc, first_id), _find_line(doc, second_id)
        if f is None or s is None:
            continue
        f["text"] = f.get("text", "") + s.get("text", "")
        remove_line_from_doc(doc, second_id)

    return {"merged_line_id": first_id, "removed_line_id": second_id}


def add_subline(
    docs: list[Optional[dict]], anchor_id: str, text: str = "", emotion: Optional[str] = None,
) -> dict:
    """anchor_idの直後に、同じグループの新しいサブ行を挿入する（§5「追加」）。
    anchorが普通の行ならグループの先頭になる。話者・セクションはanchorから引き継ぐ
    （サブ行の話者は個別に選べない＝グループの一員である前提）。
    """
    holders = _docs_with_line(docs, anchor_id)
    if not holders:
        raise ValueError(f"line not found: {anchor_id}")
    ref = _find_line(holders[0], anchor_id)
    parent_id = ref.get("parent_line_id") or anchor_id
    (new_n,) = allocate_subline_seq(docs, parent_id, 1)
    new_id = subline_id(parent_id, new_n)

    for doc in holders:
        line = _find_line(doc, anchor_id)
        line["parent_line_id"] = parent_id
        new_line = {
            "id": new_id,
            "order": 0,
            "speaker_id": line.get("speaker_id", ""),
            "speaker_name": line.get("speaker_name", ""),
            "text": text,
            "emotion": emotion if emotion is not None else line.get("emotion", "neutral"),
            "speed": line.get("speed", 1.0),
            "pause_after_sec": line.get("pause_after_sec", 0.4),
            "section": line.get("section", "main"),
            "notes": "",
            "parent_line_id": parent_id,
        }
        insert_line_after(doc, anchor_id, new_line)

    return {"new_line_id": new_id, "parent_line_id": parent_id}


def apply_auto_split(
    docs: list[Optional[dict]], line_id: str, limit: int = DEFAULT_SPLIT_LIMIT,
) -> dict:
    """自動区切りの提案（``propose_splits``）を実際に適用する（§9-3）。
    最初の断片は元の行のIDのまま、残りは新しいサブ行として直後に連続挿入する。
    """
    holders = _docs_with_line(docs, line_id)
    if not holders:
        raise ValueError(f"line not found: {line_id}")
    ref = _find_line(holders[0], line_id)
    pieces = propose_splits(ref.get("text", ""), limit)
    if len(pieces) < 2:
        raise ValueError("この行は自動区切りの対象ではありません（上限内、または句読点が無い）")

    parent_id = ref.get("parent_line_id") or line_id
    seq_ns = allocate_subline_seq(docs, parent_id, len(pieces) - 1)
    new_ids = [subline_id(parent_id, n) for n in seq_ns]

    for doc in holders:
        line = _find_line(doc, line_id)
        line["parent_line_id"] = parent_id
        line["text"] = pieces[0]["text"]
        anchor = line_id
        for piece, new_id in zip(pieces[1:], new_ids):
            new_line = {
                "id": new_id,
                "order": 0,
                "speaker_id": line.get("speaker_id", ""),
                "speaker_name": line.get("speaker_name", ""),
                "text": piece["text"],
                "emotion": line.get("emotion", "neutral"),
                "speed": line.get("speed", 1.0),
                "pause_after_sec": line.get("pause_after_sec", 0.4),
                "section": line.get("section", "main"),
                "notes": "",
                "parent_line_id": parent_id,
                "split_review": piece["split_review"],
            }
            insert_line_after(doc, anchor, new_line)
            anchor = new_id

    return {"line_id": line_id, "parent_line_id": parent_id, "new_line_ids": new_ids,
            "split_review": [p["split_review"] for p in pieces[1:]]}
