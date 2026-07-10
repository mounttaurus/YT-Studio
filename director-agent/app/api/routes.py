import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.core import project_manager

router = APIRouter(tags=["api"])

TTS_AGENT_URL = os.getenv("TTS_AGENT_URL", "http://tts-agent:8004")
RESEARCH_AGENT_URL = os.getenv("RESEARCH_AGENT_URL", "http://research-agent:8001")
SCRIPTING_AGENT_URL = os.getenv("SCRIPTING_AGENT_URL", "http://scripting-agent:8002")
SCRAPPING_AGENT_URL = os.getenv("SCRAPPING_AGENT_URL", "http://scrapping-agent:8003")
EDITING_AGENT_URL = os.getenv("EDITING_AGENT_URL", "http://editing-agent:8006")

PROJECT_PATH_RE = re.compile(r"^projects/([^/]+)")


@router.get("/health")
async def health():
    return {"status": "ok", "service": "director-agent"}


@router.get("/projects")
async def get_projects():
    return {"projects": project_manager.list_projects()}


@router.get("/projects/{project_id}/episodes")
async def get_project_episodes(project_id: str):
    episodes = project_manager.get_project_episodes(project_id)
    if not episodes:
        raise HTTPException(status_code=404, detail="project or episodes not found")
    return {"episodes": episodes}


@router.get("/projects/{project_id}/episodes/{episode_number}/script")
async def get_episode_script(project_id: str, episode_number: int):
    script = project_manager.get_episode_script(project_id, episode_number)
    if script is None:
        raise HTTPException(status_code=404, detail="script not found")
    return script


@router.get("/projects/{project_id}/episodes/{episode_number}/tts")
async def get_episode_tts(project_id: str, episode_number: int, lang: str | None = None):
    """エピソードのtts.json（生成済み音声一覧）を読み取り専用で返す。"""
    data = project_manager.get_episode_tts(project_id, episode_number, lang=lang)
    if data is None:
        raise HTTPException(status_code=404, detail="tts.json not found")
    return data


# ─── TTS連携 ──────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/episodes/{episode_number}/tts/run")
async def run_tts(project_id: str, episode_number: int):
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.post(
                f"{TTS_AGENT_URL}/projects/{project_id}/run",
                params={"episode": episode_number},
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"tts-agent unreachable: {e}")
    if res.status_code >= 400:
        raise HTTPException(status_code=res.status_code, detail=res.text)
    return res.json()


@router.get("/projects/{project_id}/tts/status")
async def get_tts_status(project_id: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.get(f"{TTS_AGENT_URL}/projects/{project_id}/status")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"tts-agent unreachable: {e}")
    if res.status_code >= 400:
        raise HTTPException(status_code=res.status_code, detail=res.text)
    return res.json()


# ─── research-agent連携（汎用プロキシ） ─────────────────────────────────
#
# 持ち込み素材→ラフ台本ダイジェスト（research-agentの/sources・/digest等）を
# director-agentから操作するため、REST APIをそのまま中継する。
# 蒸留(LLM)・URL取得・検索は時間がかかるためタイムアウトは長め（300秒）。

@router.api_route("/api/research/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_research(path: str, request: Request):
    url = f"{RESEARCH_AGENT_URL}/{path}"
    body = await request.body()
    headers = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]

    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            res = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=body,
                headers=headers,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"research-agent unreachable: {e}")

    if request.method != "GET":
        m = PROJECT_PATH_RE.match(path)
        if m:
            project_manager.append_director_log(m.group(1), {
                "target": "research-agent",
                "method": request.method,
                "path": path,
                "status_code": res.status_code,
            })

    content_type = res.headers.get("content-type", "")
    if "application/json" in content_type:
        return JSONResponse(content=res.json(), status_code=res.status_code)
    return Response(content=res.content, status_code=res.status_code, media_type=content_type)


# ─── scripting-agent連携（汎用プロキシ） ────────────────────────────────
#
# scripting-agentのUI機能をdirector-agent側で再現するため、
# scripting-agentのREST APIをそのまま中継する。director-agent自身は
# 台本生成・編集のロジックを持たない（疎結合維持）。
# 更新系リクエストは project ごとの director_log.json に監査ログとして記録する。
# シリーズ一括生成(generate-series)はプラン+全話ぶんのLLM呼び出しで数分かかるため
# タイムアウトは600秒（120秒では途中でReadTimeout→502になる実測バグ）。

