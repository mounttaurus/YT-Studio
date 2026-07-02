import asyncio
import io
import json
import os
import shutil
import struct
import uuid
import zipfile
import zlib
from datetime import datetime, timezone
from typing import Optional

from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app.core import (
    auto_selector,
    character_manager,
    cloudflare_client,
    comfy_client,
    grok_image_client,
    lyria_client,
    model_downloader,
    nanobanana_client,
    panel_presets,
    pexels_client,
    pixabay_client,
    project_manager,
    query_generator,
    runware_client,
    style_manager,
    vecteezy_client,
)

# 検索ソースのレジストリ。新ソース追加時はクライアントモジュールを作りここに登録する。
# AI生成は検索ではなくセクション単位の /generate エンドポイントで独立して動く。
SOURCES = {
    "pexels": {"client": pexels_client, "env_key": "PEXELS_API_KEY"},
    "pixabay": {"client": pixabay_client, "env_key": "PIXABAY_API_KEY"},
    "vecteezy": {"client": vecteezy_client, "env_key": "VECTEEZY_API_KEY"},
}

router = APIRouter(tags=["api"])


class QueryGenRequest(BaseModel):
    extra_prompt: Optional[str] = None
    model: Optional[str] = None


class SearchRequest(BaseModel):
    media: str = "video"          # video | photo | both
    per_query: int = 4            # クエリ×ソースごとの候補数
    sources: list[str] = ["pexels"]


class GenerateRequest(BaseModel):
    section: str
    prompt: str = ""              # 空ならセクションの代表クエリを使う
    style: str = "realistic"      # realistic | anime | lineart
    count: int = 2                # 1〜4枚
    provider: str = "comfy"       # comfy（ローカルSD） | nanobanana（Gemini API）


class AutoSelectRequest(BaseModel):
    model: Optional[str] = None


class SectionSelection(BaseModel):
    section: str
    candidate_ids: list[str]


class SelectRequest(BaseModel):
    selections: list[SectionSelection]


# ユーザー素材アップロードで受け付ける拡張子 → media_type
ALLOWED_UPLOAD_EXTS = {
    ".mp4": "video", ".mov": "video", ".webm": "video",
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo",
}


def _section_line_ids(project_id: str, episode_number: int, section: str) -> list[str]:
    """セクション名に対応するline_idsを footage_draft.json → script.json の順に解決する。"""
    if not section:
        return []
    draft = project_manager.read_footage_draft(project_id, episode_number)
    for s in (draft or {}).get("sections", []):
        if s.get("section") == section:
            return s.get("line_ids", [])
    script = project_manager.get_episode_script(project_id, episode_number)
    if script:
        for g in query_generator.group_lines_by_section(script):
            if g["section"] == section:
                return g["line_ids"]
    return []


# NanoBanana生成の素材候補プール（確定前の候補画像置き場、自己配信する）
NB_POOL = project_manager.SHARED_DIR / "footage_pool" / "nanobanana"
# ブラウザ/自分自身から候補画像を取得するための公開URL（ホスト公開ポート）
SCRAP_PUBLIC_URL = os.getenv("SCRAP_PUBLIC_URL", "http://localhost:8003")


async def _generate_nb_candidates(prompt: str, style_name: str, count: int) -> list[dict]:
    """NanoBananaで素材候補を生成し、プールに保存して候補形式で返す（16:9）。"""
    style = style_manager.get_style(style_name) or {}
    nb_prompt = style.get("prefix", "") + prompt
    NB_POOL.mkdir(parents=True, exist_ok=True)
    candidates = []
    for _ in range(max(1, min(count, 4))):
        data = await nanobanana_client.generate_one(nb_prompt, aspect="16:9")
        fname = f"nb_{uuid.uuid4().hex[:12]}.png"
        (NB_POOL / fname).write_bytes(data)
        url = f"{SCRAP_PUBLIC_URL}/imagegen/nb/{fname}"
        candidates.append({
            "candidate_id": f"ai_nb_{fname[3:-4]}",
            "media_type": "photo",
            "source": "ai",
            "query": prompt,
            "original_url": "",
            "download_url": url,       # selectは同一コンテナ内からlocalhostで取得できる
            "thumbnail_url": url,
            "duration_sec": 0.0,
            "resolution": "",
            "photographer": f"AI (nanobanana/{style_name})",
            "license": "ai-generated",
        })
    return candidates


def _recalc_metadata(footage: dict) -> None:
    clips = footage.get("clips", [])
    footage["metadata"] = {
        "clip_count": len(clips),
        "total_footage_duration_sec": round(sum(c.get("duration_sec", 0.0) for c in clips), 2),
        "all_downloaded": True,
    }


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "scrapping-agent",
        "pexels_key_configured": bool(os.getenv("PEXELS_API_KEY")),
        "pixabay_key_configured": bool(os.getenv("PIXABAY_API_KEY")),
        "llm_key_configured": bool(os.getenv("OPENROUTER_API_KEY")),
        "sources": {
            name: bool(os.getenv(cfg["env_key"])) for name, cfg in SOURCES.items()
        },
        "ai_reachable": await comfy_client.is_reachable(),
        "nanobanana_configured": nanobanana_client.is_configured(),
        "grok_configured": grok_image_client.is_configured(),
    }


@router.get("/vecteezy/account")
async def get_vecteezy_account():
    """Vecteezyのアカウント情報(ダウンロードクォータ残等)を返す。コスト監視用（読み取りのみ・課金なし）。"""
    try:
        return await vecteezy_client.account_info()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"vecteezy account_info failed: {e}")


@router.get("/imagegen/styles")
async def get_imagegen_styles():
    """画像生成スタイル定義（shared/imagegen/styles.json、無ければデフォルト生成）。"""
    return {"styles": style_manager.load_styles()}


@router.get("/panel/presets")
async def get_panel_presets():
    """紙芝居パネル生成の構造化入力プリセット（表情/ポーズ/ショット/アングル/シーン）。"""
    return {"presets": panel_presets.load_presets()}


@router.get("/imagegen/models")
async def get_imagegen_models():
    """ComfyUIが認識しているモデル一覧（checkpoint / LoRA / VAE）。"""
    if not await comfy_client.is_reachable():
        raise HTTPException(status_code=502, detail="imagegen-agent (ComfyUI) is not reachable")
    return await comfy_client.list_models()


class ModelDownloadRequest(BaseModel):
    url: str                      # Civitai / HuggingFace 等の直リンク
    kind: str = "lora"            # checkpoint | lora | vae
    filename: Optional[str] = None  # 省略時はURL末尾から推定


@router.post("/imagegen/models/download")
async def start_model_download(req: ModelDownloadRequest):
    """モデルファイルをURLからimagegen-agent/models/へバックグラウンドダウンロードする。"""
    if not model_downloader.is_mounted():
        raise HTTPException(
            status_code=503,
            detail="models volume not mounted (docker compose up -d for scrapping-agent required)",
        )
    try:
        return model_downloader.start(req.url, req.kind, req.filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/imagegen/nb/{filename}")
async def get_nb_pool_file(filename: str):
    """NanoBanana生成候補画像の配信（footage_pool/nanobanana/配下限定）。"""
    f = (NB_POOL / filename).resolve()
    if not f.is_relative_to(NB_POOL.resolve()) or not f.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {filename}")
    return FileResponse(f, media_type="image/png")


@router.get("/imagegen/models/downloads")
async def model_download_status():
    """進行中・完了済みダウンロードの状態一覧。"""
    return {"downloads": model_downloader.status_all()}


# ─── 🎨 自由生成スタジオ（台本に縛られない自由な画像生成） ─────────────────
#
# 素材タブ（台本セクション単位）/ キャラタブ（人物ライブラリ）とは別系統。
# 任意プロンプト＋任意参照画像から「状況に合わせた」画像を作る。
# 用途: 実在人物のイラスト化 / ニュース写真→イラスト化 / API規約で弾かれる
#       表現をローカルSD1.5で作る逃げ道、など。
# 出力は shared/direct_output/（自由生成のスクラッチ置き場）に確定保存する。

DIRECT_OUTPUT = project_manager.SHARED_DIR / "direct_output"
FREE_STAGING = DIRECT_OUTPUT / "_staging"  # 未保存の生成候補（ephemeral）
FREE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
FREE_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a"}
FREE_MEDIA_EXTS = FREE_IMAGE_EXTS | FREE_AUDIO_EXTS
_MEDIA_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
}


