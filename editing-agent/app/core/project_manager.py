"""
shared/projects/ の読み書きヘルパー。
editing-agent が読むもの: episodes/epNN/script.json, tts.json, footage.json
editing-agent が書くもの: episodes/epNN/edit/timeline.otio, subtitles.srt, edit.json
                          project.json の episodes[n].status.editing / video_edit, errors[]
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
PROJECTS_DIR = SHARED_DIR / "projects"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_project_dir(project_id: str) -> Path | None:
    if not PROJECTS_DIR.exists():
        return None
    matches = list(PROJECTS_DIR.glob(f"{project_id}*"))
    return matches[0] if matches else None


def episode_dir(project_id: str, episode_number: int) -> Path | None:
    pj_dir = find_project_dir(project_id)
    if pj_dir is None:
        return None
    d = pj_dir / "episodes" / f"ep{episode_number:02d}"
    return d if d.exists() else None


def list_projects() -> list[dict]:
    """プロジェクト一覧（エピソードごとのscript/tts/footage有無つき）。"""
    if not PROJECTS_DIR.exists():
        return []

    summaries = []
    for d in sorted(PROJECTS_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        pj = _read_json(d / "project.json")
        episodes = []
        eps_dir = d / "episodes"
        if eps_dir.exists():
            for ep_dir in sorted(eps_dir.iterdir()):
                if not ep_dir.is_dir() or not ep_dir.name.startswith("ep"):
                    continue
                try:
                    num = int(ep_dir.name[2:])
                except ValueError:
                    continue
                episodes.append({
                    "number": num,
                    "has_script": (ep_dir / "script.json").exists(),
                    "has_tts": (ep_dir / "tts.json").exists(),
                    "has_footage": (ep_dir / "footage.json").exists(),
                    "has_edit": (ep_dir / "edit" / "edit.json").exists(),
                })
        summaries.append({
            "id": pj.get("id", d.name),
            "title": pj.get("title", d.name),
            "episodes": episodes,
        })
    return summaries


def get_episode_status(project_id: str, episode_number: int) -> dict:
    """project.json の episodes[n].status を返す（無ければ空dict）。"""
    pj_dir = find_project_dir(project_id)
    if pj_dir is None:
        return {}
    pj = _read_json(pj_dir / "project.json")
    for ep in pj.get("episodes", []):
        if ep.get("number") == episode_number:
            return ep.get("status", {})
    return {}


def get_project_episodes(project_id: str) -> list[dict]:
    """エピソード一覧（project.jsonのstatus＋tts/footage/edit有無）。"""
    pj_dir = find_project_dir(project_id)
    if pj_dir is None:
        return []
    pj = _read_json(pj_dir / "project.json")
    status_by_number = {ep.get("number"): ep.get("status", {}) for ep in pj.get("episodes", [])}

    eps_dir = pj_dir / "episodes"
    if not eps_dir.exists():
        return []

    result = []
    for ep_dir in sorted(eps_dir.iterdir()):
        if not ep_dir.is_dir() or not ep_dir.name.startswith("ep"):
            continue
        try:
            num = int(ep_dir.name[2:])
        except ValueError:
            continue
        result.append({
            "number": num,
            "status": status_by_number.get(num, {}),
            "has_tts": (ep_dir / "tts.json").exists(),
            "has_footage": (ep_dir / "footage.json").exists(),
            "has_edit": (ep_dir / "edit" / "edit.json").exists(),
        })
    return result


def get_episode_script(project_id: str, episode_number: int) -> dict | None:
    ep_dir = episode_dir(project_id, episode_number)
    if ep_dir is None:
        return None
    f = ep_dir / "script.json"
    if not f.exists():
        return None
    return _read_json(f)


def get_episode_tts(project_id: str, episode_number: int) -> dict | None:
    ep_dir = episode_dir(project_id, episode_number)
    if ep_dir is None:
        return None
    f = ep_dir / "tts.json"
    if not f.exists():
        return None
    return _read_json(f)


def get_episode_speakers(project_id: str, episode_number: int) -> list[dict]:
    """そのエピソードで実際に喋る話者を出現順に返す（字幕スタイルUI用）。

    tts.json の audio_files から speaker_id→speaker_name を重複排除して収集。
    """
    tts = get_episode_tts(project_id, episode_number)
    if not tts:
        return []
    seen: dict[str, str] = {}
    for a in sorted(tts.get("audio_files", []), key=lambda a: a.get("order", 0)):
        sid = a.get("speaker_id", "")
        if sid and sid not in seen:
            seen[sid] = a.get("speaker_name", "")
    return [{"speaker_id": k, "speaker_name": v} for k, v in seen.items()]


def get_episode_footage(project_id: str, episode_number: int) -> dict | None:
    ep_dir = episode_dir(project_id, episode_number)
    if ep_dir is None:
        return None
    f = ep_dir / "footage.json"
    if not f.exists():
        return None
    return _read_json(f)


def write_edit_outputs(project_id: str, episode_number: int, otio_text: str, srt_text: str,
                       edit_json: dict, fcpxml_text: str | None = None) -> Path | None:
    """edit/timeline.otio, subtitles.srt, (任意)subtitles.fcpxml, edit.json を書き出す。戻り値はeditディレクトリ。"""
    ep_dir = episode_dir(project_id, episode_number)
    if ep_dir is None:
        return None
    edit_dir = ep_dir / "edit"
    edit_dir.mkdir(parents=True, exist_ok=True)
    (edit_dir / "timeline.otio").write_text(otio_text, encoding="utf-8")
    (edit_dir / "subtitles.srt").write_text(srt_text, encoding="utf-8")
    if fcpxml_text is not None:
        (edit_dir / "subtitles.fcpxml").write_text(fcpxml_text, encoding="utf-8")
    (edit_dir / "edit.json").write_text(json.dumps(edit_json, ensure_ascii=False, indent=2), encoding="utf-8")
    return edit_dir


def get_subtitle_style(project_id: str) -> dict:
    """project.json の config.subtitle_style を返す（無ければ空dict）。

    本籍はここ1箇所（DATA_SCHEMA 6c）。話者別スタイルは per_speaker[speaker_id]。
    """
    pj_dir = find_project_dir(project_id)
    if pj_dir is None:
        return {}
    pj = _read_json(pj_dir / "project.json")
    return (pj.get("config") or {}).get("subtitle_style", {})


def set_subtitle_style(project_id: str, style: dict) -> bool:
    """project.json の config.subtitle_style を上書き保存する。"""
    pj_dir = find_project_dir(project_id)
    if pj_dir is None:
        return False
    pj_file = pj_dir / "project.json"
    pj = _read_json(pj_file)
    pj.setdefault("config", {})["subtitle_style"] = style
    pj["updated_at"] = datetime.now(timezone.utc).isoformat()
    pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def read_edit_result(project_id: str, episode_number: int) -> dict | None:
    ep_dir = episode_dir(project_id, episode_number)
    if ep_dir is None:
        return None
    f = ep_dir / "edit" / "edit.json"
    if not f.exists():
        return None
    return _read_json(f)


def update_episode_status(project_id: str, episode_number: int, **status_updates: str) -> None:
    """project.json の episodes[n].status の指定キーを更新する。

    例: update_episode_status(pid, 1, editing="done", video_edit="pending")
    """
    pj_dir = find_project_dir(project_id)
    if pj_dir is None:
        return
    pj_file = pj_dir / "project.json"
    pj = _read_json(pj_file)
    for ep in pj.setdefault("episodes", []):
        if ep.get("number") == episode_number:
            status = ep.setdefault("status", {})
            status.update(status_updates)
            break
    pj["updated_at"] = datetime.now(timezone.utc).isoformat()
    pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")


def append_error(project_id: str, stage: str, message: str) -> None:
    pj_dir = find_project_dir(project_id)
    if pj_dir is None:
        return
    pj_file = pj_dir / "project.json"
    pj = _read_json(pj_file)
    pj.setdefault("errors", []).append({
        "stage": stage,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "recoverable": True,
    })
    pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
