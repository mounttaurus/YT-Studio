import json
import logging
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.core import audio_utils, cache_manager, tts_client
from app.core.emotion_mapper import apply_emotion_to_text, emotion_to_emoji
from app.core.project_manager import (
    append_error,
    get_project_dir,
    get_episode_dir,
    get_script_path,
    get_audio_dir,
    list_episodes,
    read_project,
    update_status,
    write_project,
)
from app.core.script_parser import (
    Line,
    detect_speakers,
    parse_bracket_format,
    parse_colon_format,
    parse_script_json,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# voices/ フォルダ：wav ファイルを直接置くだけでキャラクター設定完了
VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
SHARED_DIR = Path(os.environ.get("SHARED_DIR", "./shared"))
# プレビュー・テスト音声はコンテナ内部（最終生成物ではない）
PREVIEW_DIR = Path(os.environ.get("PREVIEW_DIR", "/app/tmp_audio"))

_running: dict[str, bool] = {}
# 全セリフ生成の進捗（run_key → {total, done, current_line_id}）。
# プロセス内メモリのみ。次回run開始時に上書きされる。
_progress: dict[str, dict] = {}


# ───────────────────────────── health ──────────────────────────────

@router.get("/health")
async def health():
    engine_status = await tts_client.check_health()
    return {
        "status": "ok",
        "engine": "irodori",
        "engine_url": tts_client.IRODORI_SERVER_URL,
        "engine_reachable": engine_status["reachable"],
    }


# ───────────────────────────── projects ────────────────────────────

@router.get("/projects")
async def list_projects_endpoint():
    projects_dir = SHARED_DIR / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    projects = []
    for d in sorted(projects_dir.iterdir()):
        if not d.is_dir():
            continue
        pj_file = d / "project.json"
        if not pj_file.exists():
            continue
        try:
            data = json.loads(pj_file.read_text(encoding="utf-8"))
            # episodes/ 構造と旧構造の両方に対応
            eps = list_episodes(d.name)
            has_script = any(e.get("has_script") for e in eps) if eps else (d / "script.json").exists()
            audio_files = []
            for ep in eps:
                ep_audio = d / "episodes" / f"ep{ep['number']:02d}" / "audio"
                if ep_audio.exists():
                    audio_files.extend([f.name for f in sorted(ep_audio.glob("*.wav"))])
            if not audio_files and (d / "audio").exists():
                audio_files = [f.name for f in sorted((d / "audio").glob("*.wav"))]
            projects.append({
                "id": d.name,
                "title": data.get("title", d.name),
                "status": data.get("status", {}),
                "has_script": has_script,
                "episodes": eps,
                "speakers": data.get("config", {}).get("tts", {}).get("speakers", []),
                "default_speed": data.get("config", {}).get("tts", {}).get("default_speed", 1.0),
                "default_pause_after_sec": data.get("config", {}).get("tts", {}).get("default_pause_after_sec", 0.3),
                "audio_files": audio_files,
            })
        except Exception as e:
            logger.warning("Failed to read project %s: %s", d.name, e)
    return {"projects": projects}


@router.post("/projects/create_sample")
async def create_sample_project():
    """テスト用サンプルプロジェクトを作成する"""
    project_dir = SHARED_DIR / "projects" / "sample_001_test"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "audio").mkdir(exist_ok=True)
    (project_dir / "footage").mkdir(exist_ok=True)
    (project_dir / "output").mkdir(exist_ok=True)

    # voices/ に実際の wav ファイルがあればその名前を使う
    available_voices = _scan_local_voices()
    spk_a_voice = available_voices[0]["id"] if len(available_voices) > 0 else "none"
    spk_b_voice = available_voices[1]["id"] if len(available_voices) > 1 else "none"

    project_json = {
        "schema_version": "1.0.0",
        "id": "sample_001",
        "slug": "test",
        "title": "サンプルプロジェクト",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "language": "ja",
        "style": "dialogue",
        "target_duration_sec": 30,
        "status": {
            "research": "skipped",
            "scripting": "done",
            "tts": "pending",
            "footage": "pending",
            "video_edit": "not_started",
        },
        "config": {
            "llm": {"provider": "openrouter", "model": "anthropic/claude-sonnet-4-5"},
            "tts": {
                "engine": "irodori",
                "speakers": [
                    {
                        "id": "speaker_a",
                        "name": "話者A",
                        "role": "main",
                        "voice": spk_a_voice,
                        "caption": "",
                    },
                    {
                        "id": "speaker_b",
                        "name": "話者B",
                        "role": "sub",
                        "voice": spk_b_voice,
                        "caption": "",
                    },
                ],
            },
            "footage": {"sources": ["pexels"], "aspect_ratio": "16:9", "resolution": "1920x1080"},
        },
        "errors": [],
    }
    script_json = {
        "schema_version": "1.0.0",
        "project_id": "sample_001",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_duration_sec": 15,
        "lines": [
            {"id": "line_001", "order": 1, "speaker_id": "speaker_a", "speaker_name": "話者A",
             "text": "こんにちは！これはテストです。", "emotion": "happy", "speed": 1.0,
             "pause_after_sec": 0.5, "section": "intro", "notes": ""},
            {"id": "line_002", "order": 2, "speaker_id": "speaker_b", "speaker_name": "話者B",
             "text": "正常に動作していますね。", "emotion": "neutral", "speed": 1.0,
             "pause_after_sec": 0.3, "section": "intro", "notes": ""},
            {"id": "line_003", "order": 3, "speaker_id": "speaker_a", "speaker_name": "話者A",
             "text": "😊では、始めましょう！", "emotion": "happy", "speed": 1.0,
             "pause_after_sec": 0.3, "section": "intro", "notes": "絵文字テスト"},
        ],
        "sections": [{"id": "intro", "label": "イントロ", "line_ids": ["line_001", "line_002", "line_003"]}],
        "metadata": {"line_count": 3, "estimated_duration_sec": 15, "style": "dialogue",
                     "checked_by_director": True, "check_passed": True, "check_notes": ""},
    }

    (project_dir / "project.json").write_text(
        json.dumps(project_json, ensure_ascii=False, indent=2), encoding="utf-8")
    (project_dir / "script.json").write_text(
        json.dumps(script_json, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"status": "created", "project_id": "sample_001_test"}


@router.get("/projects/{project_id}/status")
async def project_status(project_id: str, episode: int = 1):
    try:
        pj = read_project(project_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Project not found: {project_id}")
    return {
        "id": pj["id"],
        "status": pj.get("status", {}),
        "errors": pj.get("errors", []),
        "progress": _progress.get(f"{project_id}:ep{episode}"),
    }


@router.post("/projects/{project_id}/speakers")
async def update_speakers(project_id: str, body: dict):
    """話者設定（voice, caption）とTTS生成のデフォルト値（速度・ポーズ）を保存する。生成前に呼び出す。"""
    try:
        pj = read_project(project_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Project not found: {project_id}")
    speakers = body.get("speakers", [])
    # 手動作成プロジェクト等で config / config.tts が無くても保存できるようにする
    tts_cfg = pj.setdefault("config", {}).setdefault("tts", {})
    tts_cfg["speakers"] = speakers
    if "default_speed" in body:
        tts_cfg["default_speed"] = float(body["default_speed"])
    if "default_pause_after_sec" in body:
        tts_cfg["default_pause_after_sec"] = float(body["default_pause_after_sec"])
    write_project(project_id, pj)
    return {
        "status": "ok",
        "speakers": speakers,
        "default_speed": tts_cfg.get("default_speed", 1.0),
        "default_pause_after_sec": tts_cfg.get("default_pause_after_sec", 0.3),
    }


@router.post("/projects/{project_id}/run")
async def project_run(
    project_id: str,
    background_tasks: BackgroundTasks,
    episode: int = 1,
):
    try:
        read_project(project_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Project not found: {project_id}")
    run_key = f"{project_id}:ep{episode}"
    if _running.get(run_key):
        raise HTTPException(409, "Already running")
    background_tasks.add_task(_run_project, project_id, episode)
    return {"status": "accepted", "project_id": project_id, "episode": episode}


@router.post("/projects/{project_id}/cancel")
async def project_cancel(project_id: str, episode: int = 1):
    _running[f"{project_id}:ep{episode}"] = False
    return {"status": "cancel_requested", "project_id": project_id, "episode": episode}


@router.post("/projects/{project_id}/run/line/{line_id}")
async def run_single_line(project_id: str, line_id: str, episode: int = 1, force: bool = False):
    """1行だけ生成する。force=true でキャッシュを無視（リテイク用）。
    生成後は tts.json の該当エントリとタイムラインも同期更新する。
    """
    try:
        pj = read_project(project_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Project not found: {project_id}")

    script_path = get_script_path(project_id, episode)
    if not script_path.exists():
        raise HTTPException(404, "script.json not found")

    lines = parse_script_json(script_path)
    line = next((l for l in lines if l.id == line_id), None)
    if not line:
        raise HTTPException(404, f"Line not found: {line_id}")

    voice, caption = _resolve_voice_caption(pj, line.speaker_id)
    # 未割当の役は暫定声で喋らせない（キャラタブが唯一の本籍。未割当=生成ブロック）。
    if voice == "none":
        raise HTTPException(
            409,
            f"話者 {line.speaker_id} にキャラ/声が未割当です。🎭キャラタブで割り当ててください",
        )
    processed_text = apply_emotion_to_text(line.text, line.emotion)
    audio_dir = get_audio_dir(project_id, episode)
    out_path = audio_dir / f"{line_id}.wav"

    key = cache_manager.get_cache_key(processed_text, voice, "irodori-stereo", caption=caption, speed=line.speed)
    cached = None if force else cache_manager.cache_hit(key)
    if cached:
        shutil.copy(cached, out_path)
        cache_hit = True
        duration = _wav_duration_sec(out_path)
    else:
        audio_bytes = await tts_client.generate(processed_text, voice, line.speed, caption=caption)
        out_path.write_bytes(audio_bytes)
        cache_manager.save_to_cache(key, audio_bytes, processed_text, voice, "irodori-stereo")
        cache_hit = False
        duration = audio_utils.wav_duration_sec(audio_bytes)

    _upsert_tts_entry(project_id, episode, lines, line, voice, caption,
                      processed_text, duration, cache_hit)
    return {
        "line_id": line_id,
        "cache_hit": cache_hit,
        "duration_sec": round(duration, 2),
        "file": str(out_path),
    }


@router.delete("/projects/{project_id}/audio/line/{line_id}")
async def delete_line_audio(project_id: str, line_id: str, episode: int = 1):
    """行の音声(wav)と tts.json のエントリを削除する（台本の行削除との同期用）。"""
    try:
        read_project(project_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Project not found: {project_id}")

    audio_dir = get_audio_dir(project_id, episode)
    wav_path = audio_dir / f"{Path(line_id).name}.wav"
    deleted_wav = False
    if wav_path.exists():
        wav_path.unlink()
        deleted_wav = True

    tts, tts_path = _load_tts_json(project_id, episode)
    deleted_entry = False
    if tts is not None:
        before = len(tts.get("audio_files", []))
        tts["audio_files"] = [f for f in tts.get("audio_files", []) if f.get("line_id") != line_id]
        deleted_entry = len(tts["audio_files"]) < before
        if deleted_entry:
            script_path = get_script_path(project_id, episode)
            lines = parse_script_json(script_path) if script_path.exists() else []
            _rebuild_tts_metadata(tts, lines)
            tts_path.write_text(json.dumps(tts, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"line_id": line_id, "deleted_wav": deleted_wav, "deleted_tts_entry": deleted_entry}


# ───────────────────────────── voices ──────────────────────────────

@router.get("/voices")
async def list_voices():
    """voices/ フォルダの参照音声一覧を返す（Irodori サーバー経由）。
    サーバーが起動していない場合はローカルスキャンにフォールバック。
    """
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{tts_client.IRODORI_SERVER_URL}/v1/audio/voices")
            resp.raise_for_status()
            data = resp.json()
            voices = [
                {"id": v["id"], "name": v["id"]}
                for v in data.get("data", [])
                if not v.get("no_ref")  # "none" 等の no_ref エントリを除外
            ]
            return {"voices": voices}
    except Exception:
        # フォールバック: ローカルスキャン
        return {"voices": _scan_local_voices()}


@router.get("/voices/profiles")
async def list_voice_profiles_endpoint():
    """声カタログ（フラットファイル）を profile 形に整形して返す。
    声カタログはフラット運用（1音声ファイル=1声、stem=voice_id）。字幕名・性格は
    character.json が本籍のため、ここは id ベースの最小プロファイルのみを返す。
    Irodori サーバーの稼働状況に関わらず常に利用可能。
    scripting-agent などの他コンテナがボイス一覧を取得するために使用する。
    """
    voices = _scan_local_voices()
    profiles = [
        {
            "id": v["id"],
            "display_name": v["id"],
            "role": "regular",
            "has_ref_wav": True,   # フラットファイル自身が参照音声
            "has_ref_latent": False,
            "has_caption": False,  # 字幕名・性格は character.json が本籍
            "description": "",
        }
        for v in voices
    ]
    return {"profiles": profiles, "count": len(profiles)}


@router.post("/voices")
async def upload_voice(file: UploadFile = File(...)):
    """voices/ フォルダに参照音声をアップロードする。"""
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    filename = Path(file.filename).name
    dest = VOICES_DIR / filename
    dest.write_bytes(await file.read())
    return {"voice_id": Path(filename).stem, "filename": filename, "status": "uploaded"}


@router.delete("/voices/{voice_id}")
async def delete_voice(voice_id: str):
    """voices/ フォルダから参照音声を削除する。"""
    VOICES_EXTENSIONS = {".wav", ".flac", ".mp3", ".m4a", ".ogg"}
    for ext in VOICES_EXTENSIONS:
        path = VOICES_DIR / f"{voice_id}{ext}"
        if path.exists():
            path.unlink()
            return {"voice_id": voice_id, "status": "deleted"}
    raise HTTPException(404, f"Voice not found: {voice_id}")


# ───────────────────────────── preview ─────────────────────────────

class PreviewRequest(BaseModel):
    text: str
    voice: str = "none"
    caption: str = ""
    emotion: str = "neutral"
    speed: float = 1.0


@router.post("/preview")
async def preview(req: PreviewRequest):
    processed = apply_emotion_to_text(req.text, req.emotion)
    caption = req.caption.strip() or None

    try:
        audio_bytes = await tts_client.generate(
            processed,
            req.voice or "none",
            req.speed,
            caption=caption,
        )
    except Exception as e:
        logger.error("TTS generate error: %s", e, exc_info=True)
        raise HTTPException(503, f"TTS server error: {e}")

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(4)
    voice_label = (req.voice or "none").replace("/", "_")[:20]
    filename = f"{ts}_{voice_label}_{rand}.wav"
    (PREVIEW_DIR / filename).write_bytes(audio_bytes)

    return {
        "filename": filename,
        "audio_url": f"/audio/direct/{filename}",
        "duration_hint": audio_utils.wav_duration_sec(audio_bytes),
    }


@router.get("/audio/direct/{filename}")
async def serve_direct_audio(filename: str):
    safe_name = Path(filename).name
    path = PREVIEW_DIR / safe_name
    if not path.exists():
        raise HTTPException(404, "Audio file not found")
    return Response(content=path.read_bytes(), media_type="audio/wav")


@router.get("/audio/project/{project_id}/{filename}")
async def serve_project_audio(project_id: str, filename: str, episode: int = 1):
    safe_name = Path(filename).name
    try:
        audio_dir = get_audio_dir(project_id, episode)
    except FileNotFoundError:
        raise HTTPException(404, "Project not found")
    path = audio_dir / safe_name
    if not path.exists():
        raise HTTPException(404, "Audio file not found")
    return Response(content=path.read_bytes(), media_type="audio/wav")


# ───────────────────────────── script upload ────────────────────────

class TextScriptRequest(BaseModel):
    text: str
    speakers: list[dict] = []
    style: str = "dialogue"


@router.post("/projects/{project_id}/script/text")
async def upload_text_script(project_id: str, req: TextScriptRequest):
    try:
        project_dir = get_project_dir(project_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Project not found: {project_id}")
    from app.core.script_parser import parse_plain_text
    lines = parse_plain_text(req.text, req.speakers, req.style)
    _save_script(project_dir, project_id, lines, req.style)
    update_status(project_id, "scripting", "done")
    return {"status": "ok", "line_count": len(lines)}


# ───────────────────────────── validate / parse ─────────────────────

@router.post("/validate/script")
async def validate_script(data: dict):
    errors = []
    if "schema_version" not in data:
        errors.append("Missing field: schema_version")
    if "lines" not in data or not isinstance(data.get("lines"), list):
        errors.append("Missing or invalid field: lines")
    if errors:
        return {"valid": False, "errors": errors}
    lines = data["lines"]
    duration = sum(l.get("pause_after_sec", 0.3) + 2.0 for l in lines)
    return {"valid": True, "line_count": len(lines), "estimated_duration": round(duration, 1), "errors": []}


class ParseTextRequest(BaseModel):
    text: str
    format: str = "colon"
    speakers: dict = {}


@router.post("/parse/text")
async def parse_text_endpoint(req: ParseTextRequest):
    if req.format == "bracket":
        lines_raw = parse_bracket_format(req.text, req.speakers)
    else:
        lines_raw = parse_colon_format(req.text, req.speakers)
    found_speakers = detect_speakers(req.text, req.format)
    script = {
        "schema_version": "1.0.0",
        "project_id": "temp",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_duration_sec": len(lines_raw) * 2.3,
        "lines": lines_raw,
        "sections": [{"id": "main", "label": "メイン", "line_ids": [l["id"] for l in lines_raw]}],
        "metadata": {
            "line_count": len(lines_raw),
            "estimated_duration_sec": len(lines_raw) * 2.3,
            "style": "dialogue" if req.format in ("colon", "bracket") else "monologue",
            "checked_by_director": False, "check_passed": False, "check_notes": "",
        },
    }
    return {"script": script, "detected_speakers": found_speakers}


# ───────────────────────────── internal ────────────────────────────

VOICE_EXTENSIONS = {".wav", ".flac", ".mp3", ".m4a", ".ogg"}


def _scan_local_voices() -> list[dict]:
    """voices/ ディレクトリ内の参照音声ファイルを直接スキャンして返す"""
    if not VOICES_DIR.exists():
        return []
    result = []
    for path in sorted(VOICES_DIR.iterdir()):
        if path.is_file() and path.suffix.lower() in VOICE_EXTENSIONS:
            result.append({"id": path.stem, "name": path.stem})
    return result


def _wav_duration_sec(path: Path) -> float:
    """WAVファイルの実際の再生時間（秒）"""
    return audio_utils.wav_duration_sec_from_file(path)


def _load_tts_json(project_id: str, episode: int) -> tuple[dict | None, Path]:
    ep_dir = get_episode_dir(project_id, episode)
    path = ep_dir / "tts.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")), path
        except Exception:
            return None, path
    return None, path


def _rebuild_tts_metadata(tts: dict, lines: list) -> None:
    """audio_files から timeline と metadata を再計算する（行単位更新・削除後の同期用）。"""
    pause_by_id = {l.id: l.pause_after_sec for l in lines}
    entries = sorted(tts.get("audio_files", []), key=lambda e: e.get("order", 0))
    timeline = []
    current = 0.0
    for e in entries:
        duration = float(e.get("duration_sec", 0.0))
        pause = pause_by_id.get(e.get("line_id"), 0.3)
        timeline.append({
            "line_id": e.get("line_id"),
            "file_path": e.get("file_path"),
            "start_sec": round(current, 3),
            "end_sec": round(current + duration, 3),
            "pause_after_sec": pause,
        })
        current += duration + pause
    tts["timeline"] = timeline
    meta = tts.setdefault("metadata", {})
    meta["total_audio_duration_sec"] = round(current, 2)
    meta["file_count"] = len(entries)
    meta["all_generated"] = bool(lines) and len(entries) == len(lines)
    tts["generated_at"] = datetime.now(timezone.utc).isoformat()


def _upsert_tts_entry(project_id: str, episode: int, lines: list, line,
                      voice: str, caption: str | None, processed_text: str,
                      duration: float, cache_hit: bool) -> None:
    """行単位生成後に tts.json の該当エントリを更新（無ければ挿入）し、タイムラインを再計算する。"""
    tts, tts_path = _load_tts_json(project_id, episode)
    if tts is None:
        tts = {
            "schema_version": "1.0.0",
            "project_id": project_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": "irodori",
            "audio_files": [],
            "timeline": [],
            "metadata": {},
        }
    entry = {
        "line_id": line.id, "order": line.order,
        "speaker_id": line.speaker_id, "speaker_name": line.speaker_name,
        "text": line.text, "processed_text": processed_text,
        "emotion": line.emotion, "emotion_emoji": emotion_to_emoji(line.emotion),
        "speed": line.speed, "voice_id": voice,
        "caption": caption,
        "file_path": f"episodes/ep{episode:02d}/audio/{line.id}.wav",
        "duration_sec": round(duration, 2), "sample_rate": 48000,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cache_hit": cache_hit,
    }
    files = [f for f in tts.get("audio_files", []) if f.get("line_id") != line.id]
    files.append(entry)
    tts["audio_files"] = sorted(files, key=lambda e: e.get("order", 0))
    _rebuild_tts_metadata(tts, lines)
    tts_path.write_text(json.dumps(tts, ensure_ascii=False, indent=2), encoding="utf-8")


def _character_voice_caption(character_id: str) -> tuple[str, str | None] | None:
    """キャラ本籍 shared/characters/{id}/character.json から (voice_id, caption) を解決。
    無効/未割当なら None を返す（呼び出し側が後方互換のインライン値へフォールバック）。"""
    if not character_id:
        return None
    cpath = SHARED_DIR / "characters" / character_id / "character.json"
    try:
        c = json.loads(cpath.read_text(encoding="utf-8"))
    except Exception:
        return None
    voice = (c.get("voice") or {}).get("voice_id") or "none"
    caption = (c.get("caption") or "").strip() or (c.get("name") or "").strip() or None
    return voice, caption


def _resolve_voice_caption(pj: dict, speaker_id: str) -> tuple[str, str | None]:
    """プロジェクト設定から speaker_id の (voice, caption) を返す。
    character_id があればキャラ本籍から解決（声・字幕を二重定義しない）。
    旧形式（voice/caption 直書き）は後方互換でフォールバック。"""
    for sp in pj.get("config", {}).get("tts", {}).get("speakers", []):
        if sp["id"] == speaker_id:
            resolved = _character_voice_caption(sp.get("character_id", ""))
            if resolved is not None:
                return resolved
            voice = sp.get("voice") or "none"
            caption = sp.get("caption", "").strip() or None
            return voice, caption
    return "none", None


def _save_script(project_dir: Path, project_id: str, lines: list, style: str):
    script = {
        "schema_version": "1.0.0",
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_duration_sec": sum(l.pause_after_sec + 2.0 for l in lines),
        "lines": [
            {"id": l.id, "order": l.order, "speaker_id": l.speaker_id,
             "speaker_name": l.speaker_name, "text": l.text, "emotion": l.emotion,
             "speed": l.speed, "pause_after_sec": l.pause_after_sec,
             "section": l.section, "notes": l.notes}
            for l in lines
        ],
        "sections": [{"id": "main", "label": "メイン", "line_ids": [l.id for l in lines]}],
        "metadata": {
            "line_count": len(lines),
            "estimated_duration_sec": sum(l.pause_after_sec + 2.0 for l in lines),
            "style": style, "checked_by_director": False,
            "check_passed": False, "check_notes": "",
        },
    }
    (project_dir / "script.json").write_text(
        json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")


async def _run_project(project_id: str, episode_number: int = 1):
    run_key = f"{project_id}:ep{episode_number}"
    _running[run_key] = True
    try:
        pj = read_project(project_id)
        script_path = get_script_path(project_id, episode_number)

        if not script_path.exists():
            append_error(project_id, "tts", "SCRIPT_NOT_FOUND",
                         f"script.json が存在しません (episode={episode_number})")
            update_status(project_id, "tts", "error", episode_number=episode_number)
            return

        lines = parse_script_json(script_path)
        update_status(project_id, "tts", "running", episode_number=episode_number)
        _progress[run_key] = {"total": len(lines), "done": 0, "current_line_id": None}

        audio_dir = get_audio_dir(project_id, episode_number)

        audio_files = []
        timeline = []
        current_time = 0.0

        for line in lines:
            if not _running.get(run_key):
                update_status(project_id, "tts", "pending", episode_number=episode_number)
                return

            _progress[run_key]["current_line_id"] = line.id
            voice, caption = _resolve_voice_caption(pj, line.speaker_id)
            # 未割当（キャラ未割当 or キャラに声が無い）の役は暫定声で喋らせず、スキップ＋警告。
            # キャラタブが唯一の本籍。NULLキャラは作らず「未割当=生成ブロック」とする。
            if voice == "none":
                append_error(project_id, "tts", "SPEAKER_UNASSIGNED",
                             f"{line.id}: 話者 {line.speaker_id} にキャラ/声が未割当のためスキップしました",
                             recoverable=True)
                continue
            processed_text = apply_emotion_to_text(line.text, line.emotion)
            key = cache_manager.get_cache_key(processed_text, voice, "irodori-stereo", caption=caption, speed=line.speed)
            out_path = audio_dir / f"{line.id}.wav"
            cache_hit = False

            cached = cache_manager.cache_hit(key)
            if cached:
                shutil.copy(cached, out_path)
                cache_hit = True
                duration = _wav_duration_sec(out_path)
            else:
                try:
                    audio_bytes = await tts_client.generate(
                        processed_text, voice, line.speed, caption=caption
                    )
                    out_path.write_bytes(audio_bytes)
                    cache_manager.save_to_cache(key, audio_bytes, processed_text, voice, "irodori-stereo")
                    duration = audio_utils.wav_duration_sec(audio_bytes)
                except Exception as e:
                    logger.error("Line %s generation failed: %s", line.id, e, exc_info=True)
                    append_error(project_id, "tts", "GENERATE_FAILED", f"{line.id}: {e}", recoverable=True)
                    continue

            audio_files.append({
                "line_id": line.id, "order": line.order,
                "speaker_id": line.speaker_id, "speaker_name": line.speaker_name,
                "text": line.text, "processed_text": processed_text,
                "emotion": line.emotion, "emotion_emoji": emotion_to_emoji(line.emotion),
                "speed": line.speed, "voice_id": voice,
                "caption": caption,
                "file_path": f"episodes/ep{episode_number:02d}/audio/{line.id}.wav",
                "duration_sec": round(duration, 2), "sample_rate": 48000,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "cache_hit": cache_hit,
            })
            timeline.append({
                "line_id": line.id,
                "file_path": f"episodes/ep{episode_number:02d}/audio/{line.id}.wav",
                "start_sec": round(current_time, 3),
                "end_sec": round(current_time + duration, 3),
                "pause_after_sec": line.pause_after_sec,
            })
            current_time += duration + line.pause_after_sec
            _progress[run_key]["done"] += 1

        tts_json = {
            "schema_version": "1.0.0", "project_id": project_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": "irodori", "audio_files": audio_files, "timeline": timeline,
            "metadata": {
                "total_audio_duration_sec": round(current_time, 2),
                "file_count": len(audio_files), "engine_version": "irodori-voicedesign",
                "all_generated": len(audio_files) == len(lines),
            },
        }
        ep_dir = get_episode_dir(project_id, episode_number)
        (ep_dir / "tts.json").write_text(
            json.dumps(tts_json, ensure_ascii=False, indent=2), encoding="utf-8")
        _progress[run_key]["current_line_id"] = None
        if lines and not audio_files:
            # 全行スキップ（話者未割当等）でも"done"にすると、editing-agent側の409前提チェックは
            # status文字列しか見ないため安全ゲートを素通りしてしまう（無音タイムラインが警告無しで生成される）。
            # 全滅時はerrorに倒し、下流の409ガードを正しく機能させる。
            append_error(project_id, "tts", "ALL_LINES_SKIPPED",
                         "全行が話者未割当等でスキップされ、音声が1件も生成されませんでした", recoverable=True)
            update_status(project_id, "tts", "error", episode_number=episode_number)
        else:
            update_status(project_id, "tts", "done", episode_number=episode_number)

    except Exception as e:
        logger.error("Unexpected error in project %s ep%d: %s", project_id, episode_number, e, exc_info=True)
        append_error(project_id, "tts", "UNEXPECTED_ERROR", str(e))
        update_status(project_id, "tts", "error", episode_number=episode_number)
    finally:
        _running.pop(run_key, None)
