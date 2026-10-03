"""ワークベンチ用の状態の一括取得（Docs/LINE_WORKBENCH_PLAN.md §5・W3）。

    GET /projects/{pid}/episodes/{n}/workbench   1話ぶんの行と4工程の状態（読み取り専用）
    GET /projects/{pid}/episodes/{n}/audit       整合検査: どの層まで最新か・次にやること（読み取り専用）
    GET /projects/{pid}/episodes/{n}/aroll-duplicates   同じ絵の繰り返しの検査（読み取り専用・✋の行は対象外）
    POST …/aroll-duplicates/fix   検査の指摘を直す（既定は案だけ・apply=true で書く・生成はしない）
    POST …/aroll-duplicates/undo  直前の「重複を直した」を戻す

ロジックは `app/core/workbench_view.py`。行を変える操作は `api/lines.py`（窓口）を通る。
"""
from fastapi import APIRouter, HTTPException, Request

from app.core import aroll_duplicates as aroll_duplicates_core
from app.core import episode_audit, line_ops, workbench_view

router = APIRouter(tags=["workbench"])


@router.get("/projects/{project_id}/episodes/{episode_number}/workbench")
async def workbench(project_id: str, episode_number: int):
    try:
        return await workbench_view.build_view(project_id, episode_number)
    except line_ops.OpError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.get("/projects/{project_id}/episodes/{episode_number}/aroll-duplicates")
async def aroll_duplicates(project_id: str, episode_number: int, window: int = aroll_duplicates_core.DEFAULT_WINDOW):
    """同じ絵・よく似た絵の繰り返しの検査（読み取りのみ・無料）。✋手直し済みの行は対象外として扱う。
    Docs/AROLL_DUPLICATE_CHECK_PLAN.md。"""
    try:
        return await aroll_duplicates_core.duplicate_report(project_id, episode_number, window=window)
    except line_ops.OpError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.post("/projects/{project_id}/episodes/{episode_number}/aroll-duplicates/fix")
async def fix_aroll_duplicates(project_id: str, episode_number: int, request: Request):
    """検査の指摘を直す。既定は案だけ（`apply=true` で書く・無料・生成はしない）。✋手直し済みの行は守る。
    body: {mode: "reselect"|"unassign", apply?, line_ids?, window?}。Docs/AROLL_DUPLICATE_CHECK_PLAN.md D2。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    try:
        return await aroll_duplicates_core.fix_duplicates(
            project_id, episode_number, mode=body.get("mode", "reselect"), apply=bool(body.get("apply", False)),
            line_ids=body.get("line_ids"), window=int(body.get("window", aroll_duplicates_core.DEFAULT_WINDOW)))
    except line_ops.OpError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.post("/projects/{project_id}/episodes/{episode_number}/aroll-duplicates/undo")
async def undo_aroll_duplicate_fix(project_id: str, episode_number: int, request: Request):
    """直前の「重複を直した」を戻す（絵の割当と確定）。body: {fix_id?}。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    fix_id = body.get("fix_id") if isinstance(body, dict) else None
    try:
        return await aroll_duplicates_core.undo_duplicate_fix(project_id, episode_number, fix_id)
    except line_ops.OpError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.get("/projects/{project_id}/episodes/{episode_number}/audit")
async def audit(project_id: str, episode_number: int, full: bool = False):
    """台本→確定→音声→絵→仕上がりの整合検査と、次にやること（ツール名つき）。何も書かない。"""
    try:
        return await episode_audit.build_audit(project_id, episode_number, full=full)
    except line_ops.OpError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
