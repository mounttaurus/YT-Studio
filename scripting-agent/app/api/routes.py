"""
REST API エンドポイント — WebUI および外部クライアント向け。
"""
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.core import character_reader, llm_client, project_manager, script_generator, style_registry, translator

TTS_AGENT_URL = os.getenv("TTS_AGENT_URL", "http://tts-agent:8004")
RESEARCH_AGENT_URL = os.getenv("RESEARCH_AGENT_URL", "http://research-agent:8001")

router = APIRouter(tags=["api"])


# ─── リクエスト/レスポンスモデル ───────────────────────────────────────

class GenerateRequest(BaseModel):
    style_id: str
    llm_model: Optional[str] = None
    rough_script: Optional[str] = None
    extra_instruction: Optional[str] = None
    episode_number: int = 1
    target_line_count: Optional[int] = None  # 指定時はこの話だけスタイル既定の行数を上書き


class SeriesGenerateRequest(BaseModel):
    style_id: str
    llm_model: Optional[str] = None
    rough_script: Optional[str] = None
    episode_count: Optional[int] = None
    extra_instruction: Optional[str] = None


class RegenerateRequest(BaseModel):
    feedback: str
    llm_model: Optional[str] = None
    episode_number: int = 1


class LineEditRequest(BaseModel):
    text: Optional[str] = None
    emotion: Optional[str] = None
    speed: Optional[float] = None
    pause_after_sec: Optional[float] = None
    notes: Optional[str] = None
    speaker_id: Optional[str] = None
    speaker_name: Optional[str] = None  # speaker_id指定時のみ使用（省略時は既存行から解決）


class LineInsertRequest(BaseModel):
    after_order: int  # この行番号の直後に挿入（0なら先頭）
    speaker_id: Optional[str] = None
    text: str = ""
    emotion: str = "neutral"
    speed: float = 1.0
    pause_after_sec: float = 0.4


class NewProjectRequest(BaseModel):
    title: str
    slug: Optional[str] = None
    channel: str = "default"


class RenameChannelRequest(BaseModel):
    old: str
    new: str


class StyleSaveRequest(BaseModel):
    style_id: str


class SeoConfigRequest(BaseModel):
    auto: bool


class SaveNamedDraftRequest(BaseModel):
    name: str
    script: Optional[dict] = None


class ExportTextRequest(BaseModel):
    script: Optional[dict] = None          # 画面に表示中の台本（名前付きドラフト読込中など）
    episode_number: Optional[int] = None   # 指定時は episodes/epNN/script_lines.txt にも保存


class NewStyleRequest(BaseModel):
    style_name: str
    description: str
    speakers: list[dict]
    structure: list[dict]
    target_line_count: int = 60
    line_count_mode: str = "auto"
    content_mode: str = "long"
    series_mode: bool = False
    is_default: bool = False


class StyleDefaultRequest(BaseModel):
    is_default: bool


class ImportScriptRequest(BaseModel):
    script: dict
    title: Optional[str] = None


class ImportStyleRequest(BaseModel):
    style: dict


# ─── エンドポイント ───────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "service": "scripting-agent"}


@router.get("/styles")
async def get_styles():
    return {"styles": style_registry.list_styles()}


@router.get("/characters")
async def get_characters():
    """キャラクター・ライブラリの要約一覧（読み取り専用・ディスク直読み）。

    スタイルの話者ピッカーが参照する。CRUD は scrapping-agent の責務。
    """
    return {"characters": character_reader.list_characters()}


@router.get("/tts-voices")
async def get_tts_voices():
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{TTS_AGENT_URL}/voices/profiles")
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return {"profiles": [], "count": 0, "error": "TTS-agentに接続できません"}


@router.get("/projects/{project_id}/episodes/{episode_number}/locales/{lang}/tts-texts")
async def get_locale_tts_texts(project_id: str, episode_number: int, lang: str):
    """翻訳言語のtts.json（生成済み音声のテキスト）をtts-agentから中継する。

    scripting UIの「要再生成」バッジ判定用（Docs/08_i18n.md §8b W1・director UIと同じ判定をscripting側でも表示のみ提供）。
    tts-agentに接続できない/未生成の場合は空配列を返す（バッジ非表示扱い＝呼び出し側で安全側に倒す）。
    """
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(
                f"{TTS_AGENT_URL}/projects/{project_id}/tts",
                params={"episode": episode_number, "lang": lang},
            )
            if resp.status_code == 404:
                return {"audio_files": []}
            resp.raise_for_status()
            data = resp.json()
            return {"audio_files": [
                {"line_id": f.get("line_id"), "text": f.get("text")}
                for f in data.get("audio_files", [])
            ]}
    except Exception:
        return {"audio_files": []}


@router.get("/llm-models")
async def get_llm_models():
    return {"models": llm_client.get_available_models(), "default": llm_client.get_default_model()}


@router.get("/llm-providers")
async def get_llm_providers():
    return {
        "providers": llm_client.get_providers(),
        "default_model": llm_client.get_default_model(),
        "default_provider": llm_client.get_default_provider(),
    }


@router.get("/llm-usage")
async def get_llm_usage():
    """OpenRouterの残高(累計購入額/消費額)を返す。コスト監視用（読み取りのみ・課金なし）。"""
    try:
        return await llm_client.openrouter_credit_balance()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"openrouter credits fetch failed: {e}")


@router.post("/llm-models/refresh")
async def refresh_llm_models():
    """OpenRouter公開APIから無料テキストモデル一覧を再取得し、CSVを更新する。"""
    try:
        count = await llm_client.refresh_openrouter_models()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"OpenRouter APIの取得に失敗しました: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "refreshed", "count": count}


