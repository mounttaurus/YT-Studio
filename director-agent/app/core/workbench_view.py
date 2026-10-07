"""ワークベンチが1話ぶんの画面を描くための「状態の一括取得」（Docs/LINE_WORKBENCH_PLAN.md §5・W3）。

`GET /projects/{pid}/episodes/{n}/workbench` が返す。台本（正本 script.json）の行ごとに、4つの工程
（台本の確定・音声・絵・仕上がり）の状態を並べる。**状態の導出はここ（サーバー側）に置く**＝
フロントは受け取った状態を描くだけ（JS 側に規則を二重に持たない・pytest で固められる）。

- 台本   … 正本 `script.json` の行。確定は `confirmations.py` の導出（運用外の話数は `confirm: null`）
- 音声   … tts-agent の `audio/pending`（要再生成・未生成・声の未割当）＋ `tts.json` の尺
          ＋ 生成の進捗（`status` の `progress`）。tts-agent に繋がらない時は tts.json の有無だけで代用
- 絵     … scrapping-agent の `aroll`（`sync` 付きのコマ一覧）。無ければ Aロール未着手
- 仕上がり … `psassist/panel_plan.json`・`qa_report.json`・`build_records.json`（読むだけ）。合成の状態（未合成／要組み直し／
            問題なし…）と「✋ 手直し済み」もここで導出する（W4b-2）

他コンテナが落ちていても画面は開く（その工程だけ `available: false` で「わからない」を出す）。
読み取り専用（何も書かない）。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from app.core import confirmations, downstream, line_ops, project_manager, psd_records
from app.core.line_ops import OpError, _ep_dir


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cast(pj: dict, chars_dir: Path) -> list[dict]:
    """配役（話者ID→名前）。名前・声の本籍はキャラ（DATA_SCHEMA §2b）。キャラ未割当の役は
    `assignable: false`（話者の選択肢には出さない＝声が紐づかない）。"""
    out = []
    for sp in ((pj.get("config") or {}).get("tts") or {}).get("speakers") or []:
        if not sp.get("id"):
            continue
        cid = sp.get("character_id") or ""
        ch = _read(chars_dir / cid / "character.json") if cid else None
        out.append({"id": sp["id"], "name": (ch or {}).get("name") or sp.get("name") or sp["id"],
                    "character_id": cid, "role": sp.get("role", ""), "assignable": bool(cid)})
    return out


async def _get(service: str, path: str, params: dict | None = None, timeout: float = 15.0):
    """他コンテナのGET。繋がらない・エラーは (None, 理由)、成功は (json, None)。404 は (None, "404")。"""
    try:
        res = await downstream.call(service, "GET", path, params=params, timeout=timeout)
    except httpx.RequestError as e:
        return None, f"{type(e).__name__}"
    if res.status_code == 404:
        return None, "404"
    if res.status_code >= 400:
        return None, f"HTTP {res.status_code}"
    return res.json(), None


async def _tts(project_id: str, episode: int) -> dict:
    pending, err = await _get("tts", f"/projects/{project_id}/audio/pending", {"episode": episode}, 10.0)
    status, _ = await _get("tts", f"/projects/{project_id}/status", {"episode": episode}, 10.0)
    prog = (status or {}).get("progress") or None
    return {
        "available": pending is not None,
        "pending": {p["line_id"]: p["state"] for p in (pending or {}).get("pending", [])},
        "unassigned": set((pending or {}).get("unassigned", [])),
        # current_line_id があるあいだが「作り直し中」（run/lines・全行生成とも、終わると None に戻る）
        "progress": None if not prog else {
            "running": bool(prog.get("current_line_id")), "current_line_id": prog.get("current_line_id"),
            "total": prog.get("total"), "done": prog.get("done"), "log": (prog.get("log") or [])[-5:]},
    }


async def _aroll(project_id: str, episode: int, ep_dir: Path) -> dict:
    data, err = await _get("scrapping", f"/projects/{project_id}/episodes/{episode}/aroll", None, 20.0)
    if data is None and err != "404":
        data = _read(ep_dir / "a_roll" / "aroll.json")     # scrapping-agent に繋がらない: sync 無しで代用
        return {"has_manifest": data is not None, "available": False, "meta": _aroll_meta(data), "cuts": {},
                "panels": {p["line_id"]: p for p in (data or {}).get("panels", []) if not p.get("orphan")}}
    cuts: dict = {}
    if data is not None:
        cr, _ = await _get("scrapping", f"/projects/{project_id}/episodes/{episode}/aroll/cuts", None, 20.0)
        for c in (cr or {}).get("cuts") or []:
            ids = c.get("line_ids") or []
            for i, lid in enumerate(ids):
                cuts[lid] = {"size": len(ids), "index": i}
    return {"has_manifest": data is not None, "available": True, "meta": _aroll_meta(data), "cuts": cuts,
            "panels": {p["line_id"]: p for p in (data or {}).get("panels", []) if not p.get("orphan")}}


def _aroll_meta(data: dict | None) -> dict:
    """話数の設定（プロンプト生成・画像生成の既定）。"""
    return {"aspect": (data or {}).get("aspect") or "16:9", "style": (data or {}).get("style") or ""}


def _plan_cutout_slot(plan_panel: dict | None) -> str | None:
    """合成プランが使った切り抜きの slot_id（生成画像そのままの旧形式は None＝判定対象外）。"""
    img = ((plan_panel or {}).get("character") or {}).get("image") or ""
    norm = img.replace("\\", "/")
    if not norm or "/cutout" not in norm:
        return None
    base = norm.rsplit("/", 1)[-1]
    return base[:-4] if base.endswith(".png") else None


def _lib_versions(chars_dir: Path, cid: str, cache: dict) -> dict:
    """在庫の {slot_id: recut_ps_at||created_at}。PS取り込みで中身だけ差し替わったことの検知に使う。"""
    if cid not in cache:
        doc = _read(chars_dir / cid / "panel_library" / "library.json") or {}
        cache[cid] = {e.get("slot_id"): e.get("edited_at") or e.get("recut_ps_at") or e.get("created_at") for e in doc.get("entries", [])}
    return cache[cid]


def _bubble_stale(p: dict | None, plan_p: dict | None) -> bool:
    """吹き出しの形が、プラン作成時点と今で食い違うか（Docs/BUBBLE_CHOICE_PLAN.md B2）。

    望む形は行（aroll.json）の `bubble_key`。プランの `bubble.key` がそれと違う、または上書きを外したのに
    プランが「ユーザー指定」のまま（自動へ戻した直後）なら要組み直し。上書きが無くプランも自動なら対象外。
    """
    if p is None or plan_p is None:
        return False
    plan_b = plan_p.get("bubble") or {}
    want = p.get("bubble_key")
    if want:
        return plan_b.get("key") != want
    return plan_b.get("key_source") == "user"


def _bubble_key(s: str | None) -> str:
    """吹き出し文字の比較用（qa_check._bubble_text_key と同じ：改行と前後の空白だけ吸収）。"""
    return (s or "").replace("\r", "").replace("\n", "").strip()


def _text_stale_now(qa_p: dict | None, live_text: str | None) -> bool:
    """PSD の吹き出し文字が、**今の台本**と食い違うか（表示のたびに計算する）。

    QA の `text_stale` は検査した瞬間の台本との比較で、台本をその後に直しても QA は走らない＝直した行が
    「問題なし」のまま残る穴があった（2026-10-04・本番1話で32行中28行が見逃されていた）。
    比べる相手は、QA が記録した PSD の実際の文字（`psd_text`）。旧い記録には無いので、`text_stale` でなかった行に限り
    検査時点の台本（`text`＝この時は PSD と一致していた）で代用する。
    """
    if not qa_p or not (live_text or "").strip():
        return False
    psd_text = qa_p.get("psd_text")
    if psd_text is None and not qa_p.get("text_stale"):
        psd_text = qa_p.get("text")
    if psd_text is None:
        return False
    return _bubble_key(psd_text) != _bubble_key(live_text)


def _build_state(p: dict | None, plan_p: dict | None, qa_p: dict | None, versions: dict,
                 live_text: str | None = None) -> str:
    """行の合成の状態（director の旧 `arollBuildState` の移し替え・判定をサーバーへ）。

    ungenerated＝絵がまだ無い／unplanned＝プラン未作成／unbuilt＝PSD 未合成／
    restale＝PSD はあるが、プラン作成時点の切り抜き・その版・吹き出しの文字・吹き出しの形が今と違う（要組み直し）／built＝最新。
    ⚠️ 旧 `qa.checked_at` と時刻を比べる方式は、1行だけ再検査すると他行の判定が狂った。ここはプランとの比較だけ。
    """
    if p is None:
        return "unknown"
    if not p.get("cutout_slot_id") and not p.get("image"):
        return "ungenerated"
    if plan_p is None:
        return "unplanned"
    if qa_p is None:
        return "unbuilt"
    slot = p.get("cutout_slot_id")
    if slot:
        if _plan_cutout_slot(plan_p) != slot:
            return "restale"
        # slot_id が同じでも、PS取り込みで中身だけ差し替わっていることがある（プラン作成時点の版と比べる）
        pv = (plan_p.get("character") or {}).get("cutout_version")
        cv = versions.get(slot)
        if pv and cv and pv != cv:
            return "restale"
    if qa_p.get("text_stale") or _text_stale_now(qa_p, live_text):   # 絵は同じでも、台本の文面が変わって吹き出しの文字が古い
        return "restale"
    if _bubble_stale(p, plan_p):        # 吹き出しの形を選び直した（上書きを足した・変えた・外した）
        return "restale"
    return "built"


def _aroll_view(a: dict, lid: str, ctx: dict) -> dict:
    """1行の絵の状態。`picture`/`sync` などの一覧用の印に加えて、絵タブの中身列・行モーダルが要る値
    （縮小画像URL・映すキャラ・使われている絵のタグ・プロンプト・絵の確定・旧セリフ・カットのつながり）を返す。"""
    p = a["panels"].get(lid)
    if not a["has_manifest"]:
        return {"has_manifest": False}
    if p is None:
        return {"has_manifest": True, "panel": False}
    pic = "stock" if p.get("cutout_slot_id") else ("generated" if p.get("status") == "done" and p.get("image") else None)
    used = p.get("used_slot") or p.get("slot") or {}
    chars = p.get("characters") or []
    cid = p.get("cutout_char_id") or (chars[0] if chars else "")
    thumb = ""
    if p.get("cutout_slot_id") and cid:
        thumb = f"/api/scrapping/characters/{cid}/panel_library/cutout/{p['cutout_slot_id']}.png"
    elif p.get("image"):
        thumb = (f"/api/scrapping/projects/{ctx['pid']}/episodes/{ctx['ep']}/aroll/image/{p['image']}")
    return {"has_manifest": True, "panel": True, "picture": pic, "sync": p.get("sync"),
            "emotion": used.get("emotion"), "background_id": p.get("background_id"),
            "speaker_changed": bool(p.get("speaker_changed")), "sync_known": a["available"],
            "status": p.get("status") or "pending", "has_image": bool(p.get("image")),
            "thumb": thumb, "has_prompt": bool((p.get("prompt") or "").strip()), "prompt": p.get("prompt") or "",
            "characters": [{"id": c, "name": ctx["char_name"](c)} for c in chars],
            "cutout_slot_id": p.get("cutout_slot_id") or None, "cutout_char_id": cid or None,
            "slot": p.get("slot") or {}, "slot_source": p.get("slot_source"), "matched": bool(p.get("slot_key")),
            "used_slot": p.get("used_slot") or None,
            "approved": bool(p.get("image_approved_at")), "source_text": p.get("source_text") or "",
            # 吹き出しの形の上書き（無ければ自動）と、プランが今の上書きに追いついていない印
            "bubble_key": p.get("bubble_key") or None,
            "bubble_stale": _bubble_stale(p, ctx["plan"].get(lid)),
            "cut": a["cuts"].get(lid),
            # 選び直した絵・台本の文面がまだ合成に反映されていない印（判定は `_build_state`）
            "restale": ctx["build_state"](lid) == "restale"}


def _tts_view(t: dict, entries: dict, lid: str) -> dict:
    e = entries.get(lid) or {}
    if t["available"]:
        if lid in t["unassigned"]:
            state = "unassigned"
        elif lid in t["pending"]:
            state = "stale" if t["pending"][lid] == "stale" else "none"
        else:
            state = "done" if e else "none"
    else:
        state = "done" if e else "none"                     # わからない: 記録の有無だけ
    running = (t["progress"] or {}).get("current_line_id") == lid
    return {"state": "queued" if running else state, "duration_sec": e.get("duration_sec"),
            "known": t["available"]}


def _psassist_meta(qa_doc: dict, plan: dict, worker: dict | None) -> dict:
    """ホスト工程（Photoshop の常駐）の状態。`available=false`（worker.json が一度も無い）は使わない環境＝画面ごと隠す。"""
    return {"available": worker is not None, "alive": bool((worker or {}).get("alive")),
            "capabilities": (worker or {}).get("capabilities") or [],
            "has_plan": bool(plan), "has_qa": bool(qa_doc),
            "checked_at": qa_doc.get("checked_at"), "episode_dir": qa_doc.get("episode_dir"),
            "summary": qa_doc.get("summary") or {}}


def _final_view(plan_p: dict | None, qa_p: dict | None, state: str, psd: dict, *, pid: str, ep: int,
                bubble_stale: bool = False, live_text: str | None = None) -> dict:
    """1行の仕上がり。プランの印（`status`・`warnings`・`bubble`）に、合成チェックの結果（重さ・指摘・画像）と
    ✋手直し済みを足す。検査していない行は QA の項目を持たない。"""
    out = {"status": (plan_p or {}).get("status"), "warnings": (plan_p or {}).get("warnings") or [],
           "bubble": ((plan_p or {}).get("bubble") or {}).get("kind")} if plan_p else {"status": None}
    base = f"/projects/{pid}/episodes/{ep}/psassist/file/"
    if qa_p:
        # QA 実行後に台本が変わった行は、QA の記録が「問題なし」のままでも指摘を足して見える化する
        stale_now = _text_stale_now(qa_p, live_text)
        issues = list(qa_p.get("issues") or [])
        severity = qa_p.get("severity")
        if stale_now and not any(i.get("code") == "TEXT_STALE" for i in issues):
            issues.append({"code": "TEXT_STALE", "severity": "advisory",
                           "label": "セリフが変わりました（吹き出しの文字が古いままです）"})
            if severity == "clean":
                severity = "advisory"
        out.update({
            "severity": severity, "issues": issues, "measured": qa_p.get("measured") or {},
            "text_stale": bool(qa_p.get("text_stale")) or stale_now, "psd": qa_p.get("psd"),
            "thumb": base + qa_p["thumb"] if qa_p.get("thumb") else None,
            "view": base + qa_p["view"] if qa_p.get("view") else None,
            "export": base + qa_p["export"] if qa_p.get("export") else None})
    # プランが選んだ形（key）とその根拠（key_source: speaker_default / question / exclaim / user）。UIの「今の形」用
    pb = (plan_p or {}).get("bubble") or {}
    out.update({"bubble_key": pb.get("key"), "bubble_source": pb.get("key_source"), "bubble_stale": bubble_stale})
    out.update({"build_state": state, "has_psd": psd["has_psd"], "edited": psd["edited"], "built_at": psd["built_at"]})
    return out


async def build_view(project_id: str, episode: int) -> dict:
    ep_dir = _ep_dir(project_id, episode)
    pj_dir = ep_dir.parents[1]
    pj = _read(pj_dir / "project.json") or {}
    cast = _cast(pj, project_manager.PROJECTS_DIR.parent / "characters")
    names = {c["id"]: c["name"] for c in cast}

    script = _read(ep_dir / "script.json")
    lines = list((script or {}).get("lines", []))
    conf_doc = None
    try:
        conf_doc = confirmations.read(ep_dir)
    except ValueError as e:
        raise OpError(500, str(e))
    states = confirmations.line_states(lines, conf_doc) if conf_doc is not None else {}

    tts_doc = _read(ep_dir / "tts.json") or {}
    entries = {f.get("line_id"): f for f in tts_doc.get("audio_files", [])}
    tts = await _tts(project_id, episode)
    aroll = await _aroll(project_id, episode, ep_dir)
    plan = {p.get("line_id"): p for p in (_read(ep_dir / "psassist" / "panel_plan.json") or {}).get("panels", [])}
    chars_dir = project_manager.PROJECTS_DIR.parent / "characters"
    name_cache: dict[str, str] = {}

    def char_name(cid: str) -> str:
        if cid not in name_cache:
            name_cache[cid] = (_read(chars_dir / cid / "character.json") or {}).get("name") or cid
        return name_cache[cid]
    psa_dir = ep_dir / "psassist"
    qa_doc = _read(psa_dir / "qa_report.json") or {}
    qa = {p.get("line_id"): p for p in qa_doc.get("panels", [])}
    records = psd_records.load_records(psa_dir)
    ver_cache: dict = {}

    live_text = {l.get("id"): l.get("text") or "" for l in lines}

    def build_state(lid: str) -> str:
        p = aroll["panels"].get(lid)
        cid = (p or {}).get("cutout_char_id") or (((p or {}).get("characters") or [None])[0])
        return _build_state(p, plan.get(lid), qa.get(lid), _lib_versions(chars_dir, cid, ver_cache) if cid else {},
                            live_text.get(lid))
    actx = {"pid": project_id, "ep": episode, "plan": plan, "char_name": char_name, "build_state": build_state}

    ep_meta = next((e for e in pj.get("episodes", []) if e.get("number") == episode), {})
    out_lines = []
    for l in lines:
        lid = l.get("id")
        pl = plan.get(lid)
        out_lines.append({
            "id": lid, "order": l.get("order"), "parent_line_id": l.get("parent_line_id"),
            "speaker_id": l.get("speaker_id") or "",
            "speaker_name": names.get(l.get("speaker_id")) or l.get("speaker_name") or "",
            "section": l.get("section"), "text": l.get("text") or "", "emotion": l.get("emotion") or "neutral",
            "speed": l.get("speed"), "pause_after_sec": l.get("pause_after_sec"),
            "split_review": bool(l.get("split_review")),
            "confirm": states.get(lid) if conf_doc is not None else None,
            "tts": _tts_view(tts, entries, lid),
            "aroll": _aroll_view(aroll, lid, actx),
            "final": _final_view(pl, qa.get(lid), build_state(lid), psd_records.psd_state(psa_dir, records, lid), pid=project_id, ep=episode,
                                bubble_stale=_bubble_stale(aroll["panels"].get(lid), pl), live_text=live_text.get(lid)),
        })

    return {
        "project": {"id": pj.get("id", project_id), "title": pj.get("title", project_id)},
        "episode": {"number": episode, "title": ep_meta.get("title", f"第{episode}話"), "status": ep_meta.get("status", {})},
        "has_script": script is not None,
        "cast": cast,
        "confirmation": {"enabled": conf_doc is not None,
                         "baseline": bool((conf_doc or {}).get("baseline")),
                         "started_at": (conf_doc or {}).get("started_at")},
        "proposal_pending": project_manager.proposal_pending(ep_dir),
        "undoable": line_ops.history(project_id, episode)["undoable"],
        "services": {"tts": tts["available"], "aroll": aroll["available"]},
        "aroll_meta": aroll["meta"],
        "psassist": _psassist_meta(qa_doc, plan, project_manager.get_psassist_worker()),
        "tts_progress": tts["progress"],
        "lines": out_lines,
    }
