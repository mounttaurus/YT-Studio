import json
import os
from datetime import datetime, timezone
from pathlib import Path

SHARED_DIR = Path(os.environ.get("SHARED_DIR", "./shared"))


def _project_path(project_id: str) -> Path:
    return SHARED_DIR / "projects" / f"{project_id}_test" if not (SHARED_DIR / "projects" / project_id).exists() else SHARED_DIR / "projects" / project_id


def _find_project_dir(project_id: str) -> Path:
    projects_dir = SHARED_DIR / "projects"
    # 完全一致
    exact = projects_dir / project_id
    if exact.exists():
        return exact
    # プレフィックス一致（例: "20260603_001" → "20260603_001_test"）
    if projects_dir.exists():
        for d in projects_dir.iterdir():
            if d.is_dir() and d.name.startswith(project_id):
                return d
    raise FileNotFoundError(f"Project not found: {project_id}")


def read_project(project_id: str) -> dict:
    project_dir = _find_project_dir(project_id)
    path = project_dir / "project.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_project(project_id: str, data: dict) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    project_dir = _find_project_dir(project_id)
    path = project_dir / "project.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def update_status(project_id: str, stage: str, status: str, episode_number: int | None = None) -> None:
    """project.json のトップレベル status[stage] を更新する。

    episode_number 指定時は episodes[n].status[stage] も同時に更新する（editing-agent等の
    409前提チェックは話ごとのstatusしか見ないため、ここを書かないと build_timeline が常に409になる）。
    """
    project = read_project(project_id)
    project.setdefault("status", {})[stage] = status
    if episode_number is not None:
        for ep in project.get("episodes", []):
            if ep.get("number") == episode_number:
                ep.setdefault("status", {})[stage] = status
                break
    write_project(project_id, project)


def append_error(project_id: str, stage: str, code: str, message: str, recoverable: bool = False) -> None:
    project = read_project(project_id)
    project.setdefault("errors", []).append({
        "stage": stage,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "code": code,
        "message": message,
        "recoverable": recoverable,
    })
    write_project(project_id, project)


def list_projects() -> list[dict]:
    projects_dir = SHARED_DIR / "projects"
    if not projects_dir.exists():
        return []
    result = []
    for d in sorted(projects_dir.iterdir()):
        if not d.is_dir():
            continue
        pj_file = d / "project.json"
        if pj_file.exists():
            try:
                data = json.loads(pj_file.read_text(encoding="utf-8"))
                result.append({"id": data.get("id", d.name), "slug": data.get("slug"), "title": data.get("title"), "status": data.get("status")})
            except Exception:
                pass
    return result


def get_project_dir(project_id: str) -> Path:
    return _find_project_dir(project_id)


def get_episode_dir(project_id: str, episode_number: int = 1) -> Path:
    """episodes/ep{NN}/ を返す。存在しなければ作成する。"""
    pj_dir = _find_project_dir(project_id)
    ep_dir = pj_dir / "episodes" / f"ep{episode_number:02d}"
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "audio").mkdir(exist_ok=True)
    return ep_dir


def get_script_path(project_id: str, episode_number: int = 1) -> Path:
    """episodes/epNN/script.json を返す。旧構造（script.json直置き）にもフォールバック。"""
    pj_dir = _find_project_dir(project_id)
    new_path = pj_dir / "episodes" / f"ep{episode_number:02d}" / "script.json"
    if new_path.exists():
        return new_path
    # 旧構造フォールバック（マイグレーション前の互換）
    old_path = pj_dir / "script.json"
    if old_path.exists():
        return old_path
    return new_path  # 存在しなくても新パスを返す（エラーは呼び出し側で処理）


def get_audio_dir(project_id: str, episode_number: int = 1) -> Path:
    """episodes/epNN/audio/ を返す。旧構造にもフォールバック。"""
    pj_dir = _find_project_dir(project_id)
    new_audio = pj_dir / "episodes" / f"ep{episode_number:02d}" / "audio"
    if new_audio.exists():
        return new_audio
    # 旧構造フォールバック
    old_audio = pj_dir / "audio"
    if old_audio.exists():
        return old_audio
    new_audio.mkdir(parents=True, exist_ok=True)
    return new_audio


def list_episodes(project_id: str) -> list[dict]:
    """エピソード一覧を返す（episodes/ がなければ ep01 相当を返す）。"""
    pj_dir = _find_project_dir(project_id)
    eps_dir = pj_dir / "episodes"
    if not eps_dir.exists():
        # 旧構造: script.json があれば ep1 として返す
        has_script = (pj_dir / "script.json").exists()
        return [{"number": 1, "has_script": has_script}] if has_script else []
    result = []
    import re
    for ep_dir in sorted(eps_dir.iterdir()):
        if not ep_dir.is_dir():
            continue
        m = re.match(r"ep(\d+)$", ep_dir.name)
        if not m:
            continue
        num = int(m.group(1))
        result.append({
            "number": num,
            "has_script": (ep_dir / "script.json").exists(),
            "has_audio": (ep_dir / "audio").exists() and any((ep_dir / "audio").glob("*.wav")),
        })
    return result
