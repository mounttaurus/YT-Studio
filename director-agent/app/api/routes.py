import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app.core import project_manager

router = APIRouter(tags=["api"])

TTS_AGENT_URL = os.getenv("TTS_AGENT_URL", "http://tts-agent:8004")
RESEARCH_AGENT_URL = os.getenv("RESEARCH_AGENT_URL", "http://research-agent:8001")
SCRIPTING_AGENT_URL = os.getenv("SCRIPTING_AGENT_URL", "http://scripting-agent:8002")
SCRAPPING_AGENT_URL = os.getenv("SCRAPPING_AGENT_URL", "http://scrapping-agent:8003")
EDITING_AGENT_URL = os.getenv("EDITING_AGENT_URL", "http://editing-agent:8006")

PROJECT_PATH_RE = re.compile(r"^projects/([^/]+)")

_PSASSIST_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".png": "image/png", ".webp": "image/webp",
                         ".json": "application/json"}


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


# ─── Photoshop 合成結果のQA（psassist/）連携 ────────────────────────────
#
# 検査は psassist/ の**ホスト常駐スクリプト**（Photoshop / win32com）が走らせる。
# コンテナではないので叩ける API が無く、連携は共有フォルダ経由が唯一の道。
# スクリプトが episode 配下の psassist/ に qa_report.json と表示用画像を書き、
# director は既存の /shared マウントを読むだけにする。

@router.get("/projects/{project_id}/episodes/{episode_number}/psassist/qa")
async def get_psassist_qa(project_id: str, episode_number: int):
    """合成結果の検査レポート（psassist/scripts/qa_check.py が書く）。"""
    data = project_manager.get_psassist_qa(project_id, episode_number)
    if data is None:
        raise HTTPException(status_code=404, detail="qa_report.json not found")
    return data


@router.get("/projects/{project_id}/episodes/{episode_number}/psassist/file/{rel:path}")
async def get_psassist_file(project_id: str, episode_number: int, rel: str):
    """psassist/ 配下の表示用画像を配信する（サムネ・詳細ビュー・納品PNG）。"""
    f = project_manager.psassist_file(project_id, episode_number, rel)
    if f is None:
        raise HTTPException(status_code=404, detail=f"file not found: {rel}")
    return FileResponse(f, media_type=_PSASSIST_MEDIA_TYPES.get(f.suffix.lower(),
                                                               "application/octet-stream"))


# ─── ホスト工程ブリッジ（ジョブキュー・AROLL_TAB_REDESIGN_PLAN.md Phase 0）────
#
# host_worker.py（ホスト常駐）とは HTTP で繋がない。director は queue/ に
# ジョブを書くだけ、state/ を読むだけ。ホストの生死は shared/_psassist/worker.json
# のハートビートで判定する（プロジェクト非依存のグローバル1本）。

# host_worker.py が実行できる工程。director はこの文字列だけを知り、Photoshop 固有の
# 詳細（COM・JSX・PSD）には触れない（AROLL_TAB_REDESIGN_PLAN.md §2-6）。
_PSASSIST_JOB_KINDS = {"build_plan", "cutout", "build_panel", "qa_check", "export_png"}
# lines 省略で「全件」を意味する工程。export_png だけは対象行の明示を必須にする
#（--resume が mtime を見ないため、全件指定だと直した行が飛ばされる。Phase 0-c）。
_PSASSIST_KINDS_ALLOW_ALL = {"build_plan", "cutout", "build_panel", "qa_check"}


@router.post("/projects/{project_id}/episodes/{episode_number}/psassist/jobs")
async def create_psassist_job(project_id: str, episode_number: int, request: Request):
    """host_worker.py へのジョブをキューへ1件書く（一方通行）。"""
    body = await request.json()
    kind = body.get("kind")
    if kind not in _PSASSIST_JOB_KINDS:
        raise HTTPException(status_code=400, detail=f"unsupported kind: {kind}")
    lines = body.get("lines")
    if lines is None and kind in _PSASSIST_KINDS_ALLOW_ALL:
        lines = []          # 省略＝全件。工程1〜5は話数まるごと回すのが通常
    elif not isinstance(lines, list) or not lines:
        # ⚠️ 空リストは「対象ゼロ」。export_png は対象行の明示を必須にする
        raise HTTPException(status_code=400, detail="lines is required (empty = no target)")
    job = project_manager.enqueue_psassist_job(
        project_id, episode_number, kind, lines, body.get("args") or {})
    if job is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return job


