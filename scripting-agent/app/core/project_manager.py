"""
shared/projects/ 以下のプロジェクトファイルを読み書きする。
schema_version 2.0.0: episodes/ サブディレクトリ構造。
"""
import json
import os
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
PROJECTS_DIR = SHARED_DIR / "projects"
SCHEMA_VERSION = "2.0.0"
DEFAULT_LIMIT = 10


# ─── ディレクトリ操作 ────────────────────────────────────────────────

def get_project_dir(project_id: str) -> Path:
    matches = list(PROJECTS_DIR.glob(f"{project_id}*"))
    if matches:
        return matches[0]
    path = PROJECTS_DIR / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_project_dir(project_id: str) -> Optional[Path]:
    """既存プロジェクトのディレクトリを返す（無ければNone。get_project_dirと違い新規作成しない）。"""
    if not PROJECTS_DIR.exists():
        return None
    matches = [d for d in PROJECTS_DIR.glob(f"{project_id}*") if d.is_dir()]
    return matches[0] if matches else None


def delete_project(project_id: str) -> bool:
    """プロジェクトディレクトリを完全に削除する（台本・素材・音声・編集情報を含む）。元に戻せない。"""
    d = find_project_dir(project_id)
    if d is None:
        return False
    shutil.rmtree(d)
    return True


def get_episode_dir(project_id: str, episode_number: int = 1) -> Path:
    """episodes/ep{NN}/ ディレクトリを返す（なければ作成）。"""
    ep_dir = get_project_dir(project_id) / "episodes" / f"ep{episode_number:02d}"
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "audio").mkdir(exist_ok=True)
    return ep_dir


# ─── 新規プロジェクト作成 ─────────────────────────────────────────────

def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_-]+", "_", text).strip("_")
    slug = re.sub(r"[^\x00-\x7F]", "", text).lower()
    return slug[:30] if slug else "project"


def _next_seq(date_str: str) -> str:
    if not PROJECTS_DIR.exists():
        return "001"
    existing = [d.name for d in PROJECTS_DIR.iterdir() if d.is_dir() and d.name.startswith(date_str)]
    max_seq = 0
    for name in existing:
        m = re.match(r"\d{8}_(\d{3})", name)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return f"{max_seq + 1:03d}"


def create_project(title: str, slug: Optional[str] = None, channel: str = "default") -> dict:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    seq = _next_seq(date_str)
    auto_slug = slug or _slugify(title) or "project"
    project_id = f"{date_str}_{seq}_{auto_slug}"

    pj_dir = PROJECTS_DIR / project_id
    pj_dir.mkdir(parents=True, exist_ok=True)
    (pj_dir / "output").mkdir(exist_ok=True)
    # ep01 を初期エピソードとして作成
    get_episode_dir(project_id, 1)

    pj = {
        "schema_version": SCHEMA_VERSION,
        "id": project_id,
        "slug": auto_slug,
        "title": title,
        "channel": channel,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "language": "ja",
        "style": "",
        "series_mode": False,
        "episodes": [
            _make_episode_entry(1, title),
        ],
        "pipeline_config": {
            "style_id": "",
            "llm_model": "",
            "tts_engine": "",
            "auto_approve": False,
        },
        "config": {
            "llm_model": "",
            "tts_engine": "",
            "target_duration_sec": 300,
            "speakers": [],
        },
        "errors": [],
    }
    (pj_dir / "project.json").write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
    return pj


def _make_episode_entry(number: int, title: str = "") -> dict:
    return {
        "number": number,
        "title": title or f"第{number}話",
        "status": {
            "scripting": "not_started",
            "tts": "not_started",
            "footage": "not_started",
            "video_edit": "not_started",
        },
    }


# ─── エピソード管理 ────────────────────────────────────────────────────

