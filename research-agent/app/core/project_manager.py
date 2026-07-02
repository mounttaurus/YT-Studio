"""
shared/projects/ 以下の読み書き。research-agent は以下を扱う:
  - research_sources.json  … 取り込み済みソースの作業セット
  - research.json          … 蒸留の来歴（出力・DATA_SCHEMA §3 / 1.1.0）
  - rough_script.txt       … ラフ台本の実体（scripting-agent が無改修で消費）
project.json は読みのみ（research 工程の状態は research.json 側で持つ＝project.json 無改修）。
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
PROJECTS_DIR = SHARED_DIR / "projects"
RESEARCH_SCHEMA_VERSION = "1.1.0"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_project_dir(project_id: str) -> Path:
    """既存プロジェクトディレクトリを返す（前方一致。無ければそのIDで作る）。"""
    if PROJECTS_DIR.exists():
        matches = sorted(PROJECTS_DIR.glob(f"{project_id}*"))
        if matches:
            return matches[0]
    path = PROJECTS_DIR / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_projects() -> list[dict]:
    if not PROJECTS_DIR.exists():
        return []
    out = []
    for d in sorted(PROJECTS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        pj = d / "project.json"
        title, pid = d.name, d.name
        if pj.exists():
            try:
                data = json.loads(pj.read_text(encoding="utf-8"))
                title = data.get("title", d.name)
                pid = data.get("id", d.name)
            except (OSError, json.JSONDecodeError):
                pass
        out.append({
            "id": pid,
            "dir": d.name,
            "title": title,
            "has_research": (d / "research.json").exists(),
            "has_rough_script": (d / "rough_script.txt").exists(),
        })
    return out


def read_project(project_id: str) -> Optional[dict]:
    f = get_project_dir(project_id) / "project.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


# ─── ソース作業セット（research_sources.json） ───────────────────────────

def load_sources(project_id: str) -> list[dict]:
    f = get_project_dir(project_id) / "research_sources.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8")).get("sources", [])
        except (OSError, json.JSONDecodeError):
            return []
    return []


def save_sources(project_id: str, sources: list[dict]) -> None:
    f = get_project_dir(project_id) / "research_sources.json"
    f.write_text(
        json.dumps({"sources": sources, "updated_at": _now()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_source(project_id: str, kind: str, title: str, text: str, url: Optional[str] = None) -> dict:
    sources = load_sources(project_id)
    next_n = len(sources) + 1
    src = {
        "id": f"S{next_n}",
        "kind": kind,
        "title": title,
        "url": url,
        "text": text or "",
        "chars": len(text or ""),
        "added_at": _now(),
    }
    sources.append(src)
    save_sources(project_id, sources)
    return src


def delete_source(project_id: str, source_id: str) -> bool:
    sources = load_sources(project_id)
    new = [s for s in sources if s.get("id") != source_id]
    if len(new) == len(sources):
        return False
    save_sources(project_id, new)
    return True


# ─── 出力（rough_script.txt / research.json） ────────────────────────────

def save_rough_script(project_id: str, text: str) -> Path:
    """ラフ台本の実体。scripting-agent の read_rough_script() がここを読む（無改修連携）。"""
    f = get_project_dir(project_id) / "rough_script.txt"
    f.write_text(text, encoding="utf-8")
    return f


def read_rough_script(project_id: str) -> Optional[str]:
    f = get_project_dir(project_id) / "rough_script.txt"
    return f.read_text(encoding="utf-8") if f.exists() else None


def save_research(project_id: str, research: dict) -> None:
    f = get_project_dir(project_id) / "research.json"
    f.write_text(json.dumps(research, ensure_ascii=False, indent=2), encoding="utf-8")


def read_research(project_id: str) -> Optional[dict]:
    f = get_project_dir(project_id) / "research.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None