@router.get("/projects/{project_id}/episodes/{episode_number}/psassist/jobs")
async def get_psassist_jobs(project_id: str, episode_number: int):
    """その話数のジョブ状態を新しい順に返す（上限50件）。"""
    return {"jobs": project_manager.list_psassist_jobs(project_id, episode_number)}


@router.get("/psassist/worker")
async def get_psassist_worker():
    """プロジェクト非依存のホスト常駐ハートビート。無ければ psassist 未使用環境。"""
    data = project_manager.get_psassist_worker()
    if data is None:
        raise HTTPException(status_code=404, detail="worker.json not found")
    return data


# ─── 台本承認 → Aロールのプロンプト自動生成（AROLL_TAB_REDESIGN_PLAN.md Phase 1）───
#
# 承認した時点でAロールのプロンプトは確定できる（LLMは無料枠・課金しない）。
# ユーザーにボタンをもう1つ押させる理由が無いので、司令塔がここで繋ぐ。
# **この2段を繋げるのは director だけ**（SCRIPTING/SCRAPPING 両方のURLを持つのは
# director だけ。docker-compose.yml の environment: 参照）。

@router.post("/projects/{project_id}/episodes/{episode_number}/approve-and-prepare")
async def approve_and_prepare(project_id: str, episode_number: int, request: Request):
    """台本を承認し、続けてAロールのプロンプトを用意する。

    ⚠️ **`overwrite` は False 固定。** 承認は台本を直すたび何度も押す操作なので、
    ユーザーが手編集したプロンプト（``prompt_source="user"``）や生成済み画像との
    紐付けを上書きで壊してはいけない。全部作り直したい時は🖼️Aロールタブの
    「プロンプトを作り直す」を使う（そちらが ``overwrite=True``）。

    ⚠️ **プロンプト生成の失敗で承認を失敗にしない。** 承認は台本の確定という
    独立した成果で、Aロールはその後工程。ここで500を返すと「承認できていない」と
    誤解させ、もう一度承認を押させることになる。``aroll_error`` に載せて200を返す。
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass  # ボディ無しで呼ばれるのが通常（承認ボタン）

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            res = await client.post(
                f"{SCRIPTING_AGENT_URL}/projects/{project_id}/approve",
                params={"episode_number": episode_number},
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"scripting-agent unreachable: {e}")
    if res.status_code >= 400:
        raise HTTPException(status_code=res.status_code, detail=res.text)
    out = res.json()

    # ここから先は「失敗しても承認は成功」。例外を外へ出さない。
    payload = {"overwrite": False}
    for k in ("style", "aspect", "extra_prompt", "model"):
        if body.get(k):
            payload[k] = body[k]
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            r2 = await client.post(
                f"{SCRAPPING_AGENT_URL}/projects/{project_id}"
                f"/episodes/{episode_number}/aroll/prompts",
                json=payload,
            )
        if r2.status_code >= 400:
            out["aroll"] = None
            out["aroll_error"] = f"HTTP {r2.status_code}: {r2.text[:300]}"
        else:
            data = r2.json()
            panels = (data.get("manifest") or {}).get("panels") or []
            out["aroll"] = {
                "panel_count": len(panels),
                "warnings": data.get("warnings") or [],
            }
            out["aroll_error"] = None
    except Exception as e:
        out["aroll"] = None
        out["aroll_error"] = f"{type(e).__name__}: {e}"

    # 承認を200で返す以上、失敗は監査ログに残さないと「静かに起きなかった」ことになる
    project_manager.append_director_log(project_id, {
        "action": "approve-and-prepare",
        "episode": episode_number,
        "aroll_panels": (out.get("aroll") or {}).get("panel_count"),
        "aroll_error": out.get("aroll_error"),
    })
    return out


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
