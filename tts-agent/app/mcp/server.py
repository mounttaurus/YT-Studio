"""
MCP サーバー — Claude Code から tts-agent をツールとして呼び出せる。
FastAPI のルーターとして /mcp パスに追加される。
"""
import json
import shutil
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from app.core import cache_manager
from app.core.engines import irodori
from app.core.emotion_mapper import apply_emotion_to_text
from app.core.project_manager import get_project_dir, list_projects, read_project
from app.core.script_parser import parse_script_json

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


# ─── ツール実装 ────────────────────────────────────────────────────

async def tts_generate_project(project_id: str) -> dict:
    """プロジェクトの script.json から全音声を生成する"""
    import httpx
    async with httpx.AsyncClient(timeout=600) as client:
        resp = await client.post(f"http://localhost:8004/projects/{project_id}/run")
        return resp.json()


async def tts_preview(text: str, voice: str = "", emotion: str = "neutral") -> dict:
    """テキストをプレビュー生成し、キャッシュファイルパスを返す"""
    processed = apply_emotion_to_text(text, emotion)
    audio_bytes = await irodori.generate(processed, voice)
    key = cache_manager.get_cache_key(processed, voice, "irodori")
    path = cache_manager.save_to_cache(key, audio_bytes, processed, voice, "irodori")
    return {"file_path": str(path), "processed_text": processed}


async def tts_list_voices() -> dict:
    """利用可能な参照音声の一覧を返す"""
    voices_dir = Path("/app/voices")
    files = [f.name for f in voices_dir.iterdir() if f.suffix in (".wav", ".mp3", ".flac")] if voices_dir.exists() else []
    engine_voices = await irodori.list_voices()
    return {"local_voices": files, "engine_voices": engine_voices}


async def tts_regenerate_line(project_id: str, line_id: str) -> dict:
    """特定のセリフを再生成する"""
    import httpx
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"http://localhost:8004/projects/{project_id}/run/line/{line_id}")
        return resp.json()


async def tts_get_status(project_id: str) -> dict:
    """プロジェクトの TTS 進捗を返す"""
    pj = read_project(project_id)
    project_dir = get_project_dir(project_id)
    audio_dir = project_dir / "audio"
    wav_files = list(audio_dir.glob("*.wav")) if audio_dir.exists() else []
    return {
        "project_id": project_id,
        "tts_status": pj["status"].get("tts"),
        "generated_files": len(wav_files),
        "errors": pj.get("errors", []),
    }


async def tts_list_projects() -> dict:
    """プロジェクト一覧を返す"""
    return {"projects": list_projects()}


_TOOLS = {
    "tts_generate_project": tts_generate_project,
    "tts_preview": tts_preview,
    "tts_list_voices": tts_list_voices,
    "tts_regenerate_line": tts_regenerate_line,
    "tts_get_status": tts_get_status,
    "tts_list_projects": tts_list_projects,
}