def list_episodes(project_id: str) -> list[dict]:
    """episodes/ フォルダ内のエピソード一覧をサマリーとして返す。"""
    pj_dir = get_project_dir(project_id)
    eps_dir = pj_dir / "episodes"
    if not eps_dir.exists():
        return []

    pj = read_project(project_id)
    ep_entries = {e["number"]: e for e in pj.get("episodes", [])}

    result = []
    for ep_dir in sorted(eps_dir.iterdir()):
        if not ep_dir.is_dir():
            continue
        m = re.match(r"ep(\d+)$", ep_dir.name)
        if not m:
            continue
        num = int(m.group(1))
        entry = ep_entries.get(num, _make_episode_entry(num))

        # ドラフト/確定の存在確認
        has_draft = (ep_dir / "script_draft.json").exists()
        has_script = (ep_dir / "script.json").exists()

        # 行数・タイトル・推定時間をドラフトから読む
        line_count = 0
        episode_title = entry.get("title", f"第{num}話")
        estimated_duration_sec = None
        src = ep_dir / "script_draft.json" if has_draft else (ep_dir / "script.json" if has_script else None)
        if src:
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
                line_count = len(data.get("lines", []))
                meta = data.get("metadata", {})
                estimated_duration_sec = meta.get("estimated_duration_sec")
                series_meta = meta.get("series", {})
                if series_meta.get("episode_title"):
                    episode_title = series_meta["episode_title"]
                    entry["title"] = episode_title
            except Exception:
                pass

        result.append({
            "number": num,
            "title": episode_title,
            "has_draft": has_draft,
            "has_script": has_script,
            "line_count": line_count,
            "estimated_duration_sec": estimated_duration_sec,
            "status": entry.get("status", {}),
        })

    return result


def ensure_episode_in_project_json(project_id: str, number: int, title: str = "") -> None:
    """project.json の episodes[] に指定番号が存在しなければ追加する。"""
    pj_dir = get_project_dir(project_id)
    pj_file = pj_dir / "project.json"
    if not pj_file.exists():
        return
    pj = json.loads(pj_file.read_text(encoding="utf-8"))
    episodes = pj.setdefault("episodes", [])
    nums = {e["number"] for e in episodes}
    if number not in nums:
        episodes.append(_make_episode_entry(number, title))
        episodes.sort(key=lambda e: e["number"])
        pj["updated_at"] = datetime.now(timezone.utc).isoformat()
        pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")


def update_episode_status(project_id: str, episode_number: int, stage: str, status: str) -> None:
    pj_dir = get_project_dir(project_id)
    pj_file = pj_dir / "project.json"
    if not pj_file.exists():
        return
    pj = json.loads(pj_file.read_text(encoding="utf-8"))
    for ep in pj.get("episodes", []):
        if ep["number"] == episode_number:
            ep.setdefault("status", {})[stage] = status
            break
    pj["updated_at"] = datetime.now(timezone.utc).isoformat()
    pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── ドラフト / 確定スクリプト ─────────────────────────────────────────