def _kind_of(name: str) -> str:
    return "audio" if Path(name).suffix.lower() in FREE_AUDIO_EXTS else "image"


def _free_candidate(name: str, provider: str, prompt: str) -> dict:
    """staging に保存済みの候補をフロント向け形式で返す。"""
    url = f"{SCRAP_PUBLIC_URL}/imagegen/free/staging/{name}"
    return {"id": name, "name": name, "provider": provider, "prompt": prompt,
            "kind": _kind_of(name), "url": url, "thumbnail_url": url}


def _sanitize_basename(name: str) -> str:
    """保存名をパス安全な basename に正規化（区切り・拡張子除去、空なら空文字）。"""
    base = Path((name or "").strip()).stem
    cleaned = "".join(c for c in base if c.isalnum() or c in (" ", "-", "_", ".")).strip()
    return cleaned.replace(" ", "_")[:80]


def _png_inject_itxt(png: bytes, keyword: str, text: str) -> bytes:
    """PNGに iTXt チャンク（UTF-8）を1つ挿入する。Pillow不要の純実装。

    keyword="parameters" にすると A1111系ツールの「PNG Info」でも読める。
    日本語プロンプト対応のため tEXt(Latin-1) ではなく iTXt(UTF-8) を使う。
    非PNG/壊れている場合は元データをそのまま返す（安全側）。挿入位置はIHDR直後。
    """
    sig = b"\x89PNG\r\n\x1a\n"
    if not png.startswith(sig):
        return png
    try:
        ihdr_len = struct.unpack(">I", png[8:12])[0]
        ihdr_end = 8 + 4 + 4 + ihdr_len + 4  # sig + (length + type + data + crc)
        kw = keyword.encode("latin-1", "replace")[:79]
        # iTXt body: keyword \0 compflag(0) compmethod(0) langtag \0 transkeyword \0 text(UTF-8)
        body = kw + b"\x00" + b"\x00" + b"\x00" + b"\x00" + b"\x00" + text.encode("utf-8")
        chunk = struct.pack(">I", len(body)) + b"iTXt" + body
        chunk += struct.pack(">I", zlib.crc32(b"iTXt" + body) & 0xFFFFFFFF)
        return png[:ihdr_end] + chunk + png[ihdr_end:]
    except Exception:
        return png


def _build_gen_params(*, provider: str, positive: str, negative: str = "",
                      style: str, mode: str, aspect: str = "", base_style: dict | None = None,
                      denoise: float | None = None, cn: dict | None = None,
                      seed: int | None = None) -> str:
    """生成情報を A1111 風の1テキストに整形する（PNG iTXt 埋め込み用）。"""
    lines = [positive.strip()]
    if negative.strip():
        lines.append(f"Negative prompt: {negative.strip()}")
    meta = [f"Provider: {provider}", f"Style: {style}", f"Mode: {mode}"]
    if provider == "comfy" and base_style:
        meta.append(f"Model: {base_style.get('checkpoint', '')}")
        meta.append(f"Size: {comfy_client.WIDTH}x{comfy_client.HEIGHT}")
        meta.append(f"Steps: {base_style.get('steps', 22)}")
        meta.append(f"CFG: {base_style.get('cfg', 7.0)}")
        meta.append(f"Sampler: {base_style.get('sampler', 'dpmpp_2m')} {base_style.get('scheduler', 'karras')}")
        if seed is not None:
            meta.append(f"Seed: {seed}")
        meta.append(f"Denoise: {denoise}")
        if cn:
            detail = f"strength {cn['strength']}, start {cn.get('start', 0.0)}, end {cn.get('end', 1.0)}"
            if cn.get("preprocess") == "canny":
                detail += f", low {cn['low']}, high {cn['high']}"
            meta.append(f"ControlNet: {cn['name']} [{cn.get('preprocess', 'tile')}] ({detail})")
    elif provider in ("nanobanana", "cloudflare", "runware", "grok"):
        meta.append(f"Aspect: {aspect}")
    lines.append(", ".join(meta))
    return "\n".join(lines)


