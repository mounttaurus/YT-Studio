"""演出プランの保存（話ごと）。

置き場: shared/projects/{pid}/episodes/epNN/opening/plan.json（最新）と history/plan_{時刻}.json（直前の版・最新20個）。
書くのは motion-agent だけ。一時ファイルに書いてから置き換える（途中で止まっても壊れたプランを残さない）。
保存する時にギミックと繋ぎの ID を正式な形に直す（別名のまま残さない）。検査に落ちても、JSON として読めれば保存する
（Claude とユーザーが一緒に詰めていく途中の状態を失わないため）。
"""
from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime
from pathlib import Path

from app.core import config, registry

KEEP_HISTORY = 20
_PID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
_HIST_RE = re.compile(r"^plan_[0-9]{8}-[0-9]{6}(-[0-9]+)?\.json$")


class PlanStoreError(ValueError):
    pass


def opening_dir(project_id: str, episode: int) -> Path:
    if not _PID_RE.match(project_id or ""):
        raise PlanStoreError(f"project_id が不正です（{project_id}）")
    if not isinstance(episode, int) or not 1 <= episode <= 999:
        raise PlanStoreError(f"episode は 1〜999 です（{episode}）")
    project = config.SHARED_DIR / "projects" / project_id
    if not project.is_dir():
        raise FileNotFoundError(f"プロジェクトがありません: {project_id}")
    return project / "episodes" / f"ep{episode:02d}" / "opening"


def canonicalize(plan: dict) -> dict:
    """ギミックと繋ぎの別名を正式な ID に直した写しを返す。"""
    p = copy.deepcopy(plan)
    beats = ((p.get("direction") or {}).get("beats")) if isinstance(p.get("direction"), dict) else None
    if isinstance(beats, dict):
        for shots in beats.values():
            for s in shots if isinstance(shots, list) else []:
                if not isinstance(s, dict):
                    continue
                s["gimmick"] = registry.canonical(s.get("gimmick"))
                if isinstance(s.get("enter"), dict):
                    s["enter"]["type"] = registry.canonical(s["enter"].get("type"))
    return p


def load(project_id: str, episode: int) -> dict | None:
    f = opening_dir(project_id, episode) / "plan.json"
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def save(project_id: str, episode: int, plan: dict) -> dict:
    """保存して、{saved_at, history_kept} を返す。"""
    if not isinstance(plan, dict):
        raise PlanStoreError("plan は JSON オブジェクトにしてください")
    d = opening_dir(project_id, episode)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "plan.json"
    hist = d / "history"
    if f.is_file():
        hist.mkdir(exist_ok=True)
        stamp = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
        dst, n = hist / f"plan_{stamp}.json", 1
        while dst.exists():
            dst = hist / f"plan_{stamp}-{n}.json"
            n += 1
        dst.write_bytes(f.read_bytes())
        for old in sorted(hist.glob("plan_*.json"))[:-KEEP_HISTORY]:
            old.unlink()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(canonicalize(plan), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, f)
    return {"saved_at": datetime.now().isoformat(timespec="seconds"),
            "history_kept": len(list(hist.glob("plan_*.json"))) if hist.is_dir() else 0}


def history(project_id: str, episode: int) -> list[str]:
    hist = opening_dir(project_id, episode) / "history"
    return sorted((p.name for p in hist.glob("plan_*.json")), reverse=True) if hist.is_dir() else []


def load_history(project_id: str, episode: int, name: str) -> dict:
    if not _HIST_RE.match(name):
        raise PlanStoreError("履歴の名前が不正です")
    f = opening_dir(project_id, episode) / "history" / name
    if not f.is_file():
        raise FileNotFoundError(name)
    return json.loads(f.read_text(encoding="utf-8"))
