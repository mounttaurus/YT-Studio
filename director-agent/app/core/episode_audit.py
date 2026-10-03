"""1話の整合検査（台本→確定→音声→絵→仕上がり）。読み取り専用・何も書かない。

ユーザーが台本に手を入れ続けると、後続（音声・絵・組版）がどこまで追随しているかが分からなくなる。
その「どこまで最新か」を機械で出し、次にやること（ツール名つき）を順に並べる。
Claude Code に後続を任せる運用の入口（`audit_episode`）で、判定はここ（コード）に置き、
手順と判断は Skill 側に置く（Docs/SKILL_DESIGN_PLAN.md R3）。

土台は `workbench_view.build_view`（行ごとの状態）。そこに無い**構造の検査**（章リスト・行ID・親子・
音声ファイル・組版プランの孤立）だけを生のファイルから足す。
`compute(view, raw)` は純粋関数（pytest で固める）、`build_audit` が実データを集める。

issue の severity: error＝次の工程へ進む前に直す／warn＝直した方がよい／info＝知っておく（実害は小さい）。
layer の state: ok／behind（追随が要る）／unknown（他コンテナに繋がらない等）／na（その環境に無い工程）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from app.core import aroll_duplicates, downstream, workbench_view
from app.core.line_ops import _ep_dir

LONG_LIMIT = 55   # 字。ワークベンチの「長い」と同じ（static/workbench/js/model.js LONG_LIMIT）
_SUB = re.compile(r"^(?P<parent>.+)_s\d+$")


def _issue(code, severity, message, line_ids=None, **extra) -> dict:
    out = {"code": code, "severity": severity, "message": message}
    if line_ids is not None:
        out["line_ids"] = list(line_ids)
        out["count"] = len(line_ids)
    out.update(extra)
    return out


def _layer(issues: list[dict], **extra) -> dict:
    state = "behind" if any(i["severity"] in ("error", "warn") for i in issues) else "ok"
    return {"state": state, "issues": issues, **extra}


def _short(ids: list[str], n: int = 6) -> str:
    return ", ".join(ids[:n]) + (f" 他{len(ids) - n}行" if len(ids) > n else "")


# ── 台本の構造 ─────────────────────────────────────────────────────────────

def _script_issues(script: dict) -> list[dict]:
    lines = script.get("lines") or []
    ids = [l.get("id") for l in lines]
    out: list[dict] = []
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})
    if dup_ids:
        out.append(_issue("ID_DUP", "error", f"同じ行IDが複数あります: {_short(dup_ids)}", dup_ids))
    if [l.get("order") for l in lines] != list(range(1, len(lines) + 1)):
        out.append(_issue("ORDER_BROKEN", "warn", "行の order が 1,2,3… の連番になっていません（次の行操作で振り直されます）"))
    secs = script.get("sections") or []
    if secs:
        flat = [x for s in secs for x in (s.get("line_ids") or [])]
        sdup = sorted({x for x in flat if flat.count(x) > 1})
        if sdup:
            out.append(_issue("SECTION_DUP", "warn", f"章のリストに同じ行が重複しています: {_short(sdup)}", sdup))
        smissing = [i for i in ids if i not in set(flat)]
        if smissing:
            out.append(_issue("SECTION_MISSING", "warn", f"どの章にも入っていない行があります: {_short(smissing)}", smissing))
        sunknown = sorted({x for x in flat if x not in set(ids)})
        if sunknown:
            out.append(_issue("SECTION_UNKNOWN", "warn", f"章のリストにあるが台本に無い行IDがあります: {_short(sunknown)}", sunknown))
        bad_sec = [l["id"] for l in lines if not any(l.get("section") == s.get("id") and l["id"] in (s.get("line_ids") or []) for s in secs)
                   and l["id"] in set(flat)]
        if bad_sec:
            out.append(_issue("SECTION_MISMATCH", "warn", f"行の section と章のリストが食い違う行: {_short(bad_sec)}", bad_sec))
    idset = set(ids)
    broken = [l["id"] for l in lines if l.get("parent_line_id") and l["parent_line_id"] not in idset]
    if broken:
        out.append(_issue("GROUP_PARENT_MISSING", "warn", f"サブ行の親が台本にありません: {_short(broken)}", broken))
    renamed = [l["id"] for l in lines if not l.get("parent_line_id") and (m := _SUB.match(l["id"] or "")) and m["parent"] not in idset]
    if renamed:
        out.append(_issue("SUBLINE_ORPHAN_NAME", "info",
                          f"名前は『_s』付きだが親の行が無い（独立した行として動きます）: {_short(renamed)}", renamed))
    empty = [l["id"] for l in lines if not (l.get("text") or "").strip()]
    if empty:
        out.append(_issue("EMPTY_TEXT", "error", f"本文が空の行があります: {_short(empty)}", empty))
    nl = [l["id"] for l in lines if re.search(r"[\r\n]", l.get("text") or "")]
    if nl:
        out.append(_issue("TEXT_NEWLINE", "warn", f"本文に改行が入っています（字幕・吹き出しに流れます）: {_short(nl)}", nl))
    odd = [l["id"] for l in lines if re.match(r"^[ー\-－―　 ]", l.get("text") or "") or re.search(r"[、。]{2,}|、。", l.get("text") or "")]
    if odd:
        out.append(_issue("TEXT_ODD", "info", f"先頭の記号・句読点の重なりがあります（読み上げに影響し得る）: {_short(odd)}", odd))
    long_ids = [l["id"] for l in lines if len(l.get("text") or "") > LONG_LIMIT]
    if long_ids:
        out.append(_issue("LONG_LINE", "info", f"{LONG_LIMIT}字を超える長い行があります（音声が不安定になりやすい）", long_ids))
    return out


# ── 音声 ───────────────────────────────────────────────────────────────────

def _tts_issues(view_lines: list[dict], tts_doc: dict, missing_files: list[str], services_ok: bool,
                confirm_enabled: bool) -> list[dict]:
    out: list[dict] = []
    if not services_ok:
        out.append(_issue("TTS_UNKNOWN", "info", "tts-agent に繋がらないため、音声の最新かどうかはわかりません（記録の有無だけ見ています）"))
    by = {}
    for l in view_lines:
        by.setdefault(l["tts"]["state"], []).append(l["id"])
    # 未確定の行は確定すれば音声が自動で作られる＝確定の問題として扱う（二重に数えない）
    unconf = {l["id"] for l in view_lines if l.get("confirm") == "unconfirmed"} if confirm_enabled else set()
    none = [i for i in by.get("none", []) if i not in unconf]
    stale = [i for i in by.get("stale", []) if i not in unconf]
    if none:
        out.append(_issue("TTS_NONE", "error", f"音声がまだ無い行（確定済み）: {_short(none)}", none))
    if stale:
        out.append(_issue("TTS_STALE", "error", f"音声の本文が今の台本と違う行（確定済み）: {_short(stale)}", stale))
    if by.get("unassigned"):
        out.append(_issue("TTS_UNASSIGNED", "error", "声が割り当てられていない話者の行があります（配役を決める）", by["unassigned"]))
    if by.get("queued"):
        out.append(_issue("TTS_RUNNING", "info", "音声を作成中の行があります（終わるまで待つ）", by["queued"]))
    if missing_files:
        out.append(_issue("TTS_FILE_MISSING", "error", f"tts.json にあるが wav ファイルが無い行: {_short(missing_files)}", missing_files))
    names = {l["id"]: l for l in view_lines}
    bad_name = [a["line_id"] for a in tts_doc.get("audio_files", [])
                if a.get("line_id") in names and a.get("speaker_name") in ("", None, a.get("speaker_id"))]
    if bad_name:
        out.append(_issue("TTS_SPEAKER_NAME", "warn",
                          f"tts.json の話者名が話者IDのまま／空です（字幕『名前: 本文』・編集のトラック名に出る）: {_short(bad_name)}", bad_name))
    return out


# ── 絵 ─────────────────────────────────────────────────────────────────────

def _aroll_issues(view_lines: list[dict], cast: list[dict], aroll_panels: list[dict], available: bool) -> list[dict]:
    out: list[dict] = []
    if not view_lines:
        return out
    if not view_lines[0]["aroll"].get("has_manifest"):
        return [_issue("AROLL_NOT_STARTED", "warn", "この話数のAロールはまだ始まっていません（プロンプトを作る／確定で下ごしらえ）")]
    if not available:
        out.append(_issue("AROLL_UNKNOWN", "info", "scrapping-agent に繋がらないため、絵の最新かどうかはわかりません"))
    no_panel = [l["id"] for l in view_lines if not l["aroll"].get("panel")]
    if no_panel:
        out.append(_issue("AROLL_NO_PANEL", "error", f"台本にあるのにAロールのコマが無い行: {_short(no_panel)}", no_panel))
    have = [l for l in view_lines if l["aroll"].get("panel")]
    no_slot = [l["id"] for l in have if not (l["aroll"].get("slot") or {}).get("emotion") and not l["aroll"].get("picture")]
    if no_slot:
        out.append(_issue("AROLL_NO_SLOT", "error", f"感情などの指定（slot）が無く、絵が決まっていない行: {_short(no_slot)}", no_slot))
    no_pic = [l["id"] for l in have if not l["aroll"].get("picture") and l["id"] not in set(no_slot)]
    if no_pic:
        out.append(_issue("AROLL_NO_PICTURE", "warn", f"絵が決まっていない行（在庫で埋める／新規生成）: {_short(no_pic)}", no_pic))
    no_bg = [l["id"] for l in have if not l["aroll"].get("background_id")]
    if no_bg:
        out.append(_issue("AROLL_NO_BACKGROUND", "warn", f"背景が未割当の行: {_short(no_bg)}", no_bg))
    stale_img = [l["id"] for l in have if l["aroll"].get("sync") == "stale" and l["aroll"].get("has_image")]
    if stale_img:
        out.append(_issue("AROLL_IMAGE_STALE", "warn", f"セリフが変わったのに絵が生成時のままの行: {_short(stale_img)}", stale_img))
    stale_prompt = [l["id"] for l in have if l["aroll"].get("sync") == "stale" and not l["aroll"].get("has_image")]
    if stale_prompt:
        out.append(_issue("AROLL_PROMPT_STALE", "info",
                          "演出プロンプトの元の本文が今の台本と違う行（絵は未生成なので実害は小さい。生成の直前に作り直されます）", stale_prompt))
    char_of = {s["id"]: s.get("character_id") for s in cast}
    mismatch = []
    for l in have:
        cid = char_of.get(l["speaker_id"])
        chars = [c["id"] for c in l["aroll"].get("characters") or []]
        if cid and chars and chars != [cid] and len(chars) == 1:
            mismatch.append(l["id"])
    if mismatch:
        out.append(_issue("AROLL_SPEAKER_MISMATCH", "warn", f"話者と絵のキャラが違う行: {_short(mismatch)}", mismatch))
    orphans = [p["line_id"] for p in aroll_panels if p.get("orphan")]
    if orphans:
        out.append(_issue("AROLL_ORPHANS", "info",
                          f"台本から外れた行のコマが残っています（Undo で戻る時のために保管。害は無い）: {_short(orphans)}", orphans))
    return out


def _duplicate_issues(report: dict | None) -> list[dict]:
    """同じ絵の繰り返し（Docs/AROLL_DUPLICATE_CHECK_PLAN.md）。検査の本体は scrapping-agent。
    同じ絵（exact）は warn・よく似た絵（near）だけなら info。直す前に合成すると再合成が要るので、合成の前に知らせる。"""
    if not report or not report.get("items"):
        return []
    exact = [i["line_id"] for i in report["items"] if i["kind"] == "exact"]
    near = [i["line_id"] for i in report["items"] if i["kind"] == "near"]
    out = []
    if exact:
        out.append(_issue("AROLL_DUPLICATE", "warn",
                          f"同じ絵が話数の中で繰り返し使われている行: {_short(exact)}（最初に出た側は残し、後の行を選び直す）", exact))
    if near:
        out.append(_issue("AROLL_DUPLICATE_NEAR", "info",
                          f"近い行によく似た絵が使われている行（{report.get('window')}行以内）: {_short(near)}", near))
    return out


def _stock_issues(report: dict | None) -> list[dict]:
    """この話で薄い在庫（Docs/STOCK_LABEL_ACCURACY_PLAN.md §4-7 L6）。判定の本体は scrapping-agent の `stock_health`。
    薄い＝その感情の絵（主タグの系統）で使える枚数 < この話の要求行数＝重複なしに賄えず、再使用か新規生成（課金）に落ちる。
    直すのは在庫の補充（別計画 L7）なので info（この話の工程は止めない）。行IDは付けない（感情ごとの集計）。"""
    thin = (report.get("thin") if isinstance(report, dict) else None) or []
    if not thin:
        return []
    return [_issue("AROLL_THIN_STOCK", "info",
                   "この話で在庫が薄い感情: " + "／".join(t["message"] for t in thin)
                   + "（足りない分は再使用か新規生成になる）", thin=thin)]


# ── 仕上がり（Photoshop 組版） ──────────────────────────────────────────────

def _final_issues(view_lines: list[dict], psa: dict, script_ids: set, plan_ids: list[str], qa_ids: list[str]) -> list[dict]:
    out: list[dict] = []
    if not psa.get("available"):
        return out
    if not psa.get("alive"):
        out.append(_issue("FINAL_WORKER_DOWN", "warn", "ホスト工程（Photoshop の常駐 host_worker）が止まっています。組版のジョブは積まれるだけで進みません"))
    # 絵が決まっていない行は組版の対象外（絵の層の問題）
    cand = [l for l in view_lines if l["aroll"].get("picture") or l["aroll"].get("has_image")]
    st = {}
    for l in cand:
        st.setdefault(l["final"].get("build_state"), []).append(l["id"])
    if st.get("unplanned"):
        out.append(_issue("FINAL_UNPLANNED", "warn", f"組版の配置プランが無い行: {_short(st['unplanned'])}", st["unplanned"]))
    if st.get("unbuilt"):
        out.append(_issue("FINAL_UNBUILT", "warn", f"PSD が未合成の行: {_short(st['unbuilt'])}", st["unbuilt"]))
    if st.get("restale"):
        out.append(_issue("FINAL_RESTALE", "warn", f"絵・本文が変わって合成が古い行（要・再合成）: {_short(st['restale'])}", st["restale"]))
    no_export = [l["id"] for l in cand if l["final"].get("has_psd") and not l["final"].get("export")]
    if no_export:
        out.append(_issue("FINAL_EXPORT_MISSING", "info", f"納品PNG（1920×1080）が未書き出しの行: {_short(no_export)}", no_export))
    edited = [l["id"] for l in view_lines if l["final"].get("edited")]
    if edited:
        out.append(_issue("FINAL_EDITED", "info", "Photoshop で手直し済み（✋）の行があります。再合成すると上書きされる（元のPSDは退避される）", edited))
    blocking = [l["id"] for l in view_lines if l["final"].get("severity") == "blocking"]
    if blocking:
        out.append(_issue("FINAL_BLOCKING", "info", f"合成チェックで重い指摘が残っている行: {_short(blocking)}", blocking))
    orphan = sorted({x for x in [*plan_ids, *qa_ids] if x not in script_ids})
    if orphan:
        out.append(_issue("FINAL_ORPHANS", "info", f"台本に無い行の組版プラン／検査結果が残っています（次の build_plan で整理）: {_short(orphan)}", orphan))
    return out


# ── 次にやること ───────────────────────────────────────────────────────────

def _issue_ids(issues: list[dict], *codes) -> list[str]:
    ids: list[str] = []
    for i in issues:
        if i["code"] in codes:
            ids += [x for x in i.get("line_ids", []) if x not in ids]
    return ids


def _actions(layers: dict, view_lines: list[dict]) -> list[dict]:
    acts: list[dict] = []

    def add(layer, label, tool, args=None, cost="free", confirm=False, line_ids=None, note=""):
        acts.append({"step": len(acts) + 1, "layer": layer, "label": label, "tool": tool, "args": args or {},
                     "cost": cost, "confirm": confirm, "count": len(line_ids) if line_ids is not None else None,
                     "line_ids": line_ids, "note": note})

    s = layers["script"]["issues"]
    if any(i["code"] in ("SECTION_DUP", "SECTION_MISSING", "SECTION_UNKNOWN", "SECTION_MISMATCH") for i in s):
        add("script", "章のリストを直す", None, cost="free",
            note="台本を1回編集すると scripting-agent が自動で直します。すぐ直すなら scripts/repair_episode_sections.py <ep dir> --apply（バックアップつき）")
    if any(i["code"] in ("ID_DUP", "EMPTY_TEXT", "GROUP_PARENT_MISSING") for i in s):
        add("script", "台本の構造エラーを直す（行ID重複・空の行・親の無いサブ行）", "update_script_line / delete_script_line / merge_line_with_next",
            cost="user", confirm=True, note="どう直すかは内容による。台本を変える操作は窓口（MCP）経由で")
    unconf = layers["confirm"].get("line_ids") or []
    if unconf:
        add("confirm", "手で変えた行を確定する（音声が自動で作り直され、絵の下ごしらえが走る）", "confirm_lines",
            {"line_ids": unconf}, cost="gpu", confirm=True, line_ids=unconf,
            note="確定は『人が確認した』という印。ユーザーに確認してから（無料・ローカルGPU）")
    t = layers["tts"]["issues"]
    tts_ids = _issue_ids(t, "TTS_NONE", "TTS_STALE")
    if tts_ids:
        add("tts", "音声を作る（確定済みで未生成・古い行）", "run_tts", {}, cost="gpu", line_ids=tts_ids,
            note="無料・ローカルGPU。最新の行はキャッシュで飛ばされる。1行だけならワークベンチの『未生成・要再生成を生成』")
    if any(i["code"] == "TTS_UNASSIGNED" for i in t):
        add("tts", "声の配役を決める", "assign_cast", cost="user", confirm=True)
    if any(i["code"] == "TTS_SPEAKER_NAME" for i in t):
        add("tts", "話者名を配役の名前に揃える", None, note="tts-agent の構造同期（台本の行操作・audio/sync-structure）で追随します。コード反映済みなら次の編集で直る")
    a = layers["aroll"]["issues"]
    no_panel_slot = _issue_ids(a, "AROLL_NO_PANEL", "AROLL_NO_SLOT")
    if no_panel_slot:
        add("aroll", "コマを下ごしらえする（LLMなし。サブ行は親から引き継ぎ・独立した行は感情のルール）", "aroll_prepare_lines",
            {"line_ids": no_panel_slot}, line_ids=no_panel_slot)
    no_bg = _issue_ids(a, "AROLL_NO_BACKGROUND")
    if no_bg or no_panel_slot:
        add("aroll", "未割当の背景を割り当てる", "aroll_assign_backgrounds", {"only_missing": True}, line_ids=no_bg or None,
            note="手動で選んだ背景は変えない")
    no_pic = _issue_ids(a, "AROLL_NO_PICTURE", "AROLL_NO_SLOT", "AROLL_NO_PANEL")
    if no_pic:
        add("aroll", "在庫で絵を埋める（無料）", "aroll_fill_missing", {"line_ids": no_pic}, line_ids=no_pic,
            note="埋まらない行は応答の need_generation に出る。そこだけ新規生成（課金）→ 次の項目")
        add("aroll", "在庫に無い行を新規生成する（課金・画像生成）", "run_aroll_batch", {"only_missing": True}, cost="paid",
            confirm=True, note="先に aroll_cutout_plan で枚数を見積もり、概算コストをユーザーに伝えてから。fill_missing の need_generation が空なら不要")
    dup_ids = _issue_ids(a, "AROLL_DUPLICATE", "AROLL_DUPLICATE_NEAR")
    if dup_ids:
        add("aroll", "同じ絵の繰り返しを直す（この話数で未使用の絵へ選び直す・無料。先に案を見る）", "aroll_fix_duplicates",
            {"mode": "reselect", "apply": False, "line_ids": dup_ids}, line_ids=dup_ids,
            note="案（apply=false）をユーザーに見せ、了承を得てから apply=true。選び直した行は絵の確定が外れ、仕上がりは要・再合成になる"
                 "（合成の前に直すと安い）。替えが無い行は plan の action=generate＝mode=unassign で絵を外し、"
                 "応答の changed の行だけを run_aroll_batch（課金・見積もりをユーザーへ）で生成する。"
                 "⚠️ 外した行に aroll_fill_missing は使わない（在庫が尽きた行へ同じ絵が再使用で戻る）。"
                 "人が選んだ絵・✋手直し済みは触らない。戻す時は aroll_undo_duplicate_fix")
    stale_img = _issue_ids(a, "AROLL_IMAGE_STALE")
    if stale_img:
        add("aroll", "セリフが変わった行の絵を見直す（描き直すか、そのままOKにするか）", "run_aroll_batch / aroll_approve_images",
            {"line_ids": stale_img}, cost="paid", confirm=True, line_ids=stale_img, note="人が絵を見て決める（黙って一致にしない）")
    f = layers["final"]["issues"]
    if layers["final"]["state"] != "na":
        if any(i["code"] == "FINAL_WORKER_DOWN" for i in f):
            add("final", "ホスト工程を起動する（Photoshop の常駐）", None, cost="user", confirm=True,
                note="ユーザーが scripts の start-psassist-worker を起動する。私は Photoshop を動かせない")
        plan_ids = _issue_ids(f, "FINAL_UNPLANNED")
        if plan_ids:
            add("final", "配置プランを作る", "psassist_run", {"kind": "build_plan", "lines": plan_ids}, cost="photoshop", confirm=True, line_ids=plan_ids)
        build_ids = _issue_ids(f, "FINAL_UNPLANNED", "FINAL_UNBUILT")
        if build_ids:
            add("final", "コマを合成する（PSD）", "psassist_run", {"kind": "build_panel", "lines": build_ids}, cost="photoshop", confirm=True, line_ids=build_ids,
                note="Photoshop を占有する")
        re_ids = _issue_ids(f, "FINAL_RESTALE")
        if re_ids:
            edited = {x for i in f if i["code"] == "FINAL_EDITED" for x in i.get("line_ids", [])}
            safe = [x for x in re_ids if x not in edited]
            add("final", "再合成する（絵・本文が変わった行）", "psassist_run", {"kind": "resync", "lines": safe}, cost="photoshop", confirm=True,
                line_ids=safe, note="手直し済み(✋)の行は既定で飛ばす。含める時は include_edited=true（元のPSDを退避してから上書き）"
                + (f"。手直し済みで飛ばす行: {_short(sorted(set(re_ids) & edited))}" if set(re_ids) & edited else ""))
        ex_ids = _issue_ids(f, "FINAL_EXPORT_MISSING")
        if build_ids or re_ids or ex_ids:
            add("final", "納品PNGを書き出す", "psassist_run", {"kind": "export_png", "lines": sorted(set(build_ids) | set(re_ids) | set(ex_ids))},
                cost="photoshop", confirm=True, note="export_png は lines 必須。合成・再合成のあと、直した行を明示して")
    return acts


_HEAD_IDS = 8   # 既定（compact）で issue に残す行IDの数。全部は ?full=true


def _compact(layers: dict, actions: list[dict]) -> None:
    """出力を短くする（MCP の文脈を食うため）。issue の行IDは先頭数件＋`more`、アクションは args に全IDがあるので
    `line_ids` の重複を落とす。アクションは先に全IDで作ってあるので、実行に必要な情報は減らない。"""
    for ly in layers.values():
        for i in ly.get("issues", []):
            ids = i.get("line_ids")
            if ids is not None and len(ids) > _HEAD_IDS:
                i["line_ids"] = ids[:_HEAD_IDS]
                i["more"] = len(ids) - _HEAD_IDS
        if isinstance(ly.get("line_ids"), list) and len(ly["line_ids"]) > _HEAD_IDS:
            ly["more"] = len(ly["line_ids"]) - _HEAD_IDS
            ly["line_ids"] = ly["line_ids"][:_HEAD_IDS]
    for a in actions:
        if a.get("line_ids") is not None and any(isinstance(v, list) for v in a["args"].values()):
            a["count"] = len(a["line_ids"])
            del a["line_ids"]


def _headline(view: dict, layers: dict) -> str:
    n = len(view["lines"])
    L = view["lines"]
    done_tts = sum(1 for l in L if l["tts"]["state"] == "done")
    pic = sum(1 for l in L if l["aroll"].get("picture") or l["aroll"].get("has_image"))
    built = sum(1 for l in L if l["final"].get("build_state") == "built")
    parts = [f"台本 {n}行"]
    if layers["confirm"]["state"] != "na":
        conf = sum(1 for l in L if l.get("confirm") == "confirmed")
        parts.append(f"確定 {conf}/{n}")
    parts += [f"音声 {done_tts}/{n}", f"絵 {pic}/{n}"]
    if layers["final"]["state"] != "na":
        parts.append(f"仕上がり {built}/{n}")
    return " ／ ".join(parts)


# ── 本体 ───────────────────────────────────────────────────────────────────

def compute(view: dict, raw: dict, full: bool = False) -> dict:
    """検査の本体（純粋関数）。`view`＝workbench_view.build_view の結果、`raw`＝生のファイル内容。
    raw: {script, tts_doc, aroll_panels(孤立コマを含む), plan_ids, qa_ids, missing_files}"""
    script = raw.get("script") or {}
    lines = view["lines"]
    enabled = bool(view["confirmation"]["enabled"])
    unconf = [l["id"] for l in lines if l.get("confirm") == "unconfirmed"] if enabled else []

    layers = {
        "script": _layer(_script_issues(script), total=len(lines)),
        "confirm": ({"state": "behind" if unconf else "ok", "line_ids": unconf, "total": len(lines),
                     "issues": ([_issue("UNCONFIRMED", "warn", f"未確定の行（手で変えたまま確定していない）: {_short(unconf)}", unconf)] if unconf else [])}
                    if enabled else {"state": "na", "issues": [], "note": "確定の運用はまだ始まっていません（この話数は従来どおり）"}),
        "tts": _layer(_tts_issues(lines, raw.get("tts_doc") or {}, raw.get("missing_files") or [], view["services"]["tts"], enabled)),
        "aroll": _layer(_aroll_issues(lines, view["cast"], raw.get("aroll_panels") or [], view["services"]["aroll"])
                        + _duplicate_issues(raw.get("duplicates")) + _stock_issues(raw.get("stock_health"))),
        "final": _layer(_final_issues(lines, view["psassist"], {l["id"] for l in lines}, raw.get("plan_ids") or [], raw.get("qa_ids") or [])),
    }
    if not view["services"]["tts"]:
        layers["tts"]["state"] = "unknown"
    if not view["services"]["aroll"]:
        layers["aroll"]["state"] = "unknown"
    if not view["psassist"].get("available"):
        layers["final"] = {"state": "na", "issues": [], "note": "Photoshop の組版を使わない環境です"}
    sev = {"error": 0, "warn": 0, "info": 0}
    for ly in layers.values():
        for i in ly["issues"]:
            sev[i["severity"]] += 1
    in_sync = all(ly["state"] in ("ok", "na") for ly in layers.values())
    actions = _actions(layers, lines)
    if not full:
        _compact(layers, actions)
    return {
        "project_id": view["project"]["id"], "episode": view["episode"]["number"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "in_sync": in_sync, "headline": _headline(view, layers), "severity_counts": sev,
        "layers": layers, "next_actions": actions,
    }


def _raw(project_id: str, episode: int) -> dict:
    ep_dir: Path = _ep_dir(project_id, episode)
    r = workbench_view._read
    script = r(ep_dir / "script.json") or {}
    tts_doc = r(ep_dir / "tts.json") or {}
    aroll_doc = r(ep_dir / "a_roll" / "aroll.json") or {}
    plan = r(ep_dir / "psassist" / "panel_plan.json") or {}
    qa = r(ep_dir / "psassist" / "qa_report.json") or {}
    missing = []
    for a in tts_doc.get("audio_files", []):
        fp = a.get("file_path") or ""
        if not fp:
            continue
        cands = [ep_dir / "audio" / Path(fp).name, ep_dir.parents[1] / fp, ep_dir / fp]
        if not any(c.exists() for c in cands):
            missing.append(a.get("line_id"))
    return {"script": script, "tts_doc": tts_doc, "aroll_panels": aroll_doc.get("panels", []),
            "plan_ids": [p.get("line_id") for p in plan.get("panels", [])],
            "qa_ids": [p.get("line_id") for p in qa.get("panels", [])], "missing_files": missing}


async def _stock_health(project_id: str, episode: int) -> dict | None:
    """この話の要求に対する在庫の健全性（scrapping-agent・無料・読み取り）。繋がらなければ None（audit を止めない）。"""
    try:
        res = await downstream.call("scrapping", "GET", f"/projects/{project_id}/episodes/{episode}/aroll/stock-health",
                                    timeout=60.0)
        return res.json() if res.status_code < 400 else None
    except Exception:
        return None


async def build_audit(project_id: str, episode: int, full: bool = False) -> dict:
    view = await workbench_view.build_view(project_id, episode)
    raw = _raw(project_id, episode)
    # 同じ絵の検査（無料・読み取り）。scrapping-agent に繋がらない・Aロール未着手なら飛ばす（検査の失敗で audit を止めない）
    if view["services"]["aroll"] and view["lines"] and view["lines"][0]["aroll"].get("has_manifest"):
        try:
            raw["duplicates"] = await aroll_duplicates.duplicate_report(project_id, episode, view=view)
        except Exception:
            raw["duplicates"] = None
        raw["stock_health"] = await _stock_health(project_id, episode)
    return compute(view, raw, full=full)