@router.post("/imagegen/free/generate")
async def free_generate(
    provider: str = Form("nanobanana"),   # nanobanana | comfy
    mode: str = Form("t2i"),              # t2i | i2i（i2iは参照画像必須）
    prompt: str = Form(...),
    style: str = Form("realistic"),
    count: int = Form(2),
    aspect: str = Form("16:9"),           # nanobananaのみ自由（comfyは16:9固定）
    denoise: float = Form(0.6),           # comfy img2img: 0.4〜0.75目安
    controlnet: str = Form(""),           # comfy: ControlNetモデル名（指定時は構図固定t2i）
    cn_type: str = Form("tile"),          # 前処理: tile=画像全体で忠実変換 / canny=エッジで構図のみ固定
    cn_strength: float = Form(0.8),       # ControlNet適用強度
    cn_low: float = Form(0.4),            # Cannyエッジ検出 下限しきい値（上げると弱い線を捨てる＝細密写真向け）
    cn_high: float = Form(0.8),           # Cannyエッジ検出 上限しきい値
    cn_start: float = Form(0.0),          # ControlNet適用 開始位置（序盤を開放すると画風が乗る）
    cn_end: float = Form(1.0),            # ControlNet適用 終了位置（終盤を開放するとディテールが出る）
    labels: str = Form(""),               # 参照画像の役割（改行区切り・files と同順）
    files: list[UploadFile] = File(default=[]),
):
    """任意プロンプト＋任意参照画像から画像をcount枚生成し、staging候補として返す。

    参照画像は最大3枚。nanobananaは全枚を参照同梱、comfyは先頭1枚をimg2imgのinit_imageに使う。
    """
    provider = (provider or "nanobanana").lower()
    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is empty")
    base_style = style_manager.get_style(style)
    if base_style is None:
        raise HTTPException(status_code=400, detail=f"unknown style: {style}")
    count = max(1, min(int(count), 4))

    # 参照画像（最大3枚）をバイト列で読み込む。ラベルは files と lockstep。
    # 参照は i2i モードでのみ使う（t2i では無視＝純粋なテキスト生成）。
    raw_files = [f for f in (files or []) if f and (f.filename or "").strip()][:3]
    if mode == "i2i" and not raw_files:
        raise HTTPException(status_code=400, detail="i2i モードは参照画像が必要です")
    if mode != "i2i":
        raw_files = []
    label_list = [s.strip() for s in (labels or "").splitlines()]
    refs: list[tuple] = []
    for i, f in enumerate(raw_files):
        ext = Path(f.filename or "").suffix.lower()
        if ext not in FREE_IMAGE_EXTS:
            raise HTTPException(status_code=400, detail=f"unsupported image type: {ext}")
        data = await f.read()
        lab = label_list[i] if i < len(label_list) else ""
        refs.append((data, nanobanana_client.mime_for(f.filename or "x.png"), lab))

    nb_prompt = base_style.get("prefix", "") + prompt
    FREE_STAGING.mkdir(parents=True, exist_ok=True)
    candidates: list[dict] = []
    try:
        if provider == "nanobanana":
            if not nanobanana_client.is_configured():
                raise HTTPException(status_code=400, detail="NanoBanana (GEMINI/OPENROUTER key) is not configured")
            for _ in range(count):
                data = await nanobanana_client.generate_one(
                    nb_prompt, reference_images=refs or None, aspect=aspect,
                )
                name = f"free_{uuid.uuid4().hex[:12]}.png"
                params = _build_gen_params(provider="nanobanana", positive=nb_prompt,
                                           style=style, mode=mode, aspect=aspect)
                data = _png_inject_itxt(data, "parameters", params)
                (FREE_STAGING / name).write_bytes(data)
                candidates.append(_free_candidate(name, "nanobanana", prompt))
        elif provider == "comfy":
            if not await comfy_client.is_reachable():
                raise HTTPException(status_code=502, detail="imagegen-agent (ComfyUI) is not reachable")
            init_image = None
            cn = None
            if refs:
                uploaded = await comfy_client.upload_image(refs[0][0], f"free_{uuid.uuid4().hex[:8]}.png")
                if controlnet.strip():
                    # ControlNet: 構図固定のt2i。img2imgより忠実に絵柄変換できる。
                    # tile=画像全体を制御に渡し原画像へ忠実 / canny=エッジのみ（low/high=しきい値）。
                    lo = max(0.0, min(cn_low, 1.0))
                    hi = max(0.0, min(cn_high, 1.0))
                    if lo > hi:
                        lo, hi = hi, lo
                    st = max(0.0, min(cn_start, 1.0))
                    en = max(0.0, min(cn_end, 1.0))
                    if st > en:
                        st, en = en, st
                    cn = {"name": controlnet.strip(), "image": uploaded,
                          "preprocess": "canny" if cn_type == "canny" else "tile",
                          "strength": max(0.1, min(cn_strength, 2.0)),
                          "low": lo, "high": hi, "start": st, "end": en}
                else:
                    init_image = uploaded
            comfy_cands = await comfy_client.generate_with_style(
                prompt, base_style, count=count, style_name=style,
                init_image=init_image, denoise=max(0.05, min(denoise, 1.0)), controlnet=cn,
            )
            for cand in comfy_cands:
                name = f"free_{uuid.uuid4().hex[:12]}.png"
                dest = FREE_STAGING / name
                await pexels_client.download(cand["download_url"], dest)
                # 実効denoise: img2img（init有・CN無）のみスライダー値、それ以外（t2i/CN）は1.0
                eff_denoise = max(0.05, min(denoise, 1.0)) if (init_image is not None and not cn) else 1.0
                params = _build_gen_params(
                    provider="comfy", positive=base_style.get("prefix", "") + prompt,
                    negative=base_style.get("negative", ""), style=style, mode=mode,
                    base_style=base_style, denoise=eff_denoise,
                    cn=cn, seed=cand.get("seed"),
                )
                dest.write_bytes(_png_inject_itxt(dest.read_bytes(), "parameters", params))
                candidates.append(_free_candidate(name, "comfy", prompt))
        elif provider == "cloudflare":
            # Cloudflare Workers AI（FLUX.2 klein 等）: NanoBanana規約回避の外部フォールバック。
            # FLUXは自然言語モデルなのでSD用prefix/negativeは付けず、プロンプトをそのまま渡す。
            # 参照画像は最大4枚（クライアント側で512px以下へ縮小）。
            if not cloudflare_client.is_configured():
                raise HTTPException(status_code=400, detail="Cloudflare (CLOUDFLARE_API_KEY/ACCOUNT_ID) is not configured")
            for _ in range(count):
                data = await cloudflare_client.generate_one(
                    prompt, reference_images=refs or None, aspect=aspect,
                )
                name = f"free_{uuid.uuid4().hex[:12]}.png"
                params = _build_gen_params(provider="cloudflare", positive=prompt,
                                           style=style, mode=mode, aspect=aspect)
                data = _png_inject_itxt(data, "parameters", params)
                (FREE_STAGING / name).write_bytes(data)
                candidates.append(_free_candidate(name, "cloudflare", prompt))
        elif provider == "runware":
            # Runware（FLUX/SDXL）: 本命の規約回避プロバイダ。safety checker無し/任意なので
            # 有名人・政治家・故人など実在人物の肖像も生成できる（Cloudflareの3030回避）。
            # FLUXは自然言語モデルなのでSD用prefix/negativeは付けず、生promptをそのまま渡す。
            # 参照がある場合は先頭1枚を seedImage(i2i) に使い、denoiseを変化量(strength)に流用。
            if not runware_client.is_configured():
                raise HTTPException(status_code=400, detail="Runware (RUNWARE_API_KEY) is not configured")
            for _ in range(count):
                data = await runware_client.generate_one(
                    prompt, reference_images=refs or None, aspect=aspect,
                    strength=max(0.05, min(denoise, 1.0)),
                )
                name = f"free_{uuid.uuid4().hex[:12]}.png"
                params = _build_gen_params(provider="runware", positive=prompt,
                                           style=style, mode=mode, aspect=aspect)
                data = _png_inject_itxt(data, "parameters", params)
                (FREE_STAGING / name).write_bytes(data)
                candidates.append(_free_candidate(name, "runware", prompt))
        elif provider == "grok":
            # Grok（xAI Imagine）: 自然言語で指示する外部プロバイダ。参照ありは images/edits、
            # 参照なしは images/generations（t2i）。FLUX系同様 SD用prefix/negativeは付けない。
            if not grok_image_client.is_configured():
                raise HTTPException(status_code=400, detail="Grok (GROK_API_KEY) is not configured")
            for _ in range(count):
                data = await grok_image_client.generate_one(
                    prompt, reference_images=refs or None, aspect=aspect,
                )
                name = f"free_{uuid.uuid4().hex[:12]}.png"
                params = _build_gen_params(provider="grok", positive=prompt,
                                           style=style, mode=mode, aspect=aspect)
                data = _png_inject_itxt(data, "parameters", params)
                (FREE_STAGING / name).write_bytes(data)
                candidates.append(_free_candidate(name, "grok", prompt))
        else:
            raise HTTPException(status_code=400, detail=f"unknown provider: {provider}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"free generation failed: {e}")

    return {"provider": provider, "mode": mode, "prompt": prompt, "candidates": candidates}


class FreeAudioRequest(BaseModel):
    prompt: str


@router.post("/imagegen/free/audio/generate")
async def free_audio_generate(req: FreeAudioRequest):
    """Lyriaで BGM/効果音を1本生成し、staging候補（MP3）として返す。

    実験的機能。Google直叩き優先、クォータエラー時のみOpenRouterへ自動フォールバック（lyria_client 参照）。
    """
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is empty")
    if not lyria_client.is_configured():
        raise HTTPException(status_code=400, detail="Lyria (GEMINI_API_KEY or OPENROUTER_API_KEY) is not configured")
    FREE_STAGING.mkdir(parents=True, exist_ok=True)
    try:
        data = await lyria_client.generate_one(prompt)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"lyria generation failed: {e}")
    name = f"free_{uuid.uuid4().hex[:12]}.mp3"
    (FREE_STAGING / name).write_bytes(data)
    return {"provider": "lyria", "prompt": prompt, "candidates": [_free_candidate(name, "lyria", prompt)]}


class FreeSaveRequest(BaseModel):
    name: str                 # staging のファイル名
    save_name: str = ""       # 任意の確定名（拡張子・区切りは無視）


@router.post("/imagegen/free/save")
async def free_save(req: FreeSaveRequest):
    """staging の候補を direct_output/ に確定保存する（衝突時は連番付与）。"""
    src = (FREE_STAGING / Path(req.name).name).resolve()
    if not src.is_relative_to(FREE_STAGING.resolve()) or not src.is_file():
        raise HTTPException(status_code=404, detail=f"staging file not found: {req.name}")
    ext = src.suffix.lower() or ".png"
    base = _sanitize_basename(req.save_name) or datetime.now(timezone.utc).strftime("gen_%Y%m%d_%H%M%S")
    DIRECT_OUTPUT.mkdir(parents=True, exist_ok=True)
    dest = DIRECT_OUTPUT / f"{base}{ext}"
    idx = 1
    while dest.exists():
        dest = DIRECT_OUTPUT / f"{base}_{idx}{ext}"
        idx += 1
    shutil.move(str(src), str(dest))
    return {"saved": dest.name, "url": f"{SCRAP_PUBLIC_URL}/imagegen/free/file/{dest.name}"}


