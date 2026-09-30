"""行の操作の窓口の HTTP 口（Docs/LINE_WORKBENCH_PLAN.md §3-1・W1）。

    POST /projects/{pid}/episodes/{n}/lines/{op}           op = edit | emotion | speaker | split | merge
                                                                | add-subline | insert | move | delete
         ?dry_run=true                                     何も変えずに「この操作で起きること」だけ返す
    POST /projects/{pid}/episodes/{n}/lines/undo           1つ前へ（台本・音声・コマが一緒に戻る）
    GET  /projects/{pid}/episodes/{n}/lines/history        Undo ボタンの件数用

  LLMの案を採用（W2・§4-1）:
    GET  /projects/{pid}/episodes/{n}/lines/proposal       ドラフトと正本の行ごとの差分（採用の前に見せる）
    POST /projects/{pid}/episodes/{n}/lines/adopt          {line_ids?, replace_all?}  採用（?dry_run=true・Undo可）

  行の確定（W2・§4）:
    GET  /projects/{pid}/episodes/{n}/lines/state          各行の確定状態＋音声の作り直し待ち
    POST /projects/{pid}/episodes/{n}/lines/confirm        {line_ids?}  行の確定（省略＝未確定すべて）
    POST /projects/{pid}/episodes/{n}/lines/confirmations/start   {baseline?}  確定の運用を始める
    POST /projects/{pid}/episodes/{n}/lines/audio-catch-up 確定済みで音声が最新でない行を作り直す（待ちを流す）

ロジックは `app/core/line_ops.py`（MCP からも同じ関数を使える）。ここは HTTP への変換だけ。
`undo`・`history` は `{op}` より先に登録する（さもないと op="undo" として拾われる）。
将来の `confirm`（W2）も同じ理由で `{op}` の前に足す。
"""
from fastapi import APIRouter, HTTPException, Query, Request

from app.core import confirm_ops, line_ops

router = APIRouter(tags=["lines"])


async def _body(request: Request) -> dict:
    """ボディ無し（merge・delete・undo）でも呼べるようにする。"""
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _http(e: line_ops.OpError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.message)


@router.get("/projects/{project_id}/episodes/{episode_number}/lines/proposal")
async def line_proposal(project_id: str, episode_number: int):
    try:
        return await line_ops.proposal(project_id, episode_number)
    except line_ops.OpError as e:
        raise _http(e)


@router.get("/projects/{project_id}/episodes/{episode_number}/lines/state")
async def lines_state(project_id: str, episode_number: int):
    try:
        return await confirm_ops.lines_state(project_id, episode_number)
    except line_ops.OpError as e:
        raise _http(e)


@router.post("/projects/{project_id}/episodes/{episode_number}/lines/confirm")
async def confirm_lines(project_id: str, episode_number: int, request: Request):
    body = await _body(request)
    line_ids = body.get("line_ids")
    if line_ids is not None and not isinstance(line_ids, list):
        raise HTTPException(status_code=400, detail="line_ids は行IDの配列（省略で未確定すべて）")
    try:
        return await confirm_ops.confirm_lines(project_id, episode_number, line_ids)
    except line_ops.OpError as e:
        raise _http(e)


@router.post("/projects/{project_id}/episodes/{episode_number}/lines/confirmations/start")
async def start_confirmations(project_id: str, episode_number: int, request: Request):
    body = await _body(request)
    try:
        return await confirm_ops.start_confirmations(
            project_id, episode_number, baseline=bool(body.get("baseline", True)))
    except line_ops.OpError as e:
        raise _http(e)


@router.post("/projects/{project_id}/episodes/{episode_number}/lines/audio-catch-up")
async def audio_catch_up(project_id: str, episode_number: int):
    try:
        return await confirm_ops.audio_catch_up(project_id, episode_number)
    except line_ops.OpError as e:
        raise _http(e)


@router.post("/projects/{project_id}/episodes/{episode_number}/lines/undo")
async def undo_line(project_id: str, episode_number: int, force: bool = Query(False)):
    try:
        return await line_ops.undo_line_op(project_id, episode_number, force=force)
    except line_ops.OpError as e:
        raise _http(e)


@router.get("/projects/{project_id}/episodes/{episode_number}/lines/history")
async def line_history(project_id: str, episode_number: int, limit: int = Query(20, ge=1, le=100)):
    try:
        return line_ops.history(project_id, episode_number, limit=limit)
    except line_ops.OpError as e:
        raise _http(e)


@router.post("/projects/{project_id}/episodes/{episode_number}/lines/{op}")
async def line_op(project_id: str, episode_number: int, op: str, request: Request,
                  dry_run: bool = Query(False)):
    try:
        return await line_ops.run_line_op(project_id, episode_number, op, await _body(request),
                                          dry_run=dry_run)
    except line_ops.OpError as e:
        raise _http(e)
