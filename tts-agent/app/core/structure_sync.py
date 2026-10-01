"""台本の行構造へ tts.json を合わせる（Docs/LINE_WORKBENCH_PLAN.md §3・I5・W1）。

台本から外れた行（削除・結合で吸収された行）の音声を**消さず孤立扱いにする**。
`tts.json` の `audio_files[]` からは外し（下流の editing-agent・タイムライン・director の
一覧は `audio_files[]` だけを読むので、そのまま「音声の無い行」に見える）、
`orphaned_audio_files[]` へ退避する。wav ファイルは `audio/` に残す。
台本へ行が戻れば（Undo）、同じエントリが `audio_files[]` へ戻る＝作り直さずに音声が生き返る。

⚠️ 計画書 §3-2 は「該当エントリに `orphan=true`」と書いていたが、`audio_files[]` の読み手が
複数（director UI・editing-agent の timeline_builder/srt_writer・scrapping-agent の
auto_selector・tts 自身の再構築）あり、フラグ方式だと全員が除外を書く必要がある。
別リストへ退避すれば読み手は無改修で済む（実装時の判断・計画書へ追記済み）。

純粋関数（ファイルI/Oなし）。呼び出し側（routes）が tts.json の読み書きとタイムライン再構築を担う。
"""
from __future__ import annotations

from datetime import datetime, timezone

ORPHAN_KEY = "orphaned_audio_files"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_structure(tts: dict, script_lines: list) -> dict:
    """tts を台本の行構造へ合わせる（in-place）。冪等。

    - 台本に無い行のエントリ → `orphaned_audio_files[]` へ（`orphaned_at` を付ける）
    - 台本へ戻った行の孤立エントリ → `audio_files[]` へ戻す（`orphaned_at` を外す）
    - 残る行の `order`・`parent_line_id`・話者名（ID/空のままのもの）を台本に追随させる（wav は触らない。
      並び替え・分割・結合の後にタイムラインの順序と TTSタブのグループ表示を正しくするため）

    `script_lines` は `Line`（`.id` `.order` `.parent_line_id` を持つもの）の並び。
    戻り値: {"orphaned": [...], "restored": [...], "refreshed": [...], "changed": bool}
    """
    by_id = {l.id: l for l in script_lines}
    files = list(tts.get("audio_files", []))
    orphans = list(tts.get(ORPHAN_KEY, []))
    live_ids = set(by_id)

    orphaned, restored, refreshed = [], [], []

    keep = []
    for f in files:
        if f.get("line_id") in live_ids:
            keep.append(f)
        else:
            orphaned.append(f.get("line_id"))
            orphans = [o for o in orphans if o.get("line_id") != f.get("line_id")]
            orphans.append({**f, "orphaned_at": _now()})

    have = {f.get("line_id") for f in keep}
    still_orphan = []
    for o in orphans:
        lid = o.get("line_id")
        if lid in live_ids and lid not in have:
            entry = {k: v for k, v in o.items() if k != "orphaned_at"}
            keep.append(entry)
            have.add(lid)
            restored.append(lid)
        elif lid in live_ids:
            continue  # 既に生きているエントリがある＝古い孤立の写しは捨てる（重複を作らない）
        else:
            still_orphan.append(o)

    for f in keep:
        line = by_id.get(f.get("line_id"))
        if line is None:
            continue
        changed = False
        if f.get("order") != line.order:
            f["order"] = line.order
            changed = True
        if (f.get("parent_line_id") or None) != (line.parent_line_id or None):
            f["parent_line_id"] = line.parent_line_id
            changed = True
        # 話者名が ID のまま・空のエントリは、台本（配役で補った名前）に追随させる。
        # 字幕の「名前: 本文」・編集のトラック名に出る。音声は触らない（キャッシュは名前を見ない）
        name = getattr(line, "speaker_name", "") or ""
        if ("speaker_name" in f and name and name != line.speaker_id
                and f.get("speaker_name") in ("", None, f.get("speaker_id"))
                and f.get("speaker_name") != name):
            f["speaker_name"] = name
            changed = True
        if changed:
            refreshed.append(f.get("line_id"))

    tts["audio_files"] = sorted(keep, key=lambda e: e.get("order", 0))
    if still_orphan:
        tts[ORPHAN_KEY] = still_orphan
    else:
        tts.pop(ORPHAN_KEY, None)

    return {"orphaned": orphaned, "restored": restored, "refreshed": refreshed,
            "changed": bool(orphaned or restored or refreshed)}


def carry_orphans(old_tts: dict | None, live_ids: set) -> list:
    """全行生成（`_run_project`）が tts.json を作り直す時に持ち越す孤立エントリを返す。

    前回の孤立エントリに加え、前回の `audio_files[]` にあって今の台本に無い行
    （台本を窓口以外の経路で変えられて、まだ孤立化されていなかった行）も孤立扱いで残す。
    今の台本に居る行は含めない。
    """
    if not old_tts:
        return []
    out = {}
    for o in old_tts.get(ORPHAN_KEY, []):
        if o.get("line_id") not in live_ids:
            out[o.get("line_id")] = o
    for f in old_tts.get("audio_files", []):
        if f.get("line_id") not in live_ids:
            out[f.get("line_id")] = {**f, "orphaned_at": _now()}
    return list(out.values())
