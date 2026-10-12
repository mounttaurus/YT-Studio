"""motion-agent の API（director の `/api/motion/*` から中継される）。

- GET  /health
- GET  /templates                         型の一覧
- GET  /templates/{tid}/v{ver}            meta.json と見本の名前
- GET  /templates/{tid}/v{ver}/samples/{name}   見本の描画入力
- POST /validate                          描画入力の検査と timing（描かない）
- POST /stills?wait=秒                    確認用の静止画のジョブ（wait を付けると終わるまで待って返す）
- POST /render                            本描画のジョブ（mp4・音なし）
- GET  /jobs/{job_id}
- GET  /gimmicks                          ギミックの台帳（ID・効き方・秒の範囲）と繋ぎ
- GET/PUT /plans/{pid}/{ep}               演出プラン（話ごと）の読み書きと検査の要約
- GET  /plans/{pid}/{ep}/history[/{name}] 過去の版
- POST /plans/{pid}/{ep}/stills|render    保存済みのプランの静止画・本描画
- POST /fonts/specimen                    書体の見本帳（画像）
- GET  /files/{path}                      shared/ のファイルを返す（描画中の Chrome が素材を取りに来る口でもある）
- GET  /hf/info・/hf/blocks・/hf/looks・/hf/projects/{pid}/{ep}  HyperFrames の版・見本・辞書（ブロック）・見た目の束・話のコンポジション
- PUT  /hf/projects/{pid}/{ep}/{comp_id}/files  話のコンポジションへファイルを書く（comp_id=new で新しい mi_NNNN・書いたあと検査）
- POST /hf/check|stills|render            HyperFrames の検査・静止画・描画（target を本文で渡す）
- GET  /audio/sfx・/audio/sfx/{kind}.wav  効果音の部品の一覧・試聴用の WAV（手続き合成・A0）
- POST /jobs/{job_id}/sfx                 終わった Remotion の描画ジョブに効果音（と BGM）を足した out_sfx.mp4 を作る（A0・A2）
- GET  /audio/kit・/audio/kit/{mood}.wav  予告編キット（BGM の手続き合成）の雰囲気の一覧・試聴用の WAV（A2）
- GET  /audio/map・/plans/{pid}/{ep}/audiomap  拍の格子から作る拍の地図（audiomap.json・A1）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from app.core import builders, config, jobs, plan as plan_mod, plan_store, registry, specimen
from app.core.templates import TemplateNotFound, get_meta, list_samples, list_templates, load_sample_input

router = APIRouter()


class RenderRequest(BaseModel):
    template_id: str
    version: int = 1
    input: dict[str, Any] | None = Field(None, description="描画入力。sample と排他")
    sample: str | None = Field(None, description="型に同梱の見本の名前")
    frames: list[dict[str, Any]] | None = Field(None, description="stills のみ。[{name, frame}]。省略でビートごとの見せ場")


def _prepare(req: RenderRequest) -> tuple[dict, dict, dict]:
    try:
        meta = get_meta(req.template_id, req.version)
        if req.sample:
            inp = load_sample_input(req.template_id, req.version, req.sample)
        elif req.input is not None:
            inp = req.input
        else:
            raise HTTPException(status_code=422, detail="input か sample のどちらかが要ります")
    except TemplateNotFound as e:
        raise HTTPException(status_code=404, detail=f"見つかりません: {e}") from e
    b = builders.for_meta(meta)
    errors = b.validate_input(meta, inp, config.SHARED_DIR)
    if errors:
        raise HTTPException(status_code=422, detail={"message": "描画入力の検査で指摘があります", "errors": errors})
    props = b.build_props(meta, inp, f"{config.SELF_URL}/files/")
    return meta, inp, props


def _timing_summary(props: dict) -> tuple[float, dict]:
    """検査の応答に載せる時間の要約（ビート型は timing、試作場はショットの区間。点群は載せない）。"""
    if "timing" in props:
        t = props["timing"]
        return round(t["total_frames"] / t["fps"], 3), t
    shots = [{k: s[k] for k in ("id", "gimmick", "from", "to", "enter")} for s in props["shots"]]
    return round(props["total_frames"] / props["fps"], 3), {"fps": props["fps"], "total_frames": props["total_frames"], "shots": shots}


@router.get("/health")
async def health():
    return {"status": "ok", "service": "motion-agent"}


@router.get("/templates")
async def templates():
    return {"templates": list_templates()}


@router.get("/templates/{template_id}/v{version}")
async def template_meta(template_id: str, version: int):
    try:
        meta = get_meta(template_id, version)
        out = {"meta": meta, "samples": list_samples(template_id, version)}
        if meta.get("extends"):  # 型 v2 は v1 の枠（個数・字数・つまみ・パレット）を土台にする
            ext = meta["extends"]
            out["base_meta"] = get_meta(ext["template_id"], ext["version"])
        return out
    except TemplateNotFound as e:
        raise HTTPException(status_code=404, detail=f"見つかりません: {e}") from e


@router.get("/templates/{template_id}/v{version}/samples/{name}")
async def template_sample(template_id: str, version: int, name: str):
    try:
        return load_sample_input(template_id, version, name)
    except TemplateNotFound as e:
        raise HTTPException(status_code=404, detail=f"見つかりません: {e}") from e


@router.get("/fonts")
async def fonts_list():
    """書体の束（同梱＋shared/motion/fonts/ のユーザーの書体）。演出プランで書体を id で選ぶための一覧。"""
    from app.core.fonts import load_fonts

    f = load_fonts()["fonts"]
    return {"fonts": [{"id": k, "family": v["family"], "weight": v.get("weight", 400), "role": v.get("role", ""),
                       "tags": v.get("tags", []), "ja": v.get("ja", True), "user": v.get("user", False)}
                      for k, v in f.items()]}


@router.post("/validate")
async def validate(req: RenderRequest):
    try:
        meta = get_meta(req.template_id, req.version)
        inp = load_sample_input(req.template_id, req.version, req.sample) if req.sample else (req.input or {})
    except TemplateNotFound as e:
        raise HTTPException(status_code=404, detail=f"見つかりません: {e}") from e
    b = builders.for_meta(meta)
    if meta.get("builder") == "plan":  # 演出プランは警告・時間の表・明滅の概算も返す
        return plan_mod.summarize(meta, inp, config.SHARED_DIR)
    errors = b.validate_input(meta, inp, config.SHARED_DIR)
    if errors:
        return {"ok": False, "errors": errors}
    total_sec, timing = _timing_summary(b.build_props(meta, inp, ""))
    return {"ok": True, "errors": [], "total_sec": total_sec, "timing": timing}


@router.post("/stills")
async def stills(req: RenderRequest, wait: float = Query(0, ge=0, le=300)):
    meta, inp, props = _prepare(req)
    frames = req.frames or builders.for_meta(meta).still_frames(props)
    layout = getattr(builders.for_meta(meta), "sheet_layout", None)
    job = jobs.submit("stills", meta["composition_id"], inp, props, frames, layout(props) if layout else None)
    if wait:
        import asyncio
        job = await asyncio.to_thread(jobs.wait, job["job_id"], wait)
    return job


@router.post("/render", status_code=202)
async def render(req: RenderRequest):
    meta, inp, props = _prepare(req)
    return jobs.submit("render", meta["composition_id"], inp, props)


@router.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    return job


@router.get("/files/{path:path}")
async def files(path: str):
    root = config.SHARED_DIR.resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")
    return FileResponse(target)


# ----- ギミックの台帳・書体の見本帳 ------------------------------------------------------------

@router.get("/gimmicks")
async def gimmicks():
    """ギミックの台帳。ID（カタログ ID＋名前）・効き方・秒の範囲・params の説明・繋ぎ・重ねもの・別名。
    型 v2 のビートごとに使えるギミックも添える。"""
    reg = registry.load()
    v2 = get_meta("kinetic_teaser", 2)
    return {"gimmicks": [{"id": k, **v} for k, v in reg["gimmicks"].items()],
            "enters": [{"id": k, **v} for k, v in reg["enters"].items()],
            "overlays": reg["overlays"], "aliases": reg["aliases"],
            "beats": {b: {"label": v["label"], "allowed": v["allowed"], "min_sec": v["min_sec"], "max_sec": v["max_sec"]}
                      for b, v in v2["beats"].items()},
            "bpm": v2["bpm"], "max_total_sec": v2["max_total_sec"], "k3_cuts": v2["k3_cuts"]}


class SpecimenRequest(BaseModel):
    text: str = Field("洗脳と記録 MK-ULTRA 2025", max_length=30)
    ids: list[str] | None = Field(None, description="書体の id（role:<タグ> も可）。指定すると他の絞り込みは無視")
    tag: str | None = Field(None, description="用途タグ（強調・不穏・機械・手書き・章題・毛筆・遊び・標準）")
    ja_only: bool = True
    user_only: bool = False
    query: str | None = Field(None, description="id か家族名に含まれる語")
    limit: int = Field(24, ge=1, le=specimen.MAX_ROWS)
    offset: int = Field(0, ge=0)


@router.post("/fonts/specimen")
async def fonts_specimen(req: SpecimenRequest):
    from app.core.fonts import load_fonts

    out_dir = config.WORK_DIR / "specimens"
    res = specimen.render(load_fonts(), req.text, out_dir, req.ids, req.tag, req.ja_only, req.user_only, req.query,
                          req.limit, req.offset)
    return {**res, "url": f"/files/{jobs.rel(out_dir / res['file'])}"}


# ----- 演出プラン（話ごと） -----------------------------------------------------------------------

class PlanBody(BaseModel):
    plan: dict[str, Any]


def _plan_meta(plan: dict) -> dict:
    t = plan.get("template") or {}
    try:
        meta = get_meta(t.get("id", "kinetic_teaser"), int(t.get("version", 2)))
    except (TemplateNotFound, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"型が見つかりません: {t}") from e
    if meta.get("builder") != "plan":
        raise HTTPException(status_code=422, detail="演出プランを使える型は kinetic_teaser v2 です")
    return meta


def _store(fn, *a):
    try:
        return fn(*a)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"見つかりません: {e}") from e
    except plan_store.PlanStoreError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/plans/{project_id}/{episode}")
async def plan_get(project_id: str, episode: int):
    plan = _store(plan_store.load, project_id, episode)
    if plan is None:
        raise HTTPException(status_code=404, detail="演出プランがありません（PUT で保存してください）")
    return {"plan": plan, "analysis": plan_mod.summarize(_plan_meta(plan), plan, config.SHARED_DIR)}


@router.put("/plans/{project_id}/{episode}")
async def plan_put(project_id: str, episode: int, body: PlanBody):
    plan = {"template": {"id": "kinetic_teaser", "version": 2}, **body.plan}
    meta = _plan_meta(plan)
    saved = _store(plan_store.save, project_id, episode, plan)
    return {"saved": saved, "analysis": plan_mod.summarize(meta, plan_store.canonicalize(plan), config.SHARED_DIR)}


@router.get("/plans/{project_id}/{episode}/audiomap")
async def plan_audiomap(project_id: str, episode: int):
    """保存済みの演出プランの拍の地図（拍の格子から作る・A1）。検査に誤りがあれば 422。"""
    plan = _store(plan_store.load, project_id, episode)
    if plan is None:
        raise HTTPException(status_code=404, detail="演出プランがありません（PUT で保存してください）")
    a = plan_mod.analyze(_plan_meta(plan), plan, config.SHARED_DIR)
    tl = a.get("timeline")
    if not tl:
        raise HTTPException(status_code=422, detail={"message": "時間の表が作れない（検査の誤り）", "errors": a.get("errors")})
    return tl["audiomap"]


@router.get("/plans/{project_id}/{episode}/history")
async def plan_history(project_id: str, episode: int):
    return {"history": _store(plan_store.history, project_id, episode)}


@router.get("/plans/{project_id}/{episode}/history/{name}")
async def plan_history_get(project_id: str, episode: int, name: str):
    return _store(plan_store.load_history, project_id, episode, name)


def _plan_request(project_id: str, episode: int) -> RenderRequest:
    plan = _store(plan_store.load, project_id, episode)
    if plan is None:
        raise HTTPException(status_code=404, detail="演出プランがありません（PUT で保存してください）")
    t = _plan_meta(plan)
    return RenderRequest(template_id=t["template_id"], version=t["version"], input=plan)


@router.post("/plans/{project_id}/{episode}/stills")
async def plan_stills(project_id: str, episode: int, wait: float = Query(0, ge=0, le=300),
                      frames: list[dict[str, Any]] | None = None):
    req = _plan_request(project_id, episode)
    req.frames = frames
    return await stills(req, wait)


@router.post("/plans/{project_id}/{episode}/render", status_code=202)
async def plan_render(project_id: str, episode: int):
    return await render(_plan_request(project_id, episode))


# ----- HyperFrames（新しい組み立て方・Docs/MOTION_UNIFIED_PLAN.md §11-3） ----------------------
# target は samples/{name} か projects/{pid}/{ep}/{comp_id}。CLI はコンテナの中だけで動く。

class HfTarget(BaseModel):
    target: str


class HfCheckRequest(HfTarget):
    deep: bool = Field(False, description="文字の枠を実測して字幕の帯・安全域を見る（ブラウザを使う・十数秒）")


class HfPutRequest(BaseModel):
    files: dict[str, Any] = Field(..., description="{パス: 中身}。中身は文字列・JSON（motion.json）・{base64}（画像）")
    check: bool = Field(True, description="書いたあと、描く前の検査（lint・ブロック・長さ…）を返す")


class HfStillsRequest(HfTarget):
    frames: int = Field(6, ge=1, le=12)
    at: list[float] | None = None


class HfRenderRequest(HfTarget):
    quality: str = "standard"


def _hf_call(fn, *a, **kw):
    from app.core import hf
    try:
        return fn(*a, **kw)
    except hf.TargetError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/hf/info")
async def hf_info():
    from app.core import hf
    return {"hyperframes": hf.version(), "samples": hf.list_samples(), "fps": 30,
            "qualities": sorted(hf.QUALITIES), "default_quality": hf.DEFAULT_QUALITY}


@router.get("/hf/blocks")
async def hf_blocks_list():
    """辞書（ブロック）の台帳。id・種別・用途・変数（型と範囲）・出し方・文字数・素材・置き方。"""
    from app.core import hf_blocks
    return {"blocks": hf_blocks.list_blocks()}


@router.get("/hf/looks")
async def hf_looks_list():
    """見た目の束（色・書体）の一覧。チャンネルのデータ（shared/motion/looks/）が辞書側の候補より先。"""
    from app.core import hf_look
    return {"looks": hf_look.list_looks()}


@router.get("/hf/projects/{project_id}/{episode}")
async def hf_project_list(project_id: str, episode: int):
    from app.core import hf
    return {"compositions": _hf_call(hf.list_project, project_id, episode)}


@router.put("/hf/projects/{project_id}/{episode}/{comp_id}/files")
async def hf_put_files(project_id: str, episode: int, comp_id: str, req: HfPutRequest):
    """話のコンポジションへ書く（M4: 書くのは motion-agent だけ）。comp_id が new なら新しい mi_NNNN。"""
    import asyncio
    from app.core import hf, hf_put
    try:
        res = await asyncio.to_thread(hf_put.put, project_id, episode, None if comp_id == "new" else comp_id, req.files)
    except hf.TargetError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except hf_put.PutError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if req.check and (hf.resolve(res["target"]) / "index.html").is_file():
        res["check"] = await asyncio.to_thread(_hf_call, hf.check, res["target"], False)
    return res


@router.post("/hf/check")
async def hf_check(req: HfCheckRequest):
    import asyncio
    from app.core import hf
    return await asyncio.to_thread(_hf_call, hf.check, req.target, req.deep)


@router.post("/hf/stills")
async def hf_stills(req: HfStillsRequest, wait: float = Query(0, ge=0, le=300)):
    job = _hf_call(jobs.submit_hf, "stills", req.target, frames=req.frames, at=req.at)
    if wait:
        import asyncio
        job = await asyncio.to_thread(jobs.wait, job["job_id"], wait)
    return job


@router.post("/hf/render", status_code=202)
async def hf_render(req: HfRenderRequest):
    """検査（lint・ネットワーク・長さ）に誤りがあれば描かない（422）。"""
    import asyncio
    from app.core import hf
    res = await asyncio.to_thread(_hf_call, hf.check, req.target, True)   # 描く前は文字の枠の実測も通す（M6）
    if not res["ok"]:
        raise HTTPException(status_code=422, detail={"message": "検査に誤りがあるので描かない", "check": res})
    return _hf_call(jobs.submit_hf, "render", req.target, quality=req.quality)


# ----- 音（効果音の手続き合成・Docs/MOTION_UNIFIED_PLAN.md §9・A0） --------------------------------

@router.get("/audio/sfx")
async def audio_sfx():
    """部品の一覧（名前・既定の引数・基準点・pitch の意味）。"""
    from app.core.audio import sfx_synth
    return {"sample_rate": sfx_synth.SR, "version": sfx_synth.VERSION, "parts": sfx_synth.describe()}


@router.get("/audio/sfx/{kind}.wav")
async def audio_sfx_wav(kind: str, length: float | None = None, pitch: float | None = None,
                        gain: float | None = None, seed: int | None = None):
    """試聴用の WAV。引数を省けば部品の既定。同じ引数なら同じバイト列（M2）。"""
    from app.core.audio import sfx_synth
    params = {k: v for k, v in (("length", length), ("pitch", pitch), ("gain", gain), ("seed", seed)) if v is not None}
    try:
        data = sfx_synth.make_wav(kind, **params)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return Response(content=data, media_type="audio/wav")


class SfxMuxRequest(BaseModel):
    min_gap_sec: float = Field(0.1, ge=0.0, le=2.0, description="同じ部品の打点がこれより近ければ後ろを間引く")
    seed: int = 0
    bgm: str | None = Field(None, description="予告編キットの雰囲気（dread・drive・hush）。演出プラン（型 v2）の描画だけ")
    bgm_gain: float = Field(0.55, ge=0.0, le=1.0, description="BGM の音量（効果音の下に敷く）")


@router.post("/jobs/{job_id}/sfx")
async def job_sfx(job_id: str, req: SfxMuxRequest | None = None):
    """終わった Remotion の本描画ジョブ（無音の out.mp4）に、props の効果音の打点から作った音を足す。
    映像は触らない（-c:v copy）。結果は同じジョブのフォルダの out_sfx.mp4。"""
    import asyncio
    from app.core.audio import mix
    req = req or SfxMuxRequest()
    try:
        return await asyncio.to_thread(mix.mux_job, job_id, req.min_gap_sec, req.seed, req.bgm, req.bgm_gain)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except mix.MixError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/audio/map")
async def audio_map(bpm: float = Query(120.0, ge=60, le=200), fps: int = Query(30, ge=24, le=60)):
    """拍の格子から作る拍の地図（型 v1 の既定の長さ・見本）。形は analyze-beatgrid.py の audiomap.json（version 2）。
    演出プランの地図は GET /plans/{pid}/{ep}/audiomap。"""
    from app.core.audio import kit
    return kit.demo_audiomap(bpm, fps)


@router.get("/audio/kit")
async def audio_kit():
    """予告編キット（BGM の手続き合成・§9-3）の雰囲気の一覧。"""
    from app.core.audio import kit
    return {"moods": kit.moods()}


@router.get("/audio/kit/{mood}.wav")
async def audio_kit_wav(mood: str, bpm: float = Query(120.0, ge=60, le=200), fps: int = Query(30, ge=24, le=60),
                        seed: int = 0):
    """試聴用の BGM（型 v1 の既定の長さ・①〜⑤の構造）。同じ引数なら同じバイト列（M2）。"""
    from app.core.audio import kit, sfx_synth
    try:
        x, _ = kit.render(kit.demo_audiomap(bpm, fps), mood, fps=fps, seed=seed)
    except kit.KitError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return Response(content=sfx_synth.wav_bytes(x), media_type="audio/wav")