@router.api_route("/api/scripting/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_scripting(path: str, request: Request):
    url = f"{SCRIPTING_AGENT_URL}/{path}"
    body = await request.body()
    headers = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]

    async with httpx.AsyncClient(timeout=600.0) as client:
        try:
            res = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=body,
                headers=headers,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"scripting-agent unreachable: {e}")

    if request.method != "GET":
        m = PROJECT_PATH_RE.match(path)
        if m:
            project_manager.append_director_log(m.group(1), {
                "target": "scripting-agent",
                "method": request.method,
                "path": path,
                "status_code": res.status_code,
            })

    content_type = res.headers.get("content-type", "")
    if "application/json" in content_type:
        return JSONResponse(content=res.json(), status_code=res.status_code)
    return Response(content=res.content, status_code=res.status_code, media_type=content_type)


# ─── scrapping-agent連携（汎用プロキシ） ─────────────────────────────────
#
# 素材収集（クエリ生成・Pexels検索・選択DL・footage.json確定）をdirector-agentの
# 📦素材タブから操作するため、scrapping-agentのREST APIをそのまま中継する。
# LLMクエリ生成・素材一括DLは時間がかかるため、タイムアウトは長め（300秒）。

@router.api_route("/api/scrapping/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_scrapping(path: str, request: Request):
    url = f"{SCRAPPING_AGENT_URL}/{path}"
    body = await request.body()
    headers = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]

    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            res = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=body,
                headers=headers,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"scrapping-agent unreachable: {e}")

    if request.method != "GET":
        m = PROJECT_PATH_RE.match(path)
        if m:
            project_manager.append_director_log(m.group(1), {
                "target": "scrapping-agent",
                "method": request.method,
                "path": path,
                "status_code": res.status_code,
            })

    content_type = res.headers.get("content-type", "")
    if "application/json" in content_type:
        return JSONResponse(content=res.json(), status_code=res.status_code)
    return Response(content=res.content, status_code=res.status_code, media_type=content_type)


# ─── tts-agent連携（汎用プロキシ） ───────────────────────────────────────
#
# tts-agentのUI機能（音声生成・参照音声管理・プレビュー等）をdirector-agent側で
# 再現するため、tts-agentのREST APIをそのまま中継する。音声ファイル(audio/*)など
# 非JSONレスポンスもそのまま転送する。

@router.api_route("/api/tts/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_tts(path: str, request: Request):
    url = f"{TTS_AGENT_URL}/{path}"
    body = await request.body()
    headers = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            res = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=body,
                headers=headers,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"tts-agent unreachable: {e}")

    if request.method != "GET":
        m = PROJECT_PATH_RE.match(path)
        if m:
            project_manager.append_director_log(m.group(1), {
                "target": "tts-agent",
                "method": request.method,
                "path": path,
                "status_code": res.status_code,
            })

    content_type = res.headers.get("content-type", "")
    if "application/json" in content_type:
        return JSONResponse(content=res.json(), status_code=res.status_code)
    return Response(content=res.content, status_code=res.status_code, media_type=content_type)


# ─── editing-agent連携（汎用プロキシ） ───────────────────────────────────
#
# OTIO/SRTラフ編集データ生成（editing-agentの/edit/run・/edit/result等）を
# director-agentの🎞️編集情報タブから操作するため、REST APIをそのまま中継する。

@router.api_route("/api/editing/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_editing(path: str, request: Request):
    url = f"{EDITING_AGENT_URL}/{path}"
    body = await request.body()
    headers = {}
    if "content-type" in request.headers:
        headers["content-type"] = request.headers["content-type"]

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            res = await client.request(
                request.method,
                url,
                params=request.query_params,
                content=body,
                headers=headers,
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"editing-agent unreachable: {e}")

    if request.method != "GET":
        m = PROJECT_PATH_RE.match(path)
        if m:
            project_manager.append_director_log(m.group(1), {
                "target": "editing-agent",
                "method": request.method,
                "path": path,
                "status_code": res.status_code,
            })

    content_type = res.headers.get("content-type", "")
    if "application/json" in content_type:
        return JSONResponse(content=res.json(), status_code=res.status_code)
    return Response(content=res.content, status_code=res.status_code, media_type=content_type)