@router.get("/imagegen/free/outputs")
async def free_outputs():
    """direct_output/ に確定保存済みの自由生成画像一覧（新しい順）。"""
    if not DIRECT_OUTPUT.is_dir():
        return {"outputs": []}
    items = [
        p for p in DIRECT_OUTPUT.iterdir()
        if p.is_file() and p.suffix.lower() in FREE_MEDIA_EXTS
    ]
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {"outputs": [
        {"name": p.name, "size": p.stat().st_size,
         "modified": p.stat().st_mtime, "kind": _kind_of(p.name),
         "url": f"{SCRAP_PUBLIC_URL}/imagegen/free/file/{p.name}"}
        for p in items
    ]}


@router.get("/imagegen/free/staging/{filename}")
async def free_staging_file(filename: str):
    """生成候補（未保存）の配信（_staging 配下限定）。"""
    f = (FREE_STAGING / filename).resolve()
    if not f.is_relative_to(FREE_STAGING.resolve()) or not f.is_file():
        raise HTTPException(status_code=404, detail=f"staging file not found: {filename}")
    return FileResponse(f, media_type=_MEDIA_MIME.get(f.suffix.lower(), "application/octet-stream"))


@router.get("/imagegen/free/file/{filename}")
async def free_output_file(filename: str):
    """確定保存済み画像の配信（direct_output 直下限定）。"""
    f = (DIRECT_OUTPUT / filename).resolve()
    if not f.is_relative_to(DIRECT_OUTPUT.resolve()) or f.parent != DIRECT_OUTPUT.resolve() or not f.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {filename}")
    return FileResponse(f)


@router.delete("/imagegen/free/staging/{filename}")
async def free_discard_staging(filename: str):
    """生成候補を破棄する（_staging 配下限定）。"""
    f = (FREE_STAGING / filename).resolve()
    if not f.is_relative_to(FREE_STAGING.resolve()) or not f.is_file():
        raise HTTPException(status_code=404, detail=f"staging file not found: {filename}")
    f.unlink()
    return {"discarded": filename}


@router.delete("/imagegen/free/file/{filename}")
async def free_delete_output(filename: str):
    """確定保存済み画像を削除する（direct_output 直下限定）。"""
    f = (DIRECT_OUTPUT / filename).resolve()
    if not f.is_relative_to(DIRECT_OUTPUT.resolve()) or f.parent != DIRECT_OUTPUT.resolve() or not f.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {filename}")
    f.unlink()
    return {"deleted": filename}


@router.get("/projects")
async def get_projects():
    return {"projects": project_manager.list_projects()}


@router.get("/projects/{project_id}/episodes/{episode_number}/script")
async def get_episode_script(project_id: str, episode_number: int):
    """承認済みscript.jsonを返す（素材収集の入力確認用）。"""
    script = project_manager.get_episode_script(project_id, episode_number)
    if script is None:
        raise HTTPException(status_code=404, detail="approved script.json not found")
    return script


@router.get("/projects/{project_id}/episodes/{episode_number}/footage_draft")
async def get_footage_draft(project_id: str, episode_number: int):
    """作業中のfootage_draft.json（クエリ・候補・選択状態）を返す。"""
    draft = project_manager.read_footage_draft(project_id, episode_number)
    if draft is None:
        raise HTTPException(status_code=404, detail="footage_draft.json not found")
    return draft


@router.post("/projects/{project_id}/episodes/{episode_number}/queries")
async def generate_queries(project_id: str, episode_number: int, req: QueryGenRequest):
    """script.jsonからセクション別の素材検索クエリをLLMで生成し、footage_draft.jsonに保存する。"""
    script = project_manager.get_episode_script(project_id, episode_number)
    if script is None:
        raise HTTPException(status_code=404, detail="approved script.json not found")

    try:
        sections = await query_generator.generate_queries(
            script, extra_prompt=req.extra_prompt, model=req.model,
        )
    except Exception as e:
        project_manager.append_error(project_id, f"query generation failed: {e}")
        raise HTTPException(status_code=502, detail=f"query generation failed: {e}")

    draft = query_generator.build_draft(
        project_id, episode_number, sections, extra_prompt=req.extra_prompt,
    )
    if not project_manager.write_footage_draft(project_id, episode_number, draft):
        raise HTTPException(status_code=404, detail="episode directory not found")
    return draft


@router.post("/projects/{project_id}/episodes/{episode_number}/search")
async def search_candidates(project_id: str, episode_number: int, req: SearchRequest):
    """footage_draft.jsonのクエリでPexelsを検索し、候補をsections[].candidatesに保存して返す。"""
    draft = project_manager.read_footage_draft(project_id, episode_number)
    if draft is None:
        raise HTTPException(status_code=404, detail="footage_draft.json not found (run /queries first)")

    clients = []
    for name in req.sources:
        cfg = SOURCES.get(name)
        if cfg is None:
            raise HTTPException(status_code=400, detail=f"unknown source: {name}")
        if not os.getenv(cfg["env_key"]):
            raise HTTPException(status_code=400, detail=f"{cfg['env_key']} is not configured")
        clients.append(cfg["client"])
    if not clients:
        raise HTTPException(status_code=400, detail="no sources specified")

    async def search_one(query: str) -> list[dict]:
        results = []
        for client in clients:
            if req.media in ("video", "both"):
                results += await client.search_videos(query, per_page=req.per_query)
            if req.media in ("photo", "both"):
                results += await client.search_photos(query, per_page=req.per_query)
        return results

    try:
        for section in draft.get("sections", []):
            batches = await asyncio.gather(*(search_one(q) for q in section.get("queries", [])))
            # 検索結果は総入れ替えするが、AI生成済み候補（/generate由来）は温存する
            ai_candidates = [c for c in section.get("candidates", []) if c.get("source") == "ai"]
            seen: set[str] = set()
            candidates = []
            for batch in list(batches) + [ai_candidates]:
                for c in batch:
                    if c["candidate_id"] in seen:
                        continue
                    seen.add(c["candidate_id"])
                    candidates.append(c)
            section["candidates"] = candidates
    except Exception as e:
        project_manager.append_error(project_id, f"pexels search failed: {e}")
        raise HTTPException(status_code=502, detail=f"pexels search failed: {e}")

    draft["searched_at"] = datetime.now(timezone.utc).isoformat()
    project_manager.write_footage_draft(project_id, episode_number, draft)
    return draft


@router.post("/projects/{project_id}/episodes/{episode_number}/generate")
async def generate_ai_candidates(project_id: str, episode_number: int, req: GenerateRequest):
    """セクション単位でAI画像をピンポイント生成し、候補に追記する。

    素材サイトに無いもの（爆破シーン等）を追加プロンプトで狙って作る用途。
    既存候補は消さず追記する。
    """
    draft = project_manager.read_footage_draft(project_id, episode_number)
    if draft is None:
        raise HTTPException(status_code=404, detail="footage_draft.json not found (run /queries first)")
    if style_manager.get_style(req.style) is None:
        raise HTTPException(status_code=400, detail=f"unknown style: {req.style}")
    if req.provider == "comfy" and not await comfy_client.is_reachable():
        raise HTTPException(status_code=502, detail="imagegen-agent (ComfyUI) is not reachable")
    if req.provider == "nanobanana" and not nanobanana_client.is_configured():
        raise HTTPException(status_code=400, detail="GEMINI_API_KEY is not configured")

    section = next((s for s in draft.get("sections", []) if s["section"] == req.section), None)
    if section is None:
        raise HTTPException(status_code=400, detail=f"unknown section: {req.section}")

    prompt = req.prompt.strip() or (section.get("queries") or [section.get("summary", "")])[0]
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is empty and section has no queries")

    try:
        if req.provider == "nanobanana":
            new_candidates = await _generate_nb_candidates(prompt, req.style, req.count)
        else:
            new_candidates = await comfy_client.generate(prompt, count=req.count, style_name=req.style)
    except Exception as e:
        project_manager.append_error(project_id, f"ai generation failed: {e}")
        raise HTTPException(status_code=502, detail=f"ai generation failed: {e}")

    existing_ids = {c["candidate_id"] for c in section.get("candidates", [])}
    section.setdefault("candidates", []).extend(
        c for c in new_candidates if c["candidate_id"] not in existing_ids
    )
    project_manager.write_footage_draft(project_id, episode_number, draft)
    return {"section": req.section, "prompt": prompt, "added": new_candidates, "draft": draft}


