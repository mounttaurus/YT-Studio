"""
MCPサーバー — Claude Code から scripting-agent をツールとして呼び出せる。
FastAPI のルーターとして /mcp パスに追加される。
"""
import json
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.core import llm_client, project_manager, script_generator, style_registry

mcp_router = APIRouter(tags=["mcp"])


class MCPRequest(BaseModel):
    tool: str
    params: dict = {}


class MCPResponse(BaseModel):
    tool: str
    result: dict


@mcp_router.post("/call", response_model=MCPResponse)
async def mcp_call(req: MCPRequest):
    handler = _TOOLS.get(req.tool)
    if not handler:
        return MCPResponse(tool=req.tool, result={"error": f"Unknown tool: {req.tool}"})
    try:
        result = await handler(**req.params)
        return MCPResponse(tool=req.tool, result=result)
    except Exception as e:
        return MCPResponse(tool=req.tool, result={"error": str(e)})


@mcp_router.get("/tools")
async def list_tools():
    return {"tools": list(_TOOLS.keys())}


# ─── ツール実装 ───────────────────────────────────────────────────────

async def scripting_generate(
    project_id: str,
    style_id: str,
    llm_model: Optional[str] = None,
    rough_script: Optional[str] = None,
) -> dict:
    """台本を生成してドラフトとして保存する。"""
    from app.api.routes import _build_input_content
    project_manager.update_project_status(project_id, "scripting", "running")
    input_content = _build_input_content(project_id, rough_script)
    script_json, warnings = await script_generator.generate(
        input_content=input_content,
        style_id=style_id,
        project_id=project_id,
        model=llm_model,
    )
    project_manager.save_draft(project_id, script_json)
    project_manager.update_project_status(project_id, "scripting", "pending")
    return {
        "project_id": project_id,
        "status": "draft_saved",
        "line_count": len(script_json["lines"]),
        "warnings": warnings,
    }


async def scripting_regenerate(
    project_id: str,
    feedback: str,
    llm_model: Optional[str] = None,
) -> dict:
    """フィードバックを元に再生成する。"""
    from app.api.routes import _build_input_content
    draft = project_manager.read_draft(project_id)
    if draft is None:
        return {"error": "ドラフトが見つかりません。先に scripting_generate を実行してください。"}

    style_id = draft["metadata"]["style"]
    model = llm_model or draft["metadata"].get("llm_model")
    input_content = _build_input_content(project_id, None)

    project_manager.update_project_status(project_id, "scripting", "running")
    script_json, warnings = await script_generator.generate(
        input_content=input_content,
        style_id=style_id,
        project_id=project_id,
        model=model,
        feedback=feedback,
    )
    project_manager.save_draft(project_id, script_json)
    project_manager.update_project_status(project_id, "scripting", "pending")
    return {
        "project_id": project_id,
        "status": "draft_saved",
        "line_count": len(script_json["lines"]),
        "warnings": warnings,
    }


async def scripting_approve(project_id: str) -> dict:
    """ドラフトを承認して script.json として確定する。"""
    script_file = project_manager.approve_script(project_id)
    return {"project_id": project_id, "status": "approved", "script_path": str(script_file)}


async def scripting_get_script(project_id: str, draft: bool = True) -> dict:
    """現在のドラフトまたは確定済みスクリプトを返す。"""
    if draft:
        data = project_manager.read_draft(project_id)
    else:
        pj_dir = project_manager.get_project_dir(project_id)
        script_file = pj_dir / "script.json"
        data = json.loads(script_file.read_text(encoding="utf-8")) if script_file.exists() else None

    if data is None:
        return {"error": "スクリプトが見つかりません"}
    return data


async def scripting_list_styles() -> dict:
    """利用可能なスタイル一覧を返す。"""
    return {"styles": style_registry.list_styles()}


async def scripting_list_projects() -> dict:
    """プロジェクト一覧を返す。"""
    return {"projects": project_manager.list_projects()}


async def scripting_list_llm_models() -> dict:
    """利用可能なLLMモデル一覧を返す。"""
    return {"models": llm_client.get_available_models(), "default": llm_client.get_default_model()}


_TOOLS = {
    "scripting_generate": scripting_generate,
    "scripting_regenerate": scripting_regenerate,
    "scripting_approve": scripting_approve,
    "scripting_get_script": scripting_get_script,
    "scripting_list_styles": scripting_list_styles,
    "scripting_list_projects": scripting_list_projects,
    "scripting_list_llm_models": scripting_list_llm_models,
}
