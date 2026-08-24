"""
shared/projects/ を読み取り専用で参照する。
director-agent は他コンテナのファイルを書き換えない（命令はAPI経由）。
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
PROJECTS_DIR = SHARED_DIR / "projects"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def list_projects() -> list[dict]:
    if not PROJECTS_DIR.exists():
        return []

    summaries = []
    for d in sorted(PROJECTS_DIR.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        pj = _read_json(d / "project.json")
        summaries.append({
            "id": pj.get("id", d.name),
            "title": pj.get("title", d.name),
            "channel": pj.get("channel", "default"),
            "episodes": pj.get("episodes", []),
        })
    return summaries


def get_project_episodes(project_id: str) -> list[dict]:
    """指定プロジェクトのエピソード一覧（ステータス・行数）を返す。"""
    matches = list(PROJECTS_DIR.glob(f"{project_id}*"))
    if not matches:
        return []
    pj_dir = matches[0]
    pj = _read_json(pj_dir / "project.json")
    ep_entries = {e["number"]: e for e in pj.get("episodes", [])}

    eps_dir = pj_dir / "episodes"
    if not eps_dir.exists():
        return list(ep_entries.values())

    result = []
    for ep_dir in sorted(eps_dir.iterdir()):
        if not ep_dir.is_dir():
            continue
        m = re.match(r"ep(\d+)$", ep_dir.name)
        if not m:
            continue
        num = int(m.group(1))
        entry = ep_entries.get(num, {"number": num, "title": f"第{num}話", "status": {}})

        has_script = (ep_dir / "script.json").exists()
        has_draft = (ep_dir / "script_draft.json").exists()
        line_count = 0
        src = ep_dir / "script.json" if has_script else (ep_dir / "script_draft.json" if has_draft else None)
        if src:
            data = _read_json(src)
            line_count = len(data.get("lines", []))

        result.append({
            "number": num,
            "title": entry.get("title", f"第{num}話"),
            "status": entry.get("status", {}),
            "has_script": has_script,
            "has_draft": has_draft,
            "line_count": line_count,
        })
    return result


def append_director_log(project_id: str, entry: dict) -> None:
    """director-agent経由で発行した命令を、プロジェクトごとの監査ログに追記する。

    各コンテナのproject.json statusとは別に、
    「いつ・どのコンテナに・何を指示したか」のトレーサビリティのみを軽量に残す。
    """
    matches = list(PROJECTS_DIR.glob(f"{project_id}*"))
    if not matches:
        return
    log_file = matches[0] / "director_log.json"
    logs: list[dict] = []
    if log_file.exists():
        try:
            logs = json.loads(log_file.read_text(encoding="utf-8"))
        except Exception:
            logs = []
    logs.append({"timestamp": datetime.now(timezone.utc).isoformat(), **entry})
    logs = logs[-200:]
    log_file.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")


def get_episode_tts(project_id: str, episode_number: int, lang: str | None = None) -> dict | None:
    """エピソードのtts.json（生成済み音声一覧・タイムライン）を返す。無ければNone。

    lang指定時は locales/{lang}/tts.json を対象にする（Docs/08_i18n.md §8b W3）。
    """
    matches = list(PROJECTS_DIR.glob(f"{project_id}*"))
    if not matches:
        return None
    ep_dir = matches[0] / "episodes" / f"ep{episode_number:02d}"
    f = (ep_dir / "locales" / lang / "tts.json") if lang else (ep_dir / "tts.json")
    if not f.exists():
        return None
    return _read_json(f)


def get_episode_script(project_id: str, episode_number: int) -> dict | None:
    """確定済みscript.json（なければscript_draft.json）を返す。どちらも無ければNone。"""
    matches = list(PROJECTS_DIR.glob(f"{project_id}*"))
    if not matches:
        return None
    ep_dir = matches[0] / "episodes" / f"ep{episode_number:02d}"
    for name in ("script.json", "script_draft.json"):
        f = ep_dir / name
        if f.exists():
            data = _read_json(f)
            data["_source"] = name
            return data
    return None


# ─── Photoshop 合成結果のQA（psassist/）───────────────────────────────
#
# psassist/ の検査は **ホスト常駐のスクリプト**（Photoshop / win32com）が走らせる。
# コンテナではないので叩ける API が存在せず、連携は共有フォルダ経由が唯一の道。
# スクリプトが episode 配下の psassist/ に qa_report.json と表示用画像を書き、
# director は既存の /shared マウントをそのまま読むだけにする。

def episode_dir(project_id: str, episode_number: int) -> Path | None:
    matches = list(PROJECTS_DIR.glob(f"{project_id}*"))
    if not matches:
        return None
    d = matches[0] / "episodes" / f"ep{episode_number:02d}"
    return d if d.is_dir() else None


def get_psassist_qa(project_id: str, episode_number: int) -> dict | None:
    """PS-Assist が書いた qa_report.json を返す。未検査ならNone。"""
    ep = episode_dir(project_id, episode_number)
    if ep is None:
        return None
    f = ep / "psassist" / "qa_report.json"
    if not f.exists():
        return None
    return _read_json(f)


def psassist_file(project_id: str, episode_number: int, rel: str) -> Path | None:
    """psassist/ 配下のファイルの実パスを返す（パストラバーサル防止）。"""
    ep = episode_dir(project_id, episode_number)
    if ep is None:
        return None
    base = (ep / "psassist").resolve()
    # レポートは "psassist/qa/thumb/line_001.jpg" の形で持っているので前置を許す
    rel = rel.lstrip("/")
    if rel.startswith("psassist/"):
        rel = rel[len("psassist/") :]
    f = (base / rel).resolve()
    if not f.is_relative_to(base) or not f.is_file():
        return None
    return f
