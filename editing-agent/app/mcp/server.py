"""
MCPサーバー — Claude Code から editing-agent をツールとして呼び出せる。
FastAPI のルーターとして /mcp パスに追加される。
"""
from fastapi import APIRouter
from pydantic import BaseModel

from app.core import edit_runner, project_manager

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

async def edit_generate(
    project_id: str,
    episode: int,
    fps: int = 30,
    path_style: str = "file_uri",
    speaker_prefix: bool = False,
    force: bool = False,
    lang: str | None = None,
) -> dict:
    """script/tts/footageを統合し、DaVinci Resolve用のラフ編集データ（timeline.otio・subtitles.srt・edit.json）を生成する。
    lang指定時は原語ではなく翻訳言語（locales/{lang}/）向けに生成する。"""
    ep_dir = project_manager.episode_dir(project_id, episode)
    if ep_dir is None:
        return {"error": f"episode not found: {project_id} ep{episode}"}

    tts = project_manager.get_episode_tts(project_id, episode, lang=lang)
    footage = project_manager.get_episode_footage(project_id, episode)
    if tts is None or footage is None:
        return {"error": "tts.json or footage.json not found"}

    tts_status = project_manager.get_episode_status(project_id, episode, lang=lang)
    footage_status = project_manager.get_episode_status(project_id, episode)
    if not force and (tts_status.get("tts") != "done" or footage_status.get("footage") != "done"):
        return {
            "error": f"prerequisite not done: tts={tts_status.get('tts')}, footage={footage_status.get('footage')} "
                     f"(force=true で上書き実行可能)",
        }

    if path_style not in ("file_uri", "windows"):
        return {"error": "path_style must be 'file_uri' or 'windows'"}

    edit_json = edit_runner.run_edit(
        project_id, episode, fps=fps, path_style=path_style, speaker_prefix=speaker_prefix, lang=lang,
    )
    return edit_json


_TOOLS = {
    "edit_generate": edit_generate,
}
