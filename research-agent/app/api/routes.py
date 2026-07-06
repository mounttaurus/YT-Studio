import logging
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.core import (
    cloudflare_text,
    digest_builder,
    grounded_search,
    llm_client,
    project_manager,
    seo_optimizer,
    source_ingest,
    youtube_client,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── モデル ───────────────────────────────────────────────────────────

class TextSourceRequest(BaseModel):
    title: Optional[str] = None
    text: Optional[str] = None
    url: Optional[str] = None


class SearchRequest(BaseModel):
    query: str
    max_results: int = 6


class DigestRequest(BaseModel):
    target_duration_sec: int = 300
    model: Optional[str] = None
    extra_instruction: Optional[str] = None


class SeoOptimizeRequest(BaseModel):
    force: bool = False
    model: Optional[str] = None
    rough_script: Optional[str] = None  # UI未保存のラフ台本を先に永続化してから分析する


class PublishPackRequest(BaseModel):
    model: Optional[str] = None


# ─── 基本 ─────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "research-agent",
        "gemini_configured": bool(llm_client.gemini_api_key()),
        "research_key_dedicated": bool(__import__("os").getenv("RESEARCH_GEMINI_API_KEY")),
        "cloudflare_configured": cloudflare_text.is_configured(),
        "youtube": {"configured": youtube_client.configured()},
    }


@router.get("/projects")
async def get_projects():
    return {"projects": project_manager.list_projects()}


@router.get("/models")
async def get_models():
    return {"models": llm_client.list_models(), "default": llm_client.DEFAULT_MODEL}


# ─── ソース ───────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/sources")
async def get_sources(project_id: str):
    sources = project_manager.load_sources(project_id)
    # UI には本文全文は返さず軽量化（先頭プレビューのみ）
    return {"sources": [
        {**{k: v for k, v in s.items() if k != "text"},
         "preview": (s.get("text") or "")[:200]}
        for s in sources
    ]}


@router.post("/projects/{project_id}/sources/upload")
async def upload_source(project_id: str, file: UploadFile = File(...)):
    data = await file.read()
    try:
        kind, text = await source_ingest.extract_file(file.filename, data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"取り込み失敗: {e}")
    if not text:
        raise HTTPException(status_code=422, detail="テキストを抽出できませんでした")
    src = project_manager.add_source(project_id, kind, file.filename, text)
    return {"source": {k: v for k, v in src.items() if k != "text"}}


@router.post("/projects/{project_id}/sources/text")
async def add_text_source(project_id: str, req: TextSourceRequest):
    if req.url:
        try:
            title, text = await source_ingest.fetch_url(req.url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"URL取得失敗: {e}")
        src = project_manager.add_source(project_id, "url", req.title or title, text, url=req.url)
    elif req.text:
        src = project_manager.add_source(project_id, "text", req.title or "貼付テキスト", req.text)
    else:
        raise HTTPException(status_code=400, detail="text か url のいずれかが必要です")
    return {"source": {k: v for k, v in src.items() if k != "text"}}


@router.post("/projects/{project_id}/sources/search")
async def search_sources(project_id: str, req: SearchRequest):
    if not llm_client.gemini_api_key():
        raise HTTPException(status_code=400, detail="Gemini APIキー未設定（検索グラウンディング不可）")
    results, summary = await grounded_search.search(req.query, req.max_results)
    added = []
    # 検索の要約を1ソースとして保存（出典群の文脈）
    if summary:
        urls = ", ".join(r["url"] for r in results)
        body = f"{summary}\n\n参照: {urls}" if urls else summary
        added.append(project_manager.add_source(project_id, "grounded", f"検索: {req.query}", body))
    # 各出典を軽量ソースとして保存（本文は未取得・URLのみ）
    for r in results:
        added.append(project_manager.add_source(
            project_id, "grounded", r["title"], r.get("snippet", ""), url=r["url"]))
    return {"added": [{k: v for k, v in s.items() if k != "text"} for s in added],
            "count": len(added)}


@router.delete("/projects/{project_id}/sources/{source_id}")
async def remove_source(project_id: str, source_id: str):
    if not project_manager.delete_source(project_id, source_id):
        raise HTTPException(status_code=404, detail="source not found")
    return {"ok": True}


# ─── 蒸留（核） ───────────────────────────────────────────────────────

@router.post("/projects/{project_id}/digest")
async def make_digest(project_id: str, req: DigestRequest):
    try:
        result = await digest_builder.build(
            project_id,
            target_duration_sec=req.target_duration_sec,
            model=req.model,
            extra_instruction=req.extra_instruction,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("digest failed")
        raise HTTPException(status_code=502, detail=f"蒸留失敗: {e}")
    return result


@router.get("/projects/{project_id}/digest")
async def get_digest(project_id: str):
    research = project_manager.read_research(project_id)
    rough = project_manager.read_rough_script(project_id)
    if research is None and rough is None:
        raise HTTPException(status_code=404, detail="まだ蒸留されていません")
    return {"research": research, "rough_script": rough}


# ─── YouTube SEOオプティマイザ ─────────────────────────────────────────

@router.post("/projects/{project_id}/seo/optimize")
async def optimize_seo(project_id: str, req: SeoOptimizeRequest):
    if not youtube_client.configured():
        raise HTTPException(status_code=503, detail="YOUTUBE_DATA_API_KEY が未設定です")
    if req.rough_script and req.rough_script.strip():
        project_manager.save_rough_script(project_id, req.rough_script)
    try:
        result = await seo_optimizer.optimize(project_id, force=req.force, model=req.model)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("seo optimize failed")
        raise HTTPException(status_code=502, detail=f"SEO分析失敗: {e}")
    return result


@router.get("/projects/{project_id}/seo")
async def get_seo(project_id: str):
    pack = seo_optimizer.read_seo_pack(project_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="seo_pack.json がまだありません")
    return pack


@router.post("/projects/{project_id}/episodes/{episode_number}/publish-pack")
async def create_publish_pack(project_id: str, episode_number: int, req: PublishPackRequest):
    try:
        result = await seo_optimizer.build_publish_pack(project_id, episode_number, model=req.model)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("publish pack failed")
        raise HTTPException(status_code=502, detail=f"公開パック生成失敗: {e}")
    return result


@router.get("/projects/{project_id}/episodes/{episode_number}/publish-pack")
async def get_publish_pack(project_id: str, episode_number: int):
    pack = seo_optimizer.read_publish_pack(project_id, episode_number)
    if pack is None:
        raise HTTPException(status_code=404, detail="publish_pack.json がまだありません")
    return pack


@router.get("/youtube/quota")
async def get_youtube_quota():
    return youtube_client.quota_status()