@router.post("/projects/{project_id}/episodes/{episode_number}/auto_select")
async def auto_select_candidates(project_id: str, episode_number: int, req: AutoSelectRequest):
    """LLMが候補から尺・意味に合う素材を自動選択する（確定は人間の承認で行う）。"""
    draft = project_manager.read_footage_draft(project_id, episode_number)
    if draft is None:
        raise HTTPException(status_code=404, detail="footage_draft.json not found")
    if not any(s.get("candidates") for s in draft.get("sections", [])):
        raise HTTPException(status_code=400, detail="no candidates to select from (run /search first)")

    try:
        result = await auto_selector.auto_select(project_id, episode_number, draft, model=req.model)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        project_manager.append_error(project_id, f"auto select failed: {e}")
        raise HTTPException(status_code=502, detail=f"auto select failed: {e}")
    return result


@router.post("/projects/{project_id}/episodes/{episode_number}/select")
async def select_and_download(project_id: str, episode_number: int, req: SelectRequest):
    """採用候補をダウンロードし、DATA_SCHEMA準拠のfootage.jsonを確定する。"""
    draft = project_manager.read_footage_draft(project_id, episode_number)
    if draft is None:
        raise HTTPException(status_code=404, detail="footage_draft.json not found")
    ep_dir = project_manager.episode_dir(project_id, episode_number)
    if ep_dir is None:
        raise HTTPException(status_code=404, detail="episode directory not found")

    sections = {s["section"]: s for s in draft.get("sections", [])}
    chosen: list[tuple[dict, dict]] = []  # (section, candidate)
    for sel in req.selections:
        section = sections.get(sel.section)
        if section is None:
            raise HTTPException(status_code=400, detail=f"unknown section: {sel.section}")
        by_id = {c["candidate_id"]: c for c in section.get("candidates", [])}
        for cid in sel.candidate_ids:
            if cid not in by_id:
                raise HTTPException(status_code=400, detail=f"unknown candidate: {cid}")
            chosen.append((section, by_id[cid]))
        section["selected"] = sel.candidate_ids

    project_manager.update_footage_status(project_id, episode_number, "running")

    clips = []
    try:
        for i, (section, cand) in enumerate(chosen, start=1):
            if cand["media_type"] == "video":
                ext = ".mp4"
            elif cand["source"] == "ai":
                ext = ".png"   # ComfyUIの出力はPNG
            else:
                ext = ".jpg"
            filename = f"clip_{i:03d}{ext}"
            # vecteezyは確定時に署名付きURLを解決する2段階方式（検索時点ではURL無し・課金保護）
            download_url = cand["download_url"]
            if cand["source"] == "vecteezy" and not download_url:
                download_url = await vecteezy_client.resolve_download_url(cand)
            await pexels_client.download(download_url, ep_dir / "footage" / filename)
            if cand["source"] == "ai":
                # AI生成素材は再利用プールにもコピー（candidate_idで一意なファイル名）
                pool = project_manager.SHARED_DIR / "footage_pool" / "ai_generated"
                pool.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ep_dir / "footage" / filename, pool / f"{cand['candidate_id']}{ext}")
            clips.append({
                "id": f"clip_{i:03d}",
                "candidate_id": cand["candidate_id"],
                "section": section["section"],
                "line_ids": section.get("line_ids", []),
                "type": "stock",
                "media_type": cand["media_type"],
                "source": cand["source"],
                "original_url": cand["original_url"],
                "file_path": f"footage/{filename}",
                "duration_sec": cand.get("duration_sec", 0.0),
                "resolution": cand.get("resolution", ""),
                "keywords": [cand.get("query", "")],
                "license": cand.get("license", ""),
                "notes": "",
            })
    except Exception as e:
        project_manager.update_footage_status(project_id, episode_number, "error")
        project_manager.append_error(project_id, f"footage download failed: {e}")
        raise HTTPException(status_code=502, detail=f"footage download failed: {e}")

    # 既存footage.jsonのユーザーアップロード素材（type=user）は温存する
    existing = project_manager.read_footage(project_id, episode_number) or {}
    user_clips = [c for c in existing.get("clips", []) if c.get("type") == "user"]

    footage = {
        "schema_version": "1.0.0",
        "project_id": draft.get("project_id", project_id),
        "episode": episode_number,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "clips": clips + user_clips,
    }
    _recalc_metadata(footage)

    # 再選択でクリップ数が減った場合、参照されなくなった旧stockファイルを削除
    referenced = {c["file_path"].split("/")[-1] for c in footage["clips"]}
    for p in (ep_dir / "footage").glob("clip_*"):
        if p.name not in referenced:
            p.unlink()

    project_manager.write_footage(project_id, episode_number, footage)
    project_manager.write_footage_draft(project_id, episode_number, draft)
    project_manager.update_footage_status(project_id, episode_number, "done")
    return footage


@router.get("/projects/{project_id}/episodes/{episode_number}/footage")
async def get_footage(project_id: str, episode_number: int):
    """確定済みfootage.jsonを返す。"""
    ep_dir = project_manager.episode_dir(project_id, episode_number)
    if ep_dir is None or not (ep_dir / "footage.json").exists():
        raise HTTPException(status_code=404, detail="footage.json not found")
    return json.loads((ep_dir / "footage.json").read_text(encoding="utf-8"))


@router.get("/projects/{project_id}/episodes/{episode_number}/footage/file/{filename}")
async def get_footage_file(project_id: str, episode_number: int, filename: str):
    """確定済み素材ファイルを配信する（UIのサムネイル・プレビュー用）。"""
    ep_dir = project_manager.episode_dir(project_id, episode_number)
    if ep_dir is None:
        raise HTTPException(status_code=404, detail="episode directory not found")
    f = (ep_dir / "footage" / filename).resolve()
    # footage/配下限定（パストラバーサル防止）
    if not f.is_relative_to((ep_dir / "footage").resolve()) or not f.is_file():
        raise HTTPException(status_code=404, detail=f"footage file not found: {filename}")
    media_types = {
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    }
    return FileResponse(f, media_type=media_types.get(f.suffix.lower(), "application/octet-stream"))


@router.post("/projects/{project_id}/episodes/{episode_number}/upload")
async def upload_user_footage(
    project_id: str,
    episode_number: int,
    file: UploadFile = File(...),
    section: str = Form(""),
):
    """ユーザー所有の素材ファイルを登録する（footage/user_NNN.* に保存しfootage.jsonに追記）。"""
    ep_dir = project_manager.episode_dir(project_id, episode_number)
    if ep_dir is None:
        raise HTTPException(status_code=404, detail="episode directory not found")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type: {ext} (allowed: {', '.join(ALLOWED_UPLOAD_EXTS)})",
        )

    footage_dir = ep_dir / "footage"
    footage_dir.mkdir(parents=True, exist_ok=True)

    # user_NNN の次の空き番号を採番（拡張子違いの衝突も避ける）
    used = {p.stem for p in footage_dir.glob("user_*")}
    idx = 1
    while f"user_{idx:03d}" in used:
        idx += 1
    filename = f"user_{idx:03d}{ext}"

    data = await file.read()
    (footage_dir / filename).write_bytes(data)

    footage = project_manager.read_footage(project_id, episode_number) or {
        "schema_version": "1.0.0",
        "project_id": project_id,
        "episode": episode_number,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "clips": [],
    }
    clip = {
        "id": f"user_{idx:03d}",
        "section": section,
        "line_ids": _section_line_ids(project_id, episode_number, section),
        "type": "user",
        "media_type": ALLOWED_UPLOAD_EXTS[ext],
        "source": "user_upload",
        "original_url": "",
        "file_path": f"footage/{filename}",
        "duration_sec": 0.0,
        "resolution": "",
        "keywords": [],
        "license": "user",
        "notes": file.filename or "",
    }
    footage["clips"].append(clip)
    _recalc_metadata(footage)
    project_manager.write_footage(project_id, episode_number, footage)
    project_manager.update_footage_status(project_id, episode_number, "done")
    return {"clip": clip, "footage": footage}


