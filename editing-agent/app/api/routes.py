import os
from typing import Optional

import opentimelineio as otio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core import edit_runner, project_manager

router = APIRouter(tags=["api"])

DEFAULT_FPS = int(os.getenv("DEFAULT_FPS", "30"))
DEFAULT_PATH_STYLE = os.getenv("OTIO_PATH_STYLE", "file_uri")


class EditRunRequest(BaseModel):
    fps: int = DEFAULT_FPS
    speaker_prefix: bool = False
    path_style: str = DEFAULT_PATH_STYLE
    force: bool = False
    subtitle_format: str = "both"  # srt | fcpxml | both
    lang: Optional[str] = None  # 省略=原語。指定時は locales/{lang}/tts.json を使い locales/{lang}/edit/ へ出力


class SubtitleStyleRequest(BaseModel):
    style: dict


@router.get("/health")
def health():
    host_shared_dir = os.getenv("HOST_SHARED_DIR", "")
    return {
        "status": "ok",
        "otio_version": otio.__version__,
        "host_shared_dir": host_shared_dir,
        "configured": bool(host_shared_dir),
    }


@router.get("/projects")
def list_projects():
    return {"projects": project_manager.list_projects()}


@router.get("/projects/{project_id}/episodes")
def get_project_episodes(project_id: str):
    episodes = project_manager.get_project_episodes(project_id)
    if not episodes:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    return {"episodes": episodes}


@router.post("/projects/{project_id}/episodes/{episode_number}/edit/run")
def run_edit(project_id: str, episode_number: int, req: EditRunRequest):
    ep_dir = project_manager.episode_dir(project_id, episode_number)
    if ep_dir is None:
        raise HTTPException(status_code=404, detail=f"episode not found: {project_id} ep{episode_number}")

    tts = project_manager.get_episode_tts(project_id, episode_number, lang=req.lang)
    footage = project_manager.get_episode_footage(project_id, episode_number)
    if tts is None or footage is None:
        raise HTTPException(status_code=422, detail="tts.json or footage.json not found")
    if "schema_version" not in tts or "schema_version" not in footage:
        raise HTTPException(status_code=422, detail="tts.json or footage.json missing schema_version")

    # tts の完了判定は対象言語（lang）で見るが、footage は言語別に持たない（原語と共有）ため常に原語で見る。
    tts_status = project_manager.get_episode_status(project_id, episode_number, lang=req.lang)
    footage_status = project_manager.get_episode_status(project_id, episode_number)
    if not req.force and (tts_status.get("tts") != "done" or footage_status.get("footage") != "done"):
        raise HTTPException(
            status_code=409,
            detail=f"prerequisite not done: tts={tts_status.get('tts')}, footage={footage_status.get('footage')} (use force=true to override)",
        )

    if req.path_style not in ("file_uri", "windows"):
        raise HTTPException(status_code=422, detail="path_style must be 'file_uri' or 'windows'")
    if req.subtitle_format not in ("srt", "fcpxml", "both"):
        raise HTTPException(status_code=422, detail="subtitle_format must be 'srt', 'fcpxml' or 'both'")

    try:
        edit_json = edit_runner.run_edit(
            project_id, episode_number,
            fps=req.fps, path_style=req.path_style, speaker_prefix=req.speaker_prefix,
            subtitle_format=req.subtitle_format, lang=req.lang,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"edit generation failed: {e}")

    return {"ok": True, "edit": edit_json}


@router.get("/projects/{project_id}/episodes/{episode_number}/edit/result")
def get_edit_result(project_id: str, episode_number: int, lang: Optional[str] = None):
    result = project_manager.read_edit_result(project_id, episode_number, lang=lang)
    if result is None:
        raise HTTPException(status_code=404, detail="edit.json not found")
    return result


@router.get("/projects/{project_id}/episodes/{episode_number}/speakers")
def get_episode_speakers(project_id: str, episode_number: int):
    return {"speakers": project_manager.get_episode_speakers(project_id, episode_number)}


@router.get("/projects/{project_id}/subtitle-style")
def get_subtitle_style(project_id: str):
    if project_manager.find_project_dir(project_id) is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    return {"style": project_manager.get_subtitle_style(project_id)}


@router.put("/projects/{project_id}/subtitle-style")
def put_subtitle_style(project_id: str, req: SubtitleStyleRequest):
    if not project_manager.set_subtitle_style(project_id, req.style):
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    return {"ok": True, "style": req.style}