@router.get("/projects/channels")
async def get_channels():
    return {"channels": project_manager.get_channels()}


@router.post("/projects/channels/rename")
async def rename_channel(req: RenameChannelRequest):
    old = (req.old or "").strip()
    new = (req.new or "").strip()
    if not old:
        raise HTTPException(status_code=400, detail="変更元チャンネルを指定してください")
    if not new:
        raise HTTPException(status_code=400, detail="新しいチャンネル名を入力してください")
    count = project_manager.rename_channel(old, new)
    return {"old": old, "new": new, "updated": count, "channels": project_manager.get_channels()}


@router.delete("/projects/channels/{channel}")
async def delete_channel(channel: str):
    """チャンネルを削除＝属するプロジェクトを 'default' へ再割当（非破壊）。"""
    channel = (channel or "").strip()
    if not channel or channel == "default":
        raise HTTPException(status_code=400, detail="default チャンネルは削除できません")
    count = project_manager.delete_channel(channel)
    return {"deleted": channel, "reassigned": count, "channels": project_manager.get_channels()}


@router.post("/projects/new")
async def create_project(req: NewProjectRequest):
    if not req.title.strip():
        raise HTTPException(status_code=400, detail="タイトルを入力してください")
    try:
        pj = project_manager.create_project(
            title=req.title.strip(),
            slug=req.slug,
            channel=req.channel,
        )
        return {"project": pj, "status": "created"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/projects")
async def get_projects(
    search: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return project_manager.list_projects(search=search, channel=channel, limit=limit, offset=offset)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """プロジェクトを完全に削除する（台本・素材・音声・編集情報を含む全データ）。元に戻せない。"""
    if not project_manager.delete_project(project_id):
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    return {"deleted": project_id}


@router.get("/projects/{project_id}/export")
async def export_project(project_id: str):
    """プロジェクト全体（台本・素材・音声・編集情報を含む全データ）をzipでエクスポートする。

    数百MB〜GB規模になり得るため、メモリ上ではなく一時ファイルへzip化してから返す（OOM回避）。
    """
    d = project_manager.find_project_dir(project_id)
    if d is None:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")

    fd, tmp_name = tempfile.mkstemp(suffix=".zip", dir=str(project_manager.SHARED_DIR))
    os.close(fd)
    tmp_path = Path(tmp_name)
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(d.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(d)))

    return FileResponse(
        tmp_path, media_type="application/zip", filename=f"{d.name}.project.zip",
        background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
    )


@router.post("/projects/import")
async def import_project(
    file: UploadFile = File(...),
    new_project_id: Optional[str] = Form(None),
):
    """プロジェクトzipバンドルをインポートする。project_id衝突時は拒否（new_project_idで別ID取り込み可）。

    数百MB〜GB規模になり得るため、アップロードを一時ファイルへストリーミング書き込みしてから展開する。
    """
    tmp_dir = Path(tempfile.mkdtemp(dir=str(project_manager.SHARED_DIR)))
    tmp_zip = tmp_dir / "upload.zip"
    try:
        with open(tmp_zip, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

        try:
            zf = zipfile.ZipFile(tmp_zip)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="zipファイルとして読み込めません")

        if "project.json" not in zf.namelist():
            raise HTTPException(status_code=400, detail="project.json が見つかりません（zipのルート直下に置いてください）")
        try:
            proj_data = json.loads(zf.read("project.json"))
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="project.json が不正なJSONです")

        project_id = (new_project_id or "").strip() or proj_data.get("id", "")
        if not project_id or "/" in project_id or "\\" in project_id or ".." in project_id:
            raise HTTPException(
                status_code=400,
                detail="project_id を解決できません（project.json の id か new_project_id を指定してください）",
            )

        dest = project_manager.PROJECTS_DIR / project_id
        if dest.exists():
            raise HTTPException(
                status_code=409,
                detail=f"project already exists: {project_id}（new_project_id を指定して別IDで取り込めます）",
            )

        # パストラバーサル防止（zip内エントリがdest外へ出ないことを確認）
        dest_resolved = dest.resolve()
        for name in zf.namelist():
            target = (dest / name).resolve()
            if dest_resolved != target and dest_resolved not in target.parents:
                raise HTTPException(status_code=400, detail=f"不正なパスを含むzipです: {name}")

        dest.mkdir(parents=True)
        zf.extractall(dest)

        # project.json の id を実際に作成したproject_idへ補正
        pj_path = dest / "project.json"
        pj = json.loads(pj_path.read_text(encoding="utf-8"))
        pj["id"] = project_id
        pj_path.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")

        return {"project_id": project_id, "status": "imported"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ─── スタイル操作 ──────────────────────────────────────────────────────

@router.post("/styles")
async def create_style(req: NewStyleRequest):
    if not req.style_name.strip():
        raise HTTPException(status_code=400, detail="スタイル名を入力してください")
    if len(req.speakers) == 0:
        raise HTTPException(status_code=400, detail="話者を1人以上追加してください")
    if len(req.structure) == 0:
        raise HTTPException(status_code=400, detail="構成セクションを1つ以上追加してください")
    try:
        style = style_registry.create_user_style(
            style_name=req.style_name.strip(),
            description=req.description.strip(),
            speakers=req.speakers,
            structure=req.structure,
            target_line_count=req.target_line_count,
            line_count_mode=req.line_count_mode,
            content_mode=req.content_mode,
            series_mode=req.series_mode,
            is_default=req.is_default,
        )
        return {"style": style_registry._style_to_summary(style), "status": "created"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/styles/{style_id}")
async def update_style(style_id: str, req: NewStyleRequest):
    style = style_registry.update_user_style(
        style_id=style_id,
        style_name=req.style_name.strip(),
        description=req.description.strip(),
        speakers=req.speakers,
        structure=req.structure,
        target_line_count=req.target_line_count,
        content_mode=req.content_mode,
        line_count_mode=req.line_count_mode,
        series_mode=req.series_mode,
        is_default=req.is_default,
    )
    if style is None:
        raise HTTPException(status_code=404, detail=f"ユーザースタイル '{style_id}' が見つかりません")
    return {"style": style_registry._style_to_summary(style), "status": "updated"}


@router.post("/styles/{style_id}/copy")
async def copy_builtin_style(style_id: str):
    copied = style_registry.copy_builtin_to_user(style_id)
    if copied is None:
        raise HTTPException(status_code=404, detail=f"組み込みスタイル '{style_id}' が見つかりません")
    return {"style": style_registry._style_to_summary(copied), "status": "copied"}


@router.patch("/styles/{style_id}/default")
async def set_style_default(style_id: str, req: StyleDefaultRequest):
    ok = style_registry.toggle_default(style_id, req.is_default)
    if not ok:
        raise HTTPException(status_code=404, detail=f"ユーザースタイル '{style_id}' が見つかりません")
    return {"style_id": style_id, "is_default": req.is_default}


@router.delete("/styles/{style_id}")
async def delete_style(style_id: str):
    ok = style_registry.delete_user_style(style_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"ユーザースタイル '{style_id}' が見つかりません")
    return {"style_id": style_id, "status": "deleted"}


@router.get("/styles/{style_id}/export")
async def export_style(style_id: str):
    """スタイルをエクスポート用封筒JSONとして返す（組み込み/ユーザー定義どちらも可）。"""
    style = style_registry.get_style(style_id)
    if style is None:
        raise HTTPException(status_code=404, detail=f"スタイル '{style_id}' が見つかりません")
    return {
        "kind": "yt-studio-style",
        "export_schema_version": "1.0.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "style": style,
    }


@router.post("/styles/import")
async def import_style(req: ImportStyleRequest):
    """スタイルJSONをユーザースタイルとして取り込む。style_idは自動採番（既存を上書きしない）。"""
    style = req.style
    if style.get("kind") == "yt-studio-style" and isinstance(style.get("style"), dict):
        style = style["style"]
    if not style.get("speakers") or not style.get("structure"):
        raise HTTPException(status_code=400, detail="style.speakers / style.structure が必要です")
    imported = style_registry.import_user_style(style)
    # _is_builtin はロード時にのみ付与されるため、書き込み直後の生dictには無い→再取得して正しく付与させる
    fresh = style_registry.get_style(imported["style_id"])
    return {"style": style_registry._style_to_summary(fresh), "status": "imported"}


# ─── プロジェクト / スタイル ──────────────────────────────────────────

@router.patch("/projects/{project_id}/style")
async def save_style(project_id: str, req: StyleSaveRequest):
    if not style_registry.get_style(req.style_id):
        raise HTTPException(status_code=404, detail=f"スタイル '{req.style_id}' が見つかりません")
    try:
        project_manager.save_project_style(project_id, req.style_id)
        return {"project_id": project_id, "style_id": req.style_id, "status": "saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/projects/{project_id}/style")
async def get_style_for_project(project_id: str):
    style_id = project_manager.get_project_style(project_id)
    return {"project_id": project_id, "style_id": style_id}


@router.patch("/projects/{project_id}/seo-config")
async def save_seo_config(project_id: str, req: SeoConfigRequest):
    """SEO自動最適化トグルの永続化先（config.seo.auto が唯一の本籍）。"""
    try:
        project_manager.save_project_seo_auto(project_id, req.auto)
        return {"project_id": project_id, "auto": req.auto, "status": "saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/projects/{project_id}/seo-config")
async def get_seo_config(project_id: str):
    auto = project_manager.get_project_seo_auto(project_id)
    return {"project_id": project_id, "auto": auto}


# ─── エピソード一覧 ────────────────────────────────────────────────────

@router.get("/projects/{project_id}/episodes")
async def get_episodes(project_id: str):
    """プロジェクトのエピソード一覧を返す。"""
    return {
        "project_id": project_id,
        "episodes": project_manager.list_episodes(project_id),
    }


def _apply_live_speaker_names(project_id: str, doc: Optional[dict]) -> Optional[dict]:
    """返却する台本の speaker_name を配役→キャラ本籍の name で都度上書きする（非破壊）。

    名前の唯一の本籍はキャラ（DATA_SCHEMA §2b）。script.json に保存済みの speaker_name は
    生成時のスナップショットなので、キャラをリネームすると乖離する（例: 「Luka」のまま「ルカ」に
    ならない）。表示時に config.tts.speakers[].character_id 経由で解決し直すことでドリフトを断つ。
    ディスクには書き戻さない（この関数はレスポンス用の一時上書きのみ）。
    """
    if not doc or not doc.get("lines"):
        return doc
    pj = project_manager.read_project(project_id)
    cast = pj.get("config", {}).get("tts", {}).get("speakers", [])
    name_map = character_reader.resolve_cast_names(cast)
    if not name_map:
        return doc
    for l in doc["lines"]:
        nm = name_map.get(l.get("speaker_id"))
        if nm:
            l["speaker_name"] = nm
    return doc


@router.get("/projects/{project_id}/episodes/{episode_number}/script")
async def get_episode_script(project_id: str, episode_number: int, draft: bool = True):
    """指定エピソードのドラフトまたは確定スクリプトを返す。"""
    if draft:
        data = project_manager.read_draft(project_id, episode_number)
    else:
        data = project_manager.read_script(project_id, episode_number)
    if data is None:
        raise HTTPException(status_code=404, detail=f"第{episode_number}話のスクリプトが見つかりません")
    return _apply_live_speaker_names(project_id, data)


@router.get("/projects/{project_id}/episodes/{episode_number}/export")
async def export_script(project_id: str, episode_number: int):
    """台本をエクスポート用封筒JSONとして返す（ドラフト優先、無ければ確定版）。"""
    script = (
        project_manager.read_draft(project_id, episode_number)
        or project_manager.read_script(project_id, episode_number)
    )
    if script is None:
        raise HTTPException(status_code=404, detail=f"第{episode_number}話の台本が見つかりません")
    return {
        "kind": "yt-studio-script",
        "export_schema_version": "1.0.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_project_id": project_id,
        "source_episode": episode_number,
        "script": script,
    }


@router.post("/projects/{project_id}/episodes/{episode_number}/import")
async def import_script(
    project_id: str, episode_number: int, req: ImportScriptRequest,
    force: bool = Query(False, description="既に確定済み台本がある話でも上書きする"),
):
    """完成台本を直接インポートして script.json として確定する（LLM生成をスキップ）。

    封筒形式（{"kind":"yt-studio-script","script":{...}}）と、script.json生の中身
    （{"lines":[...], ...}）の両方を受理する＝ファイル名やラップの有無に依存しない。
    既に確定済み台本がある話への上書きは force=true を明示した時のみ許可する。
    """
    script = req.script
    if script.get("kind") == "yt-studio-script" and isinstance(script.get("script"), dict):
        script = script["script"]
    if "lines" not in script or not isinstance(script.get("lines"), list):
        raise HTTPException(status_code=400, detail="script.lines が必要です")

    if not force and project_manager.read_script(project_id, episode_number) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"第{episode_number}話には既に確定済み台本があります。force=true で上書きできます",
        )

    script.setdefault("project_id", project_id)
    script.setdefault("schema_version", "1.0.0")
    script.setdefault("metadata", {})
    script["metadata"]["checked_by_director"] = True
    script["metadata"]["check_passed"] = True

    ep_dir = project_manager.get_episode_dir(project_id, episode_number)
    (ep_dir / "script_draft.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ep_dir / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    project_manager.ensure_episode_in_project_json(project_id, episode_number, req.title or "")
    project_manager.update_episode_status(project_id, episode_number, "scripting", "skipped")
    project_manager.update_project_status(project_id, "scripting", "skipped")

    return {
        "project_id": project_id,
        "episode_number": episode_number,
        "status": "imported",
        "line_count": len(script["lines"]),
    }


# ─── 台本生成 ────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/generate")
async def generate_script(project_id: str, req: GenerateRequest):
    """台本を生成してエピソードのドラフトとして保存する。"""
    try:
        project_manager.update_project_status(project_id, "scripting", "running")

        if req.rough_script:
            project_manager.save_rough_script(project_id, req.rough_script)

        input_content = _build_input_content(project_id, req.rough_script)

        script_json, warnings = await script_generator.generate(
            input_content=input_content,
            style_id=req.style_id,
            project_id=project_id,
            model=req.llm_model,
            extra_instruction=req.extra_instruction,
            target_line_count=req.target_line_count,
        )
        project_manager.save_draft(project_id, script_json, req.episode_number)
        project_manager.ensure_episode_in_project_json(project_id, req.episode_number)
        project_manager.update_project_status(project_id, "scripting", "pending")
        project_manager.update_episode_status(project_id, req.episode_number, "scripting", "pending")

        style = style_registry.get_style(req.style_id)
        if style:
            project_manager.sync_tts_speakers(project_id, style.get("speakers", []))

        return {
            "project_id": project_id,
            "episode_number": req.episode_number,
            "status": "draft_saved",
            "line_count": len(script_json["lines"]),
            "warnings": warnings,
            "script": script_json,
        }
    except ValueError as e:
        project_manager.update_project_status(project_id, "scripting", "error", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        project_manager.update_project_status(project_id, "scripting", "error", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/generate-series")
async def generate_series_script(project_id: str, req: SeriesGenerateRequest):
    """シリーズ台本を生成し、各話を episodes/epNN/ に保存する。"""
    try:
        project_manager.update_project_status(project_id, "scripting", "running")

        if req.rough_script:
            project_manager.save_rough_script(project_id, req.rough_script)

        input_content = _build_input_content(project_id, req.rough_script)

        episodes = await script_generator.generate_series(
            input_content=input_content,
            style_id=req.style_id,
            project_id=project_id,
            model=req.llm_model,
            episode_count=req.episode_count,
            user_instruction=req.extra_instruction,
        )

        results = []
        for number, script_json, warnings in episodes:
            project_manager.save_draft(project_id, script_json, number)
            project_manager.ensure_episode_in_project_json(project_id, number)
            series_meta = script_json.get("metadata", {}).get("series", {})
            ep_title = series_meta.get("episode_title", f"第{number}話")
            project_manager.update_episode_status(project_id, number, "scripting", "pending")
            results.append({
                "episode_number": number,
                "episode_title": ep_title,
                "line_count": len(script_json.get("lines", [])),
                "warnings": warnings,
            })

        project_manager.update_project_status(project_id, "scripting", "pending")

        style = style_registry.get_style(req.style_id)
        if style:
            project_manager.sync_tts_speakers(project_id, style.get("speakers", []))

        return {
            "project_id": project_id,
            "status": "series_draft_saved",
            "episode_count": len(results),
            "episodes": results,
        }
    except ValueError as e:
        project_manager.update_project_status(project_id, "scripting", "error", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        project_manager.update_project_status(project_id, "scripting", "error", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/regenerate")
async def regenerate_script(project_id: str, req: RegenerateRequest):
    """フィードバックを元に指定エピソードを再生成する。"""
    try:
        project_manager.update_project_status(project_id, "scripting", "running")

        draft = project_manager.read_draft(project_id, req.episode_number)
        if draft is None:
            raise HTTPException(status_code=404, detail=f"第{req.episode_number}話のドラフトが見つかりません。先に /generate を実行してください。")

        style_id = draft["metadata"]["style"]
        model = req.llm_model or draft["metadata"].get("llm_model")
        input_content = _build_input_content(project_id, None)

        script_json, warnings = await script_generator.generate(
            input_content=input_content,
            style_id=style_id,
            project_id=project_id,
            model=model,
            feedback=req.feedback,
        )
        project_manager.save_draft(project_id, script_json, req.episode_number)
        project_manager.update_project_status(project_id, "scripting", "pending")
        project_manager.update_episode_status(project_id, req.episode_number, "scripting", "pending")

        return {
            "project_id": project_id,
            "episode_number": req.episode_number,
            "status": "draft_saved",
            "line_count": len(script_json["lines"]),
            "warnings": warnings,
            "script": script_json,
        }
    except HTTPException:
        raise
    except ValueError as e:
        project_manager.update_project_status(project_id, "scripting", "error", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        project_manager.update_project_status(project_id, "scripting", "error", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/approve")
async def approve_script(
    project_id: str,
    episode_number: int = Query(1, description="承認するエピソード番号"),
):
    """指定エピソードのドラフトを承認して script.json として確定する。"""
    try:
        script_file = project_manager.approve_script(project_id, episode_number)
        return {
            "project_id": project_id,
            "episode_number": episode_number,
            "status": "approved",
            "script_path": str(script_file),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/projects/{project_id}/script")
async def get_script(project_id: str, draft: bool = True, episode: int = 1):
    """後方互換: 指定エピソードのドラフトまたは確定スクリプトを返す。"""
    if draft:
        data = project_manager.read_draft(project_id, episode)
    else:
        data = project_manager.read_script(project_id, episode)
    if data is None:
        raise HTTPException(status_code=404, detail="スクリプトが見つかりません")
    return _apply_live_speaker_names(project_id, data)


# ─── 多言語（行保存翻訳・DATA_SCHEMA §2c / Docs/08_i18n.md §5） ─────────────

class TranslateRequest(BaseModel):
    lang: str                              # ISO 639-1（例 en / es）
    model: Optional[str] = None            # 省略時は既存LLMルーティング
    instructions: Optional[str] = None     # 任意の訳調指示


@router.post("/projects/{project_id}/episodes/{episode_number}/translate")
async def translate_episode(
    project_id: str,
    episode_number: int,
    req: TranslateRequest,
    force: bool = Query(False, description="既存翻訳がある場合に上書きするか"),
):
    """確定 script.json を行保存翻訳して locales/{lang}/script.json を生成する。

    line_id / speaker_id / section 等は原本と同一のまま text だけを翻訳する
    （Aロールパネル・素材紐付けを言語間で共有するための必須条件）。
    """
    lang = req.lang.strip().lower()
    pj = project_manager.read_project(project_id)
    if lang == pj.get("language", "ja"):
        raise HTTPException(status_code=400, detail=f"原語（{lang}）への翻訳は不要です")

    if project_manager.read_script(project_id, episode_number) is None:
        raise HTTPException(
            status_code=409,
            detail="確定 script.json がありません。先に台本を承認（✓）してください",
        )
    if project_manager.read_locale_script(project_id, episode_number, lang) is not None and not force:
        raise HTTPException(
            status_code=409,
            detail=f"{lang} の翻訳が既に存在します。上書きするには ?force=true を指定してください",
        )

    project_manager.update_locale_status(project_id, episode_number, lang, "translation", "running")
    try:
        result = await translator.translate_episode(
            project_id, episode_number, lang,
            model=req.model, instructions=req.instructions,
        )
    except ValueError as e:
        project_manager.update_locale_status(project_id, episode_number, lang, "translation", "error")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        project_manager.update_locale_status(project_id, episode_number, lang, "translation", "error")
        raise HTTPException(status_code=500, detail=f"翻訳に失敗しました: {e}")

    project_manager.update_locale_status(
        project_id, episode_number, lang, "translation", "done", title=result["title"],
    )
    project_manager.update_locale_status(project_id, episode_number, lang, "tts", "pending")
    return {
        "project_id": project_id,
        "episode_number": episode_number,
        "lang": lang,
        "title": result["title"],
        "line_count": result["line_count"],
        "translated_count": result["translated_count"],
        "status": "done",
    }


@router.get("/projects/{project_id}/episodes/{episode_number}/locales")
async def list_locales(project_id: str, episode_number: int):
    """このエピソードの言語別状態（原語＋翻訳済み言語＋鮮度）を返す。"""
    pj = project_manager.read_project(project_id)
    source_lang = pj.get("language", "ja")
    script = project_manager.read_script(project_id, episode_number)
    current_hash = project_manager.source_script_hash(script) if script else None

    locales = {}
    for ep in pj.get("episodes", []):
        if ep.get("number") == episode_number:
            locales = ep.get("locales", {}) or {}
            break

    out = []
    for lang, meta in locales.items():
        loc_script = project_manager.read_locale_script(project_id, episode_number, lang)
        stale = bool(
            loc_script is not None and current_hash is not None
            and loc_script.get("source_script_hash") != current_hash
        )
        out.append({
            "lang": lang,
            "title": meta.get("title", ""),
            "status": meta.get("status", {}),
            "has_script": loc_script is not None,
            "stale": stale,
        })
    return {
        "project_id": project_id,
        "episode_number": episode_number,
        "source_language": source_lang,
        "has_source_script": script is not None,
        "locales": out,
    }


@router.get("/projects/{project_id}/episodes/{episode_number}/locales/{lang}/script")
async def get_locale_script(project_id: str, episode_number: int, lang: str):
    """翻訳済み script.json を返す（speaker_name は原語同様に都度解決）。"""
    data = project_manager.read_locale_script(project_id, episode_number, lang)
    if data is None:
        raise HTTPException(status_code=404, detail=f"{lang} の翻訳スクリプトがありません")
    return _apply_live_speaker_names(project_id, data)


@router.delete("/projects/{project_id}/episodes/{episode_number}/locales/{lang}")
async def delete_locale(project_id: str, episode_number: int, lang: str):
    """言語別成果物（locales/{lang}/ 一式）と project.json の locales エントリを削除する。"""
    ep_dir = project_manager.get_project_dir(project_id) / "episodes" / f"ep{episode_number:02d}"
    loc_dir = ep_dir / "locales" / lang
    if not loc_dir.exists():
        raise HTTPException(status_code=404, detail=f"{lang} の翻訳がありません")
    shutil.rmtree(loc_dir)

    pj_dir = project_manager.get_project_dir(project_id)
    pj_file = pj_dir / "project.json"
    if pj_file.exists():
        pj = json.loads(pj_file.read_text(encoding="utf-8"))
        for ep in pj.get("episodes", []):
            if ep.get("number") == episode_number:
                (ep.get("locales") or {}).pop(lang, None)
                break
        pj["updated_at"] = datetime.now(timezone.utc).isoformat()
        pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"deleted": lang, "project_id": project_id, "episode_number": episode_number}


class LocaleLinePatchRequest(BaseModel):
    text: str


@router.patch("/projects/{project_id}/episodes/{episode_number}/locales/{lang}/lines/{line_id}")
async def patch_locale_line(
    project_id: str, episode_number: int, lang: str, line_id: str, req: LocaleLinePatchRequest,
):
    """翻訳済み行の text のみを編集する（Docs/08_i18n.md §8b W1）。

    行の挿入・削除・並べ替えは意図的に提供しない（line_id構造の保護＝Aロール/素材共有の前提を守る
    ガードレール）。source_script_hash は触らない（鮮度判定は原語変更の検知であり、翻訳側の手修正は
    「古さ」ではない）。
    """
    loc_script = project_manager.read_locale_script(project_id, episode_number, lang)
    if loc_script is None:
        raise HTTPException(status_code=404, detail=f"{lang} の翻訳スクリプトがありません")

    target = next((l for l in loc_script.get("lines", []) if l.get("id") == line_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"行が見つかりません: {line_id}")

    target["text"] = req.text
    project_manager.save_locale_script(project_id, episode_number, lang, loc_script)
    return _apply_live_speaker_names(project_id, loc_script)


# ─── 行操作（ライトスルー同期） ───────────────────────────────────────
#
# 行の編集・挿入・削除は script_draft.json と script.json（存在する場合）の
# 両方へ即時反映する。承認済みの台本を後から微修正しても、TTSが読む
# script.json と常に同期した状態を保つ（再承認は不要）。
# 行の id は不変（音声ファイル・キャッシュ・tts.json との対応キー）。
# 挿入・削除時は order のみ振り直す。

def _load_script_docs(project_id: str, episode: int):
    """(primary, draft, script)。primaryは表示と同じ優先順位（draft優先、なければ確定版）。"""
    draft = project_manager.read_draft(project_id, episode)
    script = project_manager.read_script(project_id, episode)
    primary = draft if draft is not None else script
    return primary, draft, script


def _save_script_docs(project_id: str, episode: int, draft, script) -> None:
    if draft is not None:
        project_manager.save_draft(project_id, draft, episode)
    if script is not None:
        project_manager.save_script(project_id, script, episode)


def _renumber_lines(doc: dict) -> None:
    for i, l in enumerate(doc.get("lines", []), 1):
        l["order"] = i
    doc.setdefault("metadata", {})["line_count"] = len(doc.get("lines", []))


def _next_line_id(*docs) -> str:
    mx = 0
    for doc in docs:
        if not doc:
            continue
        for l in doc.get("lines", []):
            m = re.match(r"line_(\d+)$", str(l.get("id", "")))
            if m:
                mx = max(mx, int(m.group(1)))
    return f"line_{mx + 1:03d}"


@router.patch("/projects/{project_id}/script/line/{order}")
async def edit_line(
    project_id: str,
    order: int,
    req: LineEditRequest,
    episode: int = Query(1),
):
    """特定行を直接編集する（ドラフトと確定版script.jsonの両方に反映）。"""
    primary, draft, script = _load_script_docs(project_id, episode)
    if primary is None:
        raise HTTPException(status_code=404, detail=f"第{episode}話の台本が見つかりません")

    line = next((l for l in primary["lines"] if l["order"] == order), None)
    if line is None:
        raise HTTPException(status_code=404, detail=f"order={order} の行が見つかりません")

    new_speaker_name = None
    if req.speaker_id is not None:
        if req.speaker_name is not None:
            # 明示指定があればそれを使う（エキストラ等は空文字も許容）
            new_speaker_name = req.speaker_name
        else:
            # 未指定なら既存行の表示名→idの順でフォールバック
            new_speaker_name = next(
                (l.get("speaker_name") for l in primary["lines"]
                 if l.get("speaker_id") == req.speaker_id and l.get("speaker_name")),
                req.speaker_id,
            )

    def apply(l: dict) -> None:
        if req.speaker_id is not None:
            l["speaker_id"] = req.speaker_id
            l["speaker_name"] = new_speaker_name
        if req.text is not None:
            l["text"] = req.text
        if req.emotion is not None:
            l["emotion"] = req.emotion
        if req.speed is not None:
            l["speed"] = req.speed
        if req.pause_after_sec is not None:
            l["pause_after_sec"] = req.pause_after_sec
        if req.notes is not None:
            l["notes"] = req.notes

    apply(line)
    # もう片方のファイルにも同じ id の行があれば同じ変更を適用
    other = script if primary is draft else draft
    if other is not None:
        oline = next((l for l in other["lines"] if l.get("id") == line.get("id")), None)
        if oline is not None:
            apply(oline)

    _save_script_docs(project_id, episode, draft, script)
    return {"project_id": project_id, "episode_number": episode, "updated_line": line}


@router.post("/projects/{project_id}/script/line")
async def insert_line(
    project_id: str,
    req: LineInsertRequest,
    episode: int = Query(1),
):
    """指定行の直後に新しい行を挿入する（after_order=0で先頭）。
    新しい行には未使用の id を発番し、order は全行で振り直す。
    """
    primary, draft, script = _load_script_docs(project_id, episode)
    if primary is None:
        raise HTTPException(status_code=404, detail=f"第{episode}話の台本が見つかりません")

    lines = primary.get("lines", [])
    if req.after_order < 0 or req.after_order > len(lines):
        raise HTTPException(status_code=400, detail=f"after_order={req.after_order} が範囲外です")
    anchor = lines[req.after_order - 1] if req.after_order >= 1 else None

    new_id = _next_line_id(draft, script)
    ref = anchor or (lines[0] if lines else {})
    # 行追加は「完全な空行」を作る＝直前行の話者をコピーしない（配役はUIの編集で選ぶ）。
    # speaker_id が明示指定された時だけ採用し、その表示名を既存行から引く。
    speaker_id = req.speaker_id or ""
    speaker_name = ""
    if speaker_id:
        speaker_name = next(
            (l.get("speaker_name") for l in lines
             if l.get("speaker_id") == speaker_id and l.get("speaker_name")),
            speaker_id,
        )
    new_line = {
        "id": new_id,
        "order": 0,  # 挿入後に振り直す
        "speaker_id": speaker_id,
        "speaker_name": speaker_name,
        "text": req.text,
        "emotion": req.emotion,
        "speed": req.speed,
        "pause_after_sec": req.pause_after_sec,
        "section": ref.get("section", "main"),
        "notes": "",
    }

    def insert_into(doc: dict) -> None:
        dl = doc.setdefault("lines", [])
        if anchor is None:
            pos = 0
        else:
            idx = next((i for i, l in enumerate(dl) if l.get("id") == anchor.get("id")), None)
            pos = (idx + 1) if idx is not None else len(dl)
        dl.insert(pos, dict(new_line))
        _renumber_lines(doc)
        # sections の line_ids にも追加（anchorと同じセクションの直後）
        placed = False
        for s in doc.get("sections") or []:
            ids = s.get("line_ids") or []
            if anchor is not None and anchor.get("id") in ids:
                ids.insert(ids.index(anchor["id"]) + 1, new_id)
                placed = True
                break
        secs = doc.get("sections") or []
        if not placed and secs:
            if anchor is None:
                secs[0].setdefault("line_ids", []).insert(0, new_id)
            else:
                secs[-1].setdefault("line_ids", []).append(new_id)

    if draft is not None:
        insert_into(draft)
    if script is not None:
        insert_into(script)
    _save_script_docs(project_id, episode, draft, script)

    saved = next((l for l in primary["lines"] if l.get("id") == new_id), new_line)
    return {"project_id": project_id, "episode_number": episode, "new_line": saved}


@router.delete("/projects/{project_id}/script/line/{order}")
async def delete_line(
    project_id: str,
    order: int,
    episode: int = Query(1),
):
    """特定行を削除する（ドラフトと確定版script.jsonの両方から）。
    対応する音声の削除は呼び出し側が tts-agent の DELETE /projects/{id}/audio/line/{line_id} で行う。
    """
    primary, draft, script = _load_script_docs(project_id, episode)
    if primary is None:
        raise HTTPException(status_code=404, detail=f"第{episode}話の台本が見つかりません")

    line = next((l for l in primary["lines"] if l["order"] == order), None)
    if line is None:
        raise HTTPException(status_code=404, detail=f"order={order} の行が見つかりません")
    line_id = line.get("id")

    def remove_from(doc: dict) -> None:
        before = len(doc.get("lines", []))
        doc["lines"] = [l for l in doc.get("lines", []) if l.get("id") != line_id]
        if len(doc["lines"]) < before:
            _renumber_lines(doc)
            for s in doc.get("sections") or []:
                if s.get("line_ids"):
                    s["line_ids"] = [i for i in s["line_ids"] if i != line_id]

    if draft is not None:
        remove_from(draft)
    if script is not None:
        remove_from(script)
    _save_script_docs(project_id, episode, draft, script)

    return {"project_id": project_id, "episode_number": episode, "deleted_line_id": line_id}


# ─── 名前付きドラフト ────────────────────────────────────────────────

@router.post("/projects/{project_id}/drafts")
async def save_named_draft(project_id: str, req: SaveNamedDraftRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="名前を入力してください")
    draft = req.script or project_manager.read_draft(project_id, 1)
    if draft is None:
        raise HTTPException(status_code=404, detail="ドラフトが見つかりません")
    try:
        f = project_manager.save_named_draft(project_id, req.name.strip(), draft)
        return {"project_id": project_id, "name": f.stem, "status": "saved", "path": str(f)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/export-text")
async def export_text(project_id: str, req: ExportTextRequest):
    """台本を行番号付きテキストにして返す。episode_number 指定時はファイルにも保存する。"""
    script = req.script
    if script is None and req.episode_number is not None:
        script = (
            project_manager.read_draft(project_id, req.episode_number)
            or project_manager.read_script(project_id, req.episode_number)
        )
    if not script or not script.get("lines"):
        raise HTTPException(status_code=404, detail="書き出す台本が見つかりません")

    # 話者名は配役→キャラ本籍で都度解決してから書き出す（保存済みスナップショットのドリフト回避）
    script = _apply_live_speaker_names(project_id, script)
    text = project_manager.build_lines_text(script)
    saved_path = None
    if req.episode_number is not None:
        f = project_manager.save_script_lines_text(project_id, text, req.episode_number)
        saved_path = str(f)
        filename = f"{project_id}_ep{req.episode_number:02d}_lines.txt"
    else:
        filename = f"{project_id}_lines.txt"
    return {"text": text, "saved_path": saved_path, "filename": filename}


@router.get("/projects/{project_id}/drafts")
async def list_named_drafts(project_id: str):
    return {"project_id": project_id, "drafts": project_manager.list_named_drafts(project_id)}


@router.get("/projects/{project_id}/drafts/{name}")
async def get_named_draft(project_id: str, name: str):
    data = project_manager.get_named_draft(project_id, name)
    if data is None:
        raise HTTPException(status_code=404, detail=f"ドラフト '{name}' が見つかりません")
    return data


@router.delete("/projects/{project_id}/drafts/{name}")
async def delete_named_draft(project_id: str, name: str):
    ok = project_manager.delete_named_draft(project_id, name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"ドラフト '{name}' が見つかりません")
    return {"project_id": project_id, "name": name, "status": "deleted"}


# ─── テキスト抽出 ─────────────────────────────────────────────────────

@router.post("/extract-text")
async def extract_text(file: UploadFile = File(...)):
    name = (file.filename or "").lower()
    raw = await file.read()
    try:
        if name.endswith(".pdf"):
            text = _extract_pdf_text(raw)
        elif name.endswith(".docx"):
            text = _extract_docx_text(raw)
        elif name.endswith(".json"):
            text = _extract_json_script_text(raw)
        else:
            text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"ファイルの読み込みに失敗しました: {e}")
    return {"filename": file.filename, "text": text}


def _extract_pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()


def _extract_docx_text(raw: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(raw))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_json_script_text(raw: bytes) -> str:
    data = json.loads(raw.decode("utf-8"))
    lines = data.get("lines") if isinstance(data, dict) else None
    if not lines:
        return json.dumps(data, ensure_ascii=False, indent=2)
    texts = [line.get("text", "") for line in lines if line.get("text")]
    return "\n".join(texts)


# ─── research-agent連携（汎用プロキシ） ─────────────────────────────────
#
# scripting-agentのスタンドアロンUI（:8002）はresearch-agent（:8001）と別オリジンのため、
# SEO最適化（/seo/optimize等）をブラウザから直接叩けない。director-agentの汎用プロキシ
# （director-agent/app/api/routes.py の proxy_research）と同じパターンをここにも敷く。
# 蒸留(LLM)・SEO分析は時間がかかるためタイムアウトは長め（300秒）。

@router.api_route("/research/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
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

    content_type = res.headers.get("content-type", "")
    if "application/json" in content_type:
        return JSONResponse(content=res.json(), status_code=res.status_code)
    return Response(content=res.content, status_code=res.status_code, media_type=content_type)


# ─── ヘルパー ──────────────────────────────────────────────────────────

def _build_input_content(project_id: str, rough_script: Optional[str]) -> str:
    parts = []
    research = project_manager.read_research(project_id)
    if research:
        parts.append("## リサーチ情報\n" + json.dumps(research, ensure_ascii=False, indent=2))
    existing_rough = rough_script or project_manager.read_rough_script(project_id)
    if existing_rough:
        parts.append("## ラフ台本\n" + existing_rough)
    # SEO反映はキュレーション済みの厳選キーワード（script_brief）だけを軽く注入する。
    # 旧来の for_script（全部盛り指示文）は詰め込みで台本を壊すため、注入ソースには使わない。
    seo_pack = project_manager.read_seo_pack(project_id)
    brief = (seo_pack or {}).get("script_brief") or {}
    keywords = [k for k in (brief.get("keywords") or []) if k]
    if keywords:
        parts.append(
            "## SEO反映（軽め・任意）\n"
            "次の語は視聴者検索に効きます。話の面白さ・構成・オチ・キャラの一貫性を最優先し、"
            "自然に馴染む範囲でのみ織り込んでください（無理なら入れなくて構いません。詰め込み厳禁）:\n"
            + "、".join(keywords)
        )
    if not parts:
        parts.append(f"プロジェクトID: {project_id}（リサーチ情報なし）")
    return "\n\n".join(parts)