@router.delete("/projects/{project_id}/episodes/{episode_number}/footage/clip/{clip_id}")
async def delete_footage_clip(project_id: str, episode_number: int, clip_id: str):
    """footage.jsonからクリップを削除し、素材ファイルも削除する。"""
    ep_dir = project_manager.episode_dir(project_id, episode_number)
    footage = project_manager.read_footage(project_id, episode_number)
    if ep_dir is None or footage is None:
        raise HTTPException(status_code=404, detail="footage.json not found")

    clips = footage.get("clips", [])
    target = next((c for c in clips if c.get("id") == clip_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"clip not found: {clip_id}")

    # ファイル削除はepisodes/epNN/footage/配下のみ許可（パストラバーサル防止）
    rel = target.get("file_path", "")
    f = (ep_dir / rel).resolve()
    if f.is_relative_to((ep_dir / "footage").resolve()) and f.exists():
        f.unlink()

    footage["clips"] = [c for c in clips if c.get("id") != clip_id]
    _recalc_metadata(footage)
    project_manager.write_footage(project_id, episode_number, footage)
    return {"deleted": clip_id, "footage": footage}


# =====================================================================
# キャラクター画像生成（Phase 1: API基盤）
# 台帳は shared/characters/{char_id}/ — footage系とは完全に別系統で管理する
# =====================================================================

class CharacterCreateRequest(BaseModel):
    char_id: str
    name: str
    appearance_prompt: str        # キャラシート（外見の固定プロンプト、一貫性の核）
    description: str = ""
    caption: str = ""             # 字幕表示名（空ならnameを使う）
    voice: Optional[dict] = None  # {engine, voice_id} 声の本籍


class CharacterPatchRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    appearance_prompt: Optional[str] = None
    provider: Optional[str] = None
    caption: Optional[str] = None
    voice: Optional[dict] = None   # {engine, voice_id} 指定時は丸ごと置換
    styles: Optional[dict] = None  # {style: {seed, loras, extra_prompt}} 部分更新


class CharacterGenerateRequest(BaseModel):
    style: str = "comic"          # comic | realistic | deformed（styles.json参照）
    expression: str = "base"      # 表情・ポーズのスラッグ（ファイル名に入る）
    prompt_extra: str = ""        # 今回だけの追加指示（ポーズ・小物等）
    count: int = 2                # 1〜4枚
    seed: Optional[int] = None    # 未指定ならキャラのスタイル別固定seed→無ければランダム
    reference: Optional[str] = None  # reference/内のファイル名。指定でimg2img参照生成（comfy単一）
    # 役割ラベル付き複数参照（NanoBanana用）。[{filename, label}] 順序＝Image順、最大3枚。
    references: Optional[list[dict]] = None
    denoise: float = 0.6          # img2img時の変化量（低い=参照に忠実、0.4〜0.75目安）
    provider: Optional[str] = None  # comfy | nanobanana（省略時はキャラのprovider設定）
    aspect: str = "1:1"           # nanobanana時のアスペクト比（1:1 | 16:9 | 9:16 | 3:4 | 4:3）


# 参照画像アップロードで受け付ける拡張子
ALLOWED_REF_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}


@router.get("/characters")
async def list_characters():
    return {"characters": character_manager.list_characters()}


@router.post("/characters")
async def create_character(req: CharacterCreateRequest):
    if not character_manager.valid_id(req.char_id):
        raise HTTPException(status_code=400, detail="char_id must match [a-z0-9][a-z0-9_-]*")
    if character_manager.read_character(req.char_id) is not None:
        raise HTTPException(status_code=409, detail=f"character already exists: {req.char_id}")
    return character_manager.create_character(
        req.char_id, req.name, req.appearance_prompt, req.description,
        caption=req.caption, voice=req.voice,
    )


@router.get("/characters/{char_id}")
async def get_character(char_id: str):
    char = character_manager.read_character(char_id)
    if char is None:
        raise HTTPException(status_code=404, detail=f"character not found: {char_id}")
    return char


@router.patch("/characters/{char_id}")
async def patch_character(char_id: str, req: CharacterPatchRequest):
    char = character_manager.read_character(char_id)
    if char is None:
        raise HTTPException(status_code=404, detail=f"character not found: {char_id}")
    for field in ("name", "description", "appearance_prompt", "provider", "caption"):
        v = getattr(req, field)
        if v is not None:
            char[field] = v
    if req.voice is not None:
        char["voice"] = {"engine": req.voice.get("engine", ""), "voice_id": req.voice.get("voice_id", "")}
    if req.styles:
        for style, cfg in req.styles.items():
            char.setdefault("styles", {}).setdefault(style, {}).update(cfg or {})
    character_manager.write_character(char)
    return char


@router.delete("/characters/{char_id}")
async def delete_character(char_id: str):
    d = character_manager.char_dir(char_id)
    if not character_manager.valid_id(char_id) or not (d / "character.json").exists():
        raise HTTPException(status_code=404, detail=f"character not found: {char_id}")
    shutil.rmtree(d)
    return {"deleted": char_id}


# 声カタログのフラット運用（1音声=1声、stem=voice_id）に合わせた拡張子探索
VOICE_EXTS = [".wav", ".flac", ".mp3", ".m4a", ".ogg"]


def _find_voice_file(engine: str, voice_id: str) -> Optional[Path]:
    if not engine or not voice_id:
        return None
    voices_dir = character_manager.SHARED_DIR / "voices" / engine
    for ext in VOICE_EXTS:
        f = voices_dir / f"{voice_id}{ext}"
        if f.exists():
            return f
    return None


@router.get("/characters/{char_id}/export")
async def export_character(char_id: str):
    """キャラをzipバンドル（character.json + reference/）でエクスポートする。

    generations（生成履歴）はgenerated/側のファイルを伴わないと無意味なため除外する。
    """
    char = character_manager.read_character(char_id)
    if char is None:
        raise HTTPException(status_code=404, detail=f"character not found: {char_id}")

    export_char = {k: v for k, v in char.items() if k != "generations"}
    ref_dir = character_manager.char_dir(char_id) / "reference"
    voice = char.get("voice") or {}
    voice_file = _find_voice_file(voice.get("engine", ""), voice.get("voice_id", ""))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("character.json", json.dumps(export_char, ensure_ascii=False, indent=2))
        if ref_dir.exists():
            for p in sorted(ref_dir.iterdir()):
                if p.is_file():
                    zf.writestr(f"reference/{p.name}", p.read_bytes())
        if voice_file:
            zf.writestr(f"voice/{voice_file.name}", voice_file.read_bytes())
    buf.seek(0)

    filename = f"{char_id}.character.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/characters/import")
