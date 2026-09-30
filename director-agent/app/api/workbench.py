"""ワークベンチ用の状態の一括取得（Docs/LINE_WORKBENCH_PLAN.md §5・W3）。

    GET /projects/{pid}/episodes/{n}/workbench   1話ぶんの行と4工程の状態（読み取り専用）

ロジックは `app/core/workbench_view.py`。行を変える操作は `api/lines.py`（窓口）を通る。
"""
from fastapi import APIRouter, HTTPException

from app.core import line_ops, workbench_view

router = APIRouter(tags=["workbench"])


@router.get("/projects/{project_id}/episodes/{episode_number}/workbench")
async def workbench(project_id: str, episode_number: int):
    try:
        return await workbench_view.build_view(project_id, episode_number)
    except line_ops.OpError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
