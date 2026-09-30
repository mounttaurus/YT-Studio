"""行の確定の窓口（Docs/LINE_WORKBENCH_PLAN.md §4・W2）。

確定 ＝ 指紋を記録する（**確定点**）→ 派生物の確定時の後処理（音声の作り直し・絵の下ごしらえ）。
後処理が失敗しても確定は取り消さず `warnings` に載せる（I3 と同じ）。画像の新規生成（課金）は
確定では走らない（I6）。音声はローカルGPUなので例外として自動で作り直す（D9）。

保存は `confirmations.py`（本籍）。ここは HTTP を知らない（`api/lines.py` が変換する）。
台本の正本は script.json。ドラフトだけにある変更は確定の対象ではない（採用してから確定する）。
"""
from __future__ import annotations

import httpx

from app.core import confirmations, derivatives, downstream, project_manager
from app.core.derivatives import ConfirmContext
from app.core.line_ops import OpError, _ep_dir, _lock

NOT_STARTED = ("確定の運用がこの話数ではまだ始まっていません。"
               "POST .../lines/confirmations/start で始める（今の台本を全行確定済みとして記録）か、"
               "『LLMの案を採用』で始めます")


def _script_lines_or_404(ep_dir, episode: int) -> list[dict]:
    lines = confirmations.script_lines(ep_dir)
    if lines is None:
        raise OpError(404, f"第{episode}話の正本（script.json）がありません。先に『LLMの案を採用』してください")
    return lines


def _doc(ep_dir) -> dict:
    try:
        doc = confirmations.read(ep_dir)
    except ValueError as e:
        raise OpError(500, str(e))
    if doc is None:
        raise OpError(409, NOT_STARTED)
    return doc


async def lines_state(project_id: str, episode: int) -> dict:
    """各行の確定状態と、音声の作り直し待ち（tts-agent に聞く。繋がらなければ null）。"""
    ep_dir = _ep_dir(project_id, episode)
    lines = confirmations.script_lines(ep_dir)
    if lines is None:
        return {"enabled": confirmations.enabled(ep_dir), "has_script": False}
    if not confirmations.enabled(ep_dir):
        return {"enabled": False, "has_script": True, "total": len(lines)}
    states = confirmations.line_states(lines, _doc(ep_dir))
    pending = None
    try:
        res = await downstream.call("tts", "GET", f"/projects/{project_id}/audio/pending",
                                    params={"episode": episode}, timeout=10.0)
        if res.status_code < 400:
            pending = [p["line_id"] for p in res.json().get("pending", [])]
    except httpx.RequestError:
        pass
    confirmed = [l for l, s in states.items() if s == confirmations.CONFIRMED]
    return {
        "enabled": True, "has_script": True,
        "started_at": _doc(ep_dir).get("started_at"), "baseline": bool(_doc(ep_dir).get("baseline")),
        "total": sum(1 for s in states.values() if s != confirmations.EMPTY),
        "confirmed_line_ids": confirmed,
        "unconfirmed_line_ids": [l for l, s in states.items() if s == confirmations.UNCONFIRMED],
        "empty_line_ids": [l for l, s in states.items() if s == confirmations.EMPTY],
        # 確定済みなのに音声が最新でない行＝作り直し待ち（エンジン停止・失敗）
        "audio_pending_line_ids": None if pending is None else [l for l in confirmed if l in set(pending)],
    }


async def start_confirmations(project_id: str, episode: int, baseline: bool = True) -> dict:
    """確定の運用を始める。既に始まっていれば何もしない。baseline=True は今の台本を全行確定済みとして記録。"""
    ep_dir = _ep_dir(project_id, episode)
    async with _lock(project_id, episode):
        lines = confirmations.script_lines(ep_dir)
        if lines is None:
            raise OpError(404, f"第{episode}話の正本（script.json）がありません")
        res = confirmations.start(ep_dir, lines, baseline=baseline)
        _audit(project_id, episode, "confirmations-start", res)
        return {**res, **confirmations.state(ep_dir, lines)}


async def confirm_lines(project_id: str, episode: int, line_ids: list[str] | None = None) -> dict:
    """行を確定する。line_ids 省略＝今の未確定すべて。確定済みの行を明示した時は記録は触らず、
    後処理（音声の待ち・絵の下ごしらえ）だけをもう一度流す（作り直し待ちの再実行）。"""
    ep_dir = _ep_dir(project_id, episode)
    # ロックを握るのは確定点（指紋の記録）だけ。後処理はLLMでのプロンプト生成など数分かかりうるので、
    # ロックの外で流す（さもないと、その間ほかの行の操作・Undo が待たされる）
    async with _lock(project_id, episode):
        lines = _script_lines_or_404(ep_dir, episode)
        _doc(ep_dir)
        try:
            res = confirmations.confirm(ep_dir, lines, line_ids)     # ← 確定点
        except KeyError:
            raise OpError(409, NOT_STARTED)
        if res["unknown"]:
            raise OpError(404, f"台本（script.json）に無い行です: {', '.join(res['unknown'])}")

    targets = set(res["confirmed"]) | set(res["already"])
    ordered = [l["id"] for l in lines if l["id"] in targets]          # 台本の並び順
    applied, warnings = {}, []
    if ordered:
        applied, warnings = await derivatives.confirm_all(ConfirmContext(
            project_id=project_id, episode=episode, line_ids=ordered, script_lines=lines, ep_dir=ep_dir))
    _audit(project_id, episode, "confirm", {**res, "warnings": warnings})
    return {"ok": True, **res, "applied": applied, "warnings": warnings, **confirmations.state(ep_dir)}


async def audio_catch_up(project_id: str, episode: int) -> dict:
    """確定済みの行のうち、音声が最新でない行（エンジン停止で作れなかった等）を作り直させる。
    エンジン（GPUプロファイル）を起動した後に「待ちを流す」ための口。最新の行は作り直さない。"""
    ep_dir = _ep_dir(project_id, episode)
    async with _lock(project_id, episode):
        lines = _script_lines_or_404(ep_dir, episode)
        confirmed = confirmations.confirmed_ids(lines, _doc(ep_dir))
    if not confirmed:
        return {"ok": True, "applied": {}, "warnings": [], "queued": []}
    applied, warnings = await derivatives.confirm_all(
        ConfirmContext(project_id=project_id, episode=episode, line_ids=confirmed,
                       script_lines=lines, ep_dir=ep_dir), only="tts")
    _audit(project_id, episode, "audio-catch-up", {"warnings": warnings})
    return {"ok": True, "applied": applied, "warnings": warnings,
            "queued": (applied.get("tts") or {}).get("queued", [])}


def _audit(project_id: str, episode: int, action: str, detail: dict) -> None:
    try:
        project_manager.append_director_log(project_id, {
            "action": action, "episode": episode,
            **{k: v for k, v in detail.items() if k in (
                "confirmed", "already", "skipped_empty", "started", "baseline", "recorded", "warnings")},
        })
    except Exception:  # noqa: BLE001
        pass