async def import_character(
    file: UploadFile = File(...),
    new_char_id: Optional[str] = Form(None),
):
    """キャラzipバンドルをインポートする。char_id衝突時は拒否（new_char_idで別ID取り込み可）。"""
    raw = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="zipファイルとして読み込めません")

    try:
        char_raw = zf.read("character.json")
    except KeyError:
        raise HTTPException(status_code=400, detail="character.json が見つかりません")
    try:
        char = json.loads(char_raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="character.json が不正なJSONです")

    required = {"char_id", "name", "appearance_prompt"}
    if not required.issubset(char.keys()):
        raise HTTPException(status_code=400, detail=f"character.json に必須キーが不足しています: {sorted(required)}")

    char_id = (new_char_id or "").strip() or char["char_id"]
    if not character_manager.valid_id(char_id):
        raise HTTPException(status_code=400, detail="char_id must match [a-z0-9][a-z0-9_-]*")
    if character_manager.read_character(char_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"character already exists: {char_id}（new_char_id を指定して別IDで取り込めます）",
        )

    char["char_id"] = char_id
    char["generations"] = []  # 生成履歴は実体(generated/)を伴わないため取り込まない
    # 欠落フィールドの補完と schema_version の現行値スタンプは
    # write_character → character_manager.normalize_character が一元的に担う。

    ref_names = [n for n in zf.namelist() if n.startswith("reference/") and not n.endswith("/")]
    voice_names = [n for n in zf.namelist() if n.startswith("voice/") and not n.endswith("/")]

    character_manager.write_character(char)
    ref_dir = character_manager.char_dir(char_id) / "reference"
    saved = []
    for name in ref_names:
        fname = Path(name).name
        if not fname:
            continue
        (ref_dir / fname).write_bytes(zf.read(name))
        saved.append(fname)

    # 声カタログは全キャラ共通のグローバル名前空間。既存ファイルがあれば上書きせず流用する。
    engine = (char.get("voice") or {}).get("engine", "")
    voice_bundled = False
    if engine and voice_names:
        voices_dir = character_manager.SHARED_DIR / "voices" / engine
        voices_dir.mkdir(parents=True, exist_ok=True)
        for name in voice_names:
            fname = Path(name).name
            if not fname:
                continue
            dest = voices_dir / fname
            if not dest.exists():
                dest.write_bytes(zf.read(name))
            voice_bundled = True

    char["reference_meta"] = {k: v for k, v in char.get("reference_meta", {}).items() if k in saved}
    character_manager.write_character(char)

    return {"char_id": char_id, "status": "imported", "reference_count": len(saved), "voice_bundled": voice_bundled}


def _resolve_ref_file(char_id: str, name: str) -> Path:
    """reference/配下のファイルを解決する（パストラバーサル防止）。"""
    ref_dir = (character_manager.char_dir(char_id) / "reference").resolve()
    f = (ref_dir / name).resolve()
    if not f.is_relative_to(ref_dir) or not f.is_file():
        raise HTTPException(status_code=404, detail=f"reference image not found: {name}")
    return f


def _resolve_ref_files(char_id: str, reference: Optional[str] = None, limit: int = 3) -> list[Path]:
    """参照画像を解決する。明示指定があればそれのみ、無ければreference/全画像（新しい順に最大limit枚）。"""
    if reference:
        return [_resolve_ref_file(char_id, reference)]
    ref_dir = (character_manager.char_dir(char_id) / "reference").resolve()
    return sorted(
        (p for p in ref_dir.glob("*") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:limit]


def _resolve_labeled_refs(
    char_id: str, references: Optional[list[dict]] = None, limit: int = 3,
) -> list[tuple[Path, str]]:
    """役割ラベル付き参照を解決する（NanoBanana用）。

    references=[{filename, label}] が来たら与えられた順に (Path, label)（最大limit）。
    未指定なら従来 _resolve_ref_files（新しい順）にラベル空で揃える。
    """
    if references:
        out: list[tuple[Path, str]] = []
        for item in references[:limit]:
            fn = (item or {}).get("filename")
            if not fn:
                continue
            out.append((_resolve_ref_file(char_id, fn), (item.get("label") or "").strip()))
        return out
    return [(p, "") for p in _resolve_ref_files(char_id, limit=limit)]


async def _nb_generate_and_save(
    char_id: str, prompt: str, refs: list[tuple[bytes, str]],
    aspect: str, count: int, style: str, expression: str, extra_meta: dict | None = None,
) -> list[dict]:
    """NanoBananaでcount枚生成し generated/ に保存、generations追記、保存情報を返す。"""
    images = []
    for _ in range(max(1, min(count, 4))):
        images.append(await nanobanana_client.generate_one(prompt, refs, aspect=aspect))
    gen_dir = character_manager.char_dir(char_id) / "generated"
    gen_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for data in images:
        filename = character_manager.next_filename(char_id, style, expression)
        (gen_dir / filename).write_bytes(data)
        entry = {
            "filename": filename, "style": style, "expression": expression,
            "seed": None, "prompt": prompt, "provider": "nanobanana",
        }
        if extra_meta:
            entry.update(extra_meta)
        character_manager.append_generation(char_id, entry)
        saved.append({**entry, "url": f"/characters/{char_id}/file/generated/{filename}"})
    return saved


@router.post("/characters/{char_id}/generate")
async def generate_character_images(char_id: str, req: CharacterGenerateRequest):
    """キャラシート＋スタイル別設定で画像を生成し、generated/に命名規則で保存する。"""
    char = character_manager.read_character(char_id)
    if char is None:
        raise HTTPException(status_code=404, detail=f"character not found: {char_id}")
    base_style = style_manager.get_style(req.style)
    if base_style is None:
        raise HTTPException(status_code=400, detail=f"unknown style: {req.style}")
    provider = (req.provider or char.get("provider") or "comfy").lower()

    # キャラのスタイル別設定（seed / LoRA / 追加プロンプト）をstyles.json定義に重ねる
    char_style = (char.get("styles") or {}).get(req.style, {})
    prompt_parts = [
        char.get("appearance_prompt", ""),
        char_style.get("extra_prompt", ""),
        req.expression if req.expression not in ("", "base") else "",
        req.prompt_extra,
    ]
    prompt = ", ".join(p.strip() for p in prompt_parts if p and p.strip())

    if provider == "nanobanana":
        # === NanoBanana: 参照画像同梱でキャラ一貫性を担保（seedは無い） ===
        if not nanobanana_client.is_configured():
            raise HTTPException(status_code=400, detail="GEMINI_API_KEY is not configured")
        # 参照解決: references[{filename,label}]優先 → 単一reference → reference/新しい順3枚
        labeled = _resolve_labeled_refs(
            char_id,
            req.references or ([{"filename": req.reference}] if req.reference else None),
        )
        refs = [(p.read_bytes(), nanobanana_client.mime_for(p.name), label) for p, label in labeled]
        ref_files = [p for p, _ in labeled]
        # スタイルはプロンプト接頭辞のみ流用（checkpoint/LoRAはローカルSD専用）
        nb_prompt = base_style.get("prefix", "") + prompt
        meta = {"reference": ";".join(p.name for p in ref_files), "denoise": None}
        try:
            saved = await _nb_generate_and_save(
                char_id, nb_prompt, refs, req.aspect, req.count, req.style, req.expression, extra_meta=meta,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"character generation failed: {e}")
        return {"char_id": char_id, "prompt": nb_prompt, "provider": provider, "generated": saved}

    # === ローカルSD（ComfyUI） ===
    if not await comfy_client.is_reachable():
        raise HTTPException(status_code=502, detail="imagegen-agent (ComfyUI) is not reachable")

    # 参照画像（img2img）: ComfyUIはinit_image 1枚のみ。references指定時は先頭を採用。
    single_ref = req.reference
    if not single_ref and req.references:
        single_ref = (req.references[0] or {}).get("filename")
    init_image = None
    if single_ref:
        ref = _resolve_ref_file(char_id, single_ref)
        try:
            init_image = await comfy_client.upload_image(
                ref.read_bytes(), f"charref_{char_id}_{ref.name}"
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"reference upload to ComfyUI failed: {e}")

    style = dict(base_style)
    if char_style.get("loras"):
        style["loras"] = char_style["loras"]
    seed = req.seed if req.seed is not None else char_style.get("seed")

    try:
        candidates = await comfy_client.generate_with_style(
            prompt, style, count=req.count, style_name=req.style, seed=seed,
            init_image=init_image, denoise=max(0.05, min(req.denoise, 1.0)),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"character generation failed: {e}")

    gen_dir = character_manager.char_dir(char_id) / "generated"
    gen_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for cand in candidates:
        filename = character_manager.next_filename(char_id, req.style, req.expression)
        await pexels_client.download(cand["download_url"], gen_dir / filename)
        entry = {
            "filename": filename,
            "style": req.style,
            "expression": req.expression,
            "seed": cand["seed"],
            "prompt": prompt,
            "provider": "comfy",
            "reference": single_ref or "",
            "denoise": req.denoise if single_ref else None,
        }
        character_manager.append_generation(char_id, entry)
        saved.append({**entry, "url": f"/characters/{char_id}/file/generated/{filename}"})
    return {"char_id": char_id, "prompt": prompt, "provider": provider, "generated": saved}


class PanelGenerateRequest(BaseModel):
    emotion_id: str = ""
    pose_id: str = ""
    shot_id: str = ""
    angle_id: str = ""
    scene_id: str = ""
    background_mode: str = "flat"      # scene | flat | transparent
    style: str = "kamishibai"
    aspect: str = "16:9"               # 紙芝居は基本16:9（4:3/3:4/1:1も可）
    count: int = 1
    extra_prompt: str = ""
    reference: Optional[str] = None    # reference/内のファイル名。未指定なら新しい順に最大3枚
    # 役割ラベル付き複数参照（NanoBanana用）。[{filename, label}] 順序＝Image順、最大3枚。
    references: Optional[list[dict]] = None
    # 任意のプレースホルダ出所（記録のみ・生成には使わない）
    script_ref: Optional[dict] = None  # {project_id, episode, line_id}


@router.post("/characters/{char_id}/panel")
async def generate_panel(char_id: str, req: PanelGenerateRequest):
    """構造化入力（表情/ポーズ/ショット/アングル/シーン）から紙芝居パネルを生成する。
    一貫性は既存のNanoBanana参照画像同梱で担保する。"""
    char = character_manager.read_character(char_id)
    if char is None:
        raise HTTPException(status_code=404, detail=f"character not found: {char_id}")
    # per-capability ゲート: 外見プロンプトが無いキャラは紙芝居を生成できない
    #（声だけのキャラ等。各能力フィールドは独立に任意＝NULLキャラを作らない設計）。
    if not (char.get("appearance_prompt") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="このキャラは外見(appearance_prompt)が未設定のため紙芝居を生成できません。🎭キャラタブで外見を設定してください",
        )
    if not nanobanana_client.is_configured():
        raise HTTPException(status_code=400, detail="NanoBanana (GEMINI/OPENROUTER key) is not configured")
    base_style = style_manager.get_style(req.style)
    if base_style is None:
        raise HTTPException(status_code=400, detail=f"unknown style: {req.style}")

    prompt = panel_presets.build_panel_prompt(
        char.get("appearance_prompt", ""), base_style.get("prefix", ""),
        emotion_id=req.emotion_id, pose_id=req.pose_id, shot_id=req.shot_id,
        angle_id=req.angle_id, scene_id=req.scene_id,
        background_mode=req.background_mode, extra_prompt=req.extra_prompt,
    )

    # 参照画像（一貫性の核）: 既存 generate_character_images と同じ解決ロジックを使う
    labeled = _resolve_labeled_refs(
        char_id,
        req.references or ([{"filename": req.reference}] if req.reference else None),
    )
    refs = [(p.read_bytes(), nanobanana_client.mime_for(p.name), label) for p, label in labeled]
    ref_files = [p for p, _ in labeled]

    expr = panel_presets.slug(req.emotion_id, req.pose_id, req.shot_id)
    meta = {
        "reference": ";".join(p.name for p in ref_files),
        "denoise": None,
        "panel": {  # 構造化メタを履歴に残す（後で再現・絞り込みできる）
            "emotion_id": req.emotion_id, "pose_id": req.pose_id, "shot_id": req.shot_id,
            "angle_id": req.angle_id, "scene_id": req.scene_id,
            "background_mode": req.background_mode, "aspect": req.aspect,
            "script_ref": req.script_ref,
        },
    }
    try:
        saved = await _nb_generate_and_save(
            char_id, prompt, refs, req.aspect, req.count, req.style, expr, extra_meta=meta,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"panel generation failed: {e}")
    return {"char_id": char_id, "prompt": prompt, "generated": saved}


@router.post("/characters/{char_id}/reference/upload")
async def upload_reference(char_id: str, file: UploadFile = File(...)):
    """手持ちのキャラクターシート等を参照画像として登録する（reference/upload_NNN.*）。"""
    char = character_manager.read_character(char_id)
    if char is None:
        raise HTTPException(status_code=404, detail=f"character not found: {char_id}")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_REF_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported image type: {ext} (allowed: {', '.join(sorted(ALLOWED_REF_EXTS))})",
        )
    ref_dir = character_manager.char_dir(char_id) / "reference"
    ref_dir.mkdir(parents=True, exist_ok=True)
    used = {p.stem for p in ref_dir.glob("upload_*")}
    idx = 1
    while f"upload_{idx:03d}" in used:
        idx += 1
    filename = f"upload_{idx:03d}{ext}"
    (ref_dir / filename).write_bytes(await file.read())
    return {"char_id": char_id, "reference": filename, "original_name": file.filename or ""}


