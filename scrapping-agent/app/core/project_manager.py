"""
shared/projects/ の読み書きヘルパー。
scrapping-agent が読むもの: episodes/epNN/script.json
scrapping-agent が書くもの: episodes/epNN/footage_draft.json, footage.json, footage/
"""
import json
import os
from pathlib import Path

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
PROJECTS_DIR = SHARED_DIR / "projects"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_project_dir(project_id: str) -> Path | None:
    matches = list(PROJECTS_DIR.glob(f"{project_id}*"))
    return matches[0] if matches else None


def episode_dir(project_id: str, episode_number: int) -> Path | None:
    pj_dir = find_project_dir(project_id)
    if pj_dir is None:
        return None
    d = pj_dir / "episodes" / f"ep{episode_number:02d}"
    return d if d.exists() else None


def get_episode_script(project_id: str, episode_number: int) -> dict | None:
    """承認済みscript.jsonのみを返す（draftは素材収集の対象外）。"""
    ep_dir = episode_dir(project_id, episode_number)
    if ep_dir is None:
        return None
    f = ep_dir / "script.json"
    if not f.exists():
        return None
    return _read_json(f)


def list_projects() -> list[dict]:
    """プロジェクト一覧（エピソードごとのscript有無・footage状況つき）。"""
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
                    "has_footage_draft": (ep_dir / "footage_draft.json").exists(),
                    "has_footage": (ep_dir / "footage.json").exists(),
                })
        summaries.append({
            "id": pj.get("id", d.name),
            "title": pj.get("title", d.name),
            "episodes": episodes,
        })
    return summaries


def read_footage_draft(project_id: str, episode_number: int) -> dict | None:
    ep_dir = episode_dir(project_id, episode_number)
    if ep_dir is None:
        return None
    f = ep_dir / "footage_draft.json"
    if not f.exists():
        return None
    return _read_json(f)


def write_footage_draft(project_id: str, episode_number: int, data: dict) -> bool:
    ep_dir = episode_dir(project_id, episode_number)
    if ep_dir is None:
        return False
    f = ep_dir / "footage_draft.json"
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def read_footage(project_id: str, episode_number: int) -> dict | None:
    ep_dir = episode_dir(project_id, episode_number)
    if ep_dir is None:
        return None
    f = ep_dir / "footage.json"
    if not f.exists():
        return None
    return _read_json(f)


def write_footage(project_id: str, episode_number: int, data: dict) -> bool:
    """確定済みfootage.jsonを書き出す。"""
    ep_dir = episode_dir(project_id, episode_number)
    if ep_dir is None:
        return False
    f = ep_dir / "footage.json"
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def update_footage_status(project_id: str, episode_number: int, status: str) -> None:
    """project.json の episodes[n].status.footage を更新する。"""
    pj_dir = find_project_dir(project_id)
    if pj_dir is None:
        return
    pj_file = pj_dir / "project.json"
    pj = _read_json(pj_file)
    for ep in pj.setdefault("episodes", []):
        if ep.get("number") == episode_number:
            ep.setdefault("status", {})["footage"] = status
            break
    pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")


def append_error(project_id: str, message: str) -> None:
    pj_dir = find_project_dir(project_id)
    if pj_dir is None:
        return
    pj_file = pj_dir / "project.json"
    pj = _read_json(pj_file)
    pj.setdefault("errors", []).append({"agent": "scrapping-agent", "message": message})
    pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
