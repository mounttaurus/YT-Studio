"""ワークベンチ用の状態の一括取得（Docs/LINE_WORKBENCH_PLAN.md §5・W3）。

    GET /projects/{pid}/episodes/{n}/workbench   1話ぶんの行と4工程の状態（読み取り専用）
    GET /projects/{pid}/episodes/{n}/audit       整合検査: どの層まで最新か・次にやること（読み取り専用）

ロジックは `app/core/workbench_view.py`。行を変える操作は `api/lines.py`（窓口）を通る。
"""
from fastapi import APIRouter, HTTPException

from app.core import episode_audit, line_ops, workbench_view

router = APIRouter(tags=["workbench"])


@router.get("/projects/{project_id}/episodes/{episode_number}/workbench")
async def workbench(project_id: str, episode_number: int):
    try:
        return await workbench_view.build_view(project_id, episode_number)
    except line_ops.OpError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


@router.get("/projects/{project_id}/episodes/{episode_number}/audit")
async def audit(project_id: str, episode_number: int, full: bool = False):
    """台本→確定→音声→絵→仕上がりの整合検査と、次にやること（ツール名つき）。何も書かない。"""
    try:
        return await episode_audit.build_audit(project_id, episode_number, full=full)
    except line_ops.OpError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