@router.delete("/characters/{char_id}/reference/{filename}")
async def delete_reference(char_id: str, filename: str):
    """参照画像を削除する（reference/配下限定）。ラベル overlay も同時に掃除する。"""
    ref_dir = (character_manager.char_dir(char_id) / "reference").resolve()
    f = (ref_dir / filename).resolve()
    if not f.is_relative_to(ref_dir) or not f.is_file():
        raise HTTPException(status_code=404, detail=f"reference not found: {filename}")
    f.unlink()
    character_manager.set_reference_label(char_id, filename, "")  # overlayのキーも除去
    return {"char_id": char_id, "deleted": filename}


class ReferenceLabelRequest(BaseModel):
    label: str = ""


@router.patch("/characters/{char_id}/reference/{filename}")
async def set_reference_label(char_id: str, filename: str, req: ReferenceLabelRequest):
    """参照画像に役割ラベルを付与/更新する（NanoBananaの Image N 対応付け用）。"""
    # ファイル実体の存在確認（パストラバーサル防止込み）
    _resolve_ref_file(char_id, filename)
    if not character_manager.set_reference_label(char_id, filename, req.label):
        raise HTTPException(status_code=404, detail=f"character not found: {char_id}")
    return {"char_id": char_id, "reference": filename, "label": req.label.strip()}


@router.post("/characters/{char_id}/reference/{filename}")
async def promote_reference(char_id: str, filename: str):
    """生成画像をリファレンス（確定見本）に昇格コピーする。Phase 3でNanoBanana参照入力に使う。"""
    d = character_manager.char_dir(char_id)
    src = (d / "generated" / filename).resolve()
    if not src.is_relative_to((d / "generated").resolve()) or not src.is_file():
        raise HTTPException(status_code=404, detail=f"generated image not found: {filename}")
    (d / "reference").mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, d / "reference" / filename)
    return {"char_id": char_id, "reference": filename}


@router.delete("/characters/{char_id}/generated/{filename}")
async def delete_generated(char_id: str, filename: str):
    """生成画像を削除する（generated/配下限定）。character.json の履歴も同時に除去する。

    reference/ に昇格済みのコピーは残す（参照見本は別管理＝意図通り）。
    """
    gen_dir = (character_manager.char_dir(char_id) / "generated").resolve()
    f = (gen_dir / filename).resolve()
    if not f.is_relative_to(gen_dir) or not f.is_file():
        raise HTTPException(status_code=404, detail=f"generated image not found: {filename}")
    f.unlink()
    character_manager.delete_generation(char_id, filename)
    return {"char_id": char_id, "deleted": filename}


@router.get("/characters/{char_id}/file/{kind}/{filename}")
async def get_character_file(char_id: str, kind: str, filename: str):
    """キャラ画像の配信（generated / reference 配下限定、パストラバーサル防止）。"""
    if kind not in ("generated", "reference"):
        raise HTTPException(status_code=404, detail="kind must be generated or reference")
    base = (character_manager.char_dir(char_id) / kind).resolve()
    f = (base / filename).resolve()
    if not f.is_relative_to(base) or not f.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {filename}")
    return FileResponse(f, media_type=_IMAGE_MEDIA_TYPES.get(f.suffix.lower(), "image/png"))