def save_draft(project_id: str, script_json: dict, episode_number: int = 1):
    ep_dir = get_episode_dir(project_id, episode_number)
    ensure_episode_in_project_json(project_id, episode_number)
    (ep_dir / "script_draft.json").write_text(
        json.dumps(script_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_draft(project_id: str, episode_number: int = 1) -> Optional[dict]:
    ep_dir = get_episode_dir(project_id, episode_number)
    f = ep_dir / "script_draft.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return None


def read_script(project_id: str, episode_number: int = 1) -> Optional[dict]:
    """確定済みスクリプト（script.json）を返す。"""
    ep_dir = get_episode_dir(project_id, episode_number)
    f = ep_dir / "script.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return None


def save_script(project_id: str, script_json: dict, episode_number: int = 1) -> None:
    """確定済みスクリプト（script.json）を上書き保存する（行操作のライトスルー同期用）。"""
    ep_dir = get_episode_dir(project_id, episode_number)
    (ep_dir / "script.json").write_text(
        json.dumps(script_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─── 行番号付きテキスト書き出し（Illustrator流し込み用） ──────────────────

def build_lines_text(script_json: dict) -> str:
    """台本を「行番号\t[話者名] セリフ」形式のプレーンテキストにする。

    吹き出し画像づくり（Illustrator等）でセリフを流し込む際の元データ。
    1行=1セリフ。セリフ内の改行は半角スペースに畳んで1行に収める。
    """
    lines = (script_json or {}).get("lines", [])
    out = []
    for i, ln in enumerate(lines, 1):
        order = ln.get("order", i)
        speaker = ln.get("speaker_name") or ln.get("speaker_id") or ""
        # セリフ内の改行だけ半角スペースに畳む。全角スペース等はそのまま温存する。
        text = re.sub(r"[\r\n]+", " ", ln.get("text") or "").strip()
        out.append(f"{order}\t[{speaker}] {text}")
    return "\n".join(out)


def save_script_lines_text(project_id: str, text: str, episode_number: int = 1) -> Path:
    """行番号付きテキストを episodes/epNN/script_lines.txt として保存する。"""
    ep_dir = get_episode_dir(project_id, episode_number)
    f = ep_dir / "script_lines.txt"
    f.write_text(text, encoding="utf-8")
    return f


def approve_script(project_id: str, episode_number: int = 1) -> Path:
    ep_dir = get_episode_dir(project_id, episode_number)
    draft_file = ep_dir / "script_draft.json"
    script_file = ep_dir / "script.json"

    if not draft_file.exists():
        raise FileNotFoundError(f"ドラフトが見つかりません: {draft_file}")

    draft = json.loads(draft_file.read_text(encoding="utf-8"))
    draft["metadata"]["checked_by_director"] = True
    draft["metadata"]["check_passed"] = True
    script_file.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")

    update_episode_status(project_id, episode_number, "scripting", "done")
    update_project_status(project_id, "scripting", "done")
    return script_file


# ─── 名前付きドラフト ────────────────────────────────────────────────

def save_named_draft(project_id: str, name: str, script_json: dict) -> Path:
    pj_dir = get_project_dir(project_id)
    drafts_dir = pj_dir / "drafts"
    drafts_dir.mkdir(exist_ok=True)
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    if not safe_name:
        safe_name = "draft"
    f = drafts_dir / f"{safe_name}.json"
    f.write_text(json.dumps(script_json, ensure_ascii=False, indent=2), encoding="utf-8")
    return f


def list_named_drafts(project_id: str) -> list[dict]:
    pj_dir = get_project_dir(project_id)
    drafts_dir = pj_dir / "drafts"
    if not drafts_dir.exists():
        return []
    result = []
    for f in sorted(drafts_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            meta = data.get("metadata", {})
            result.append({
                "name": f.stem,
                "line_count": meta.get("line_count", len(data.get("lines", []))),
                "style_name": meta.get("style_name", meta.get("style", "")),
                "estimated_duration_sec": meta.get("estimated_duration_sec"),
                "saved_at": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
            })
        except Exception:
            pass
    return result


def get_named_draft(project_id: str, name: str) -> Optional[dict]:
    pj_dir = get_project_dir(project_id)
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    f = pj_dir / "drafts" / f"{safe_name}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def delete_named_draft(project_id: str, name: str) -> bool:
    pj_dir = get_project_dir(project_id)
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    f = pj_dir / "drafts" / f"{safe_name}.json"
    if not f.exists():
        return False
    f.unlink()
    return True


# ─── プロジェクト一覧 ────────────────────────────────────────────────

def _read_project_summary(d: Path) -> dict:
    pj_file = d / "project.json"
    if pj_file.exists():
        try:
            pj = json.loads(pj_file.read_text(encoding="utf-8"))
            return {
                "id": pj.get("id", d.name),
                "slug": pj.get("slug", ""),
                "title": pj.get("title", d.name),
                "channel": pj.get("channel", "default"),
                "status": pj.get("status", {}),
                "updated_at": pj.get("updated_at", ""),
                "episode_count": len(pj.get("episodes", [])),
            }
        except Exception:
            pass
    return {
        "id": d.name, "slug": "", "title": d.name,
        "channel": "default", "status": {}, "updated_at": "", "episode_count": 0,
    }


def list_projects(
    search: Optional[str] = None,
    channel: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    if not PROJECTS_DIR.exists():
        return {"projects": [], "total": 0, "has_more": False}

    all_dirs = sorted(
        [d for d in PROJECTS_DIR.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    summaries = [_read_project_summary(d) for d in all_dirs]

    if channel and channel != "all":
        summaries = [s for s in summaries if s["channel"] == channel]

    if search and search.strip():
        q = search.strip().lower()
        summaries = [s for s in summaries if q in s["title"].lower() or q in s["id"].lower()]

    total = len(summaries)
    paged = summaries[offset: offset + limit]
    return {"projects": paged, "total": total, "has_more": (offset + limit) < total}


def get_channels() -> list[str]:
    if not PROJECTS_DIR.exists():
        return ["default"]
    channels = set()
    for d in PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        pj_file = d / "project.json"
        if pj_file.exists():
            try:
                pj = json.loads(pj_file.read_text(encoding="utf-8"))
                channels.add(pj.get("channel", "default"))
            except Exception:
                channels.add("default")
    return sorted(channels) if channels else ["default"]


def _set_channel_for_projects(old: str, new: str) -> int:
    """channel が old の全プロジェクトの channel フィールドを new に書き換える。書き換えた件数を返す。"""
    if not PROJECTS_DIR.exists():
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    count = 0
    for d in PROJECTS_DIR.iterdir():
        if not d.is_dir():
            continue
        pj_file = d / "project.json"
        if not pj_file.exists():
            continue
        try:
            pj = json.loads(pj_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if pj.get("channel", "default") != old:
            continue
        pj["channel"] = new
        pj["updated_at"] = now_iso
        pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
        count += 1
    return count


def rename_channel(old: str, new: str) -> int:
    """チャンネル名 old を new にリネーム（属する全プロジェクトの channel を書き換え）。書き換えた件数を返す。"""
    new = (new or "").strip() or "default"
    if not old or old == new:
        return 0
    return _set_channel_for_projects(old, new)


def delete_channel(channel: str) -> int:
    """チャンネルを削除する＝属する全プロジェクトを 'default' へ再割当（非破壊）。再割当した件数を返す。"""
    if not channel or channel == "default":
        return 0
    return _set_channel_for_projects(channel, "default")


# ─── 単一プロジェクト操作 ────────────────────────────────────────────

def read_project(project_id: str) -> dict:
    pj_dir = get_project_dir(project_id)
    pj_file = pj_dir / "project.json"
    if pj_file.exists():
        return json.loads(pj_file.read_text(encoding="utf-8"))
    return {"id": project_id, "status": {}, "episodes": [], "errors": []}


def update_project_status(project_id: str, stage: str, status: str, error: Optional[str] = None):
    pj_dir = get_project_dir(project_id)
    pj_file = pj_dir / "project.json"
    if pj_file.exists():
        pj = json.loads(pj_file.read_text(encoding="utf-8"))
    else:
        pj = {"id": project_id, "status": {}, "errors": [], "created_at": datetime.now(timezone.utc).isoformat()}

    pj.setdefault("status", {})[stage] = status
    pj["updated_at"] = datetime.now(timezone.utc).isoformat()
    if error:
        pj.setdefault("errors", []).append({"stage": stage, "message": error, "at": pj["updated_at"]})

    pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_research(project_id: str) -> Optional[dict]:
    pj_dir = get_project_dir(project_id)
    f = pj_dir / "research.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return None


def read_rough_script(project_id: str) -> Optional[str]:
    pj_dir = get_project_dir(project_id)
    f = pj_dir / "rough_script.txt"
    if f.exists():
        return f.read_text(encoding="utf-8")
    return None


def save_rough_script(project_id: str, text: str):
    pj_dir = get_project_dir(project_id)
    (pj_dir / "rough_script.txt").write_text(text, encoding="utf-8")


def sync_tts_speakers(project_id: str, speakers: list[dict]):
    """スタイルの話者を project.json の cast（config.tts.speakers）へ同期する。

    役→キャラ割当の唯一の本籍は project.json（DATA_SCHEMA §2 config.tts.speakers[].character_id）。
    スタイルはその初期値（定番配役）を供給するだけ。よってここは「上書き」ではなく「マージ」:
    - project 側に既に割り当て済みの character_id はユーザーの選択として温存（台本再生成で手割当を消さない）。
    - 未割当の役にだけスタイルの character_id を初期値として流し込む。
    - スタイルに無く project にだけ在る役（TTS/台本でのセリフ追加＝EXCEPTION由来）は捨てずに保持。
    - character_id が無い旧スタイルは後方互換でインライン voice/caption を引き継ぐ。
    """
    pj_dir = get_project_dir(project_id)
    pj_file = pj_dir / "project.json"
    if pj_file.exists():
        pj = json.loads(pj_file.read_text(encoding="utf-8"))
    else:
        pj = {"id": project_id, "status": {}, "errors": [], "created_at": datetime.now(timezone.utc).isoformat()}

    existing = {
        sp["id"]: sp
        for sp in pj.get("config", {}).get("tts", {}).get("speakers", [])
        if sp.get("id")
    }

    merged = []
    for sp in speakers:
        sid = sp["id"]
        prev = existing.get(sid, {})
        out = {"id": sid, "name": sp.get("name", sid), "role": sp.get("role", "")}
        # project側の手割当を最優先。無ければスタイルの character_id を初期値に。
        char_id = prev.get("character_id") or sp.get("character_id", "")
        if char_id:
            out["character_id"] = char_id
        else:
            # 旧スタイル（character_id 無し）は後方互換でインライン声/字幕を引き継ぐ
            out["voice"] = prev.get("voice") or sp.get("voice_id", "")
            out["caption"] = prev.get("caption", "")
        merged.append(out)

    # スタイルに無く project にだけ在る役（EXCEPTION: セリフ追加で増えた登場人物）を温存
    style_ids = {sp["id"] for sp in speakers}
    for sid, prev in existing.items():
        if sid not in style_ids:
            merged.append(prev)

    pj.setdefault("config", {}).setdefault("tts", {})["speakers"] = merged
    pj["updated_at"] = datetime.now(timezone.utc).isoformat()
    pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")


def save_project_style(project_id: str, style_id: str):
    pj_dir = get_project_dir(project_id)
    pj_file = pj_dir / "project.json"
    if pj_file.exists():
        pj = json.loads(pj_file.read_text(encoding="utf-8"))
    else:
        pj = {"id": project_id, "status": {}, "errors": [], "created_at": datetime.now(timezone.utc).isoformat()}
    pj["style"] = style_id
    pj["updated_at"] = datetime.now(timezone.utc).isoformat()
    pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")


def get_project_style(project_id: str) -> str:
    return read_project(project_id).get("style", "")


# ─── マイグレーション ────────────────────────────────────────────────

def _is_migrated(pj_dir: Path) -> bool:
    """schema_version 2.0.0 以降ならマイグレーション済みと判断する。"""
    pj_file = pj_dir / "project.json"
    if not pj_file.exists():
        return False
    try:
        pj = json.loads(pj_file.read_text(encoding="utf-8"))
        ver = pj.get("schema_version", "1.0.0")
        major = int(ver.split(".")[0])
        return major >= 2
    except Exception:
        return False


def migrate_project(project_id: str) -> str:
    """
    旧構造 → episodes/ 構造へ移行する。
    バックアップを _backup_{date}/ に作成してから変換する。
    戻り値: "migrated" | "already_migrated" | "skipped"
    """
    pj_dir = get_project_dir(project_id)
    if _is_migrated(pj_dir):
        return "already_migrated"

    # ── バックアップ作成 ──
    date_str = datetime.now().strftime("%Y%m%d")
    backup_dir = pj_dir / f"_backup_{date_str}"
    if not backup_dir.exists():
        shutil.copytree(pj_dir, backup_dir, ignore=shutil.ignore_patterns("_backup_*"))

    episodes_dir = pj_dir / "episodes"
    episodes_dir.mkdir(exist_ok=True)

    migrated_episodes: list[int] = []

    # ── script_draft.json → episodes/ep01/script_draft.json ──
    old_draft = pj_dir / "script_draft.json"
    if old_draft.exists():
        ep01_dir = episodes_dir / "ep01"
        ep01_dir.mkdir(exist_ok=True)
        (ep01_dir / "audio").mkdir(exist_ok=True)
        shutil.copy2(old_draft, ep01_dir / "script_draft.json")
        old_draft.unlink()
        if 1 not in migrated_episodes:
            migrated_episodes.append(1)

    # ── script.json → episodes/ep01/script.json ──
    old_script = pj_dir / "script.json"
    if old_script.exists():
        ep01_dir = episodes_dir / "ep01"
        ep01_dir.mkdir(exist_ok=True)
        (ep01_dir / "audio").mkdir(exist_ok=True)
        shutil.copy2(old_script, ep01_dir / "script.json")
        old_script.unlink()
        if 1 not in migrated_episodes:
            migrated_episodes.append(1)

    # ── audio/ → episodes/ep01/audio/ ──
    old_audio = pj_dir / "audio"
    if old_audio.exists() and any(old_audio.iterdir()):
        ep01_dir = episodes_dir / "ep01"
        ep01_dir.mkdir(exist_ok=True)
        new_audio = ep01_dir / "audio"
        new_audio.mkdir(exist_ok=True)
        for f in old_audio.iterdir():
            if f.is_file():
                shutil.copy2(f, new_audio / f.name)
        shutil.rmtree(old_audio)
        if 1 not in migrated_episodes:
            migrated_episodes.append(1)
    elif old_audio.exists():
        shutil.rmtree(old_audio)

    # ── series/script_epNN.json → episodes/epNN/script_draft.json ──
    old_series = pj_dir / "series"
    if old_series.exists():
        for f in sorted(old_series.glob("script_ep*.json")):
            m = re.match(r"script_ep(\d+)\.json$", f.name)
            if not m:
                continue
            num = int(m.group(1))
            ep_dir = episodes_dir / f"ep{num:02d}"
            ep_dir.mkdir(exist_ok=True)
            (ep_dir / "audio").mkdir(exist_ok=True)
            shutil.copy2(f, ep_dir / "script_draft.json")
            if num not in migrated_episodes:
                migrated_episodes.append(num)
        shutil.rmtree(old_series)

    migrated_episodes.sort()
    if not migrated_episodes:
        # ファイルが何もなくてもep01フォルダは作る
        ep01_dir = episodes_dir / "ep01"
        ep01_dir.mkdir(exist_ok=True)
        (ep01_dir / "audio").mkdir(exist_ok=True)
        migrated_episodes = [1]

    # ── project.json を 2.0.0 へ更新 ──
    pj_file = pj_dir / "project.json"
    if pj_file.exists():
        pj = json.loads(pj_file.read_text(encoding="utf-8"))
    else:
        pj = {"id": project_id, "status": {}, "errors": []}

    pj["schema_version"] = SCHEMA_VERSION
    pj.setdefault("series_mode", len(migrated_episodes) > 1)
    pj.setdefault("pipeline_config", {
        "style_id": pj.get("style", ""),
        "llm_model": pj.get("config", {}).get("llm_model", ""),
        "tts_engine": pj.get("config", {}).get("tts_engine", ""),
        "auto_approve": False,
    })

    # episodes[] を構築
    existing_eps = {e["number"]: e for e in pj.get("episodes", [])}
    new_eps = []
    for num in migrated_episodes:
        entry = existing_eps.get(num, _make_episode_entry(num))
        # 旧 scripting status を ep01 に引き継ぐ
        if num == 1:
            old_scripting = pj.get("status", {}).get("scripting", "not_started")
            entry.setdefault("status", {})["scripting"] = old_scripting
        new_eps.append(entry)
    pj["episodes"] = new_eps
    pj["updated_at"] = datetime.now(timezone.utc).isoformat()

    pj_file.write_text(json.dumps(pj, ensure_ascii=False, indent=2), encoding="utf-8")
    return "migrated"


def migrate_all_projects() -> dict:
    """PROJECTS_DIR 以下の全プロジェクトをマイグレーションする。起動時に呼ぶ。"""
    if not PROJECTS_DIR.exists():
        return {"total": 0, "migrated": 0, "already_migrated": 0, "skipped": 0}

    counts = {"total": 0, "migrated": 0, "already_migrated": 0, "skipped": 0}
    for d in PROJECTS_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        counts["total"] += 1
        try:
            result = migrate_project(d.name)
            counts[result] = counts.get(result, 0) + 1
        except Exception:
            counts["skipped"] += 1
    return counts
