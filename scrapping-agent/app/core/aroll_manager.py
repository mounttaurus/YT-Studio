"""
Aロール（マンガ形式パネル）のマニフェスト管理＋バッチ生成エンジン。

正本: shared/projects/{id}/episodes/epNN/a_roll/aroll.json
画像: shared/projects/{id}/episodes/epNN/a_roll/panel_{order:03d}_{line_id}.png

設計方針:
- マニフェストの prompt は「演出部分」のみ（aroll_prompt_generator参照）。
  生成時に スタイル接頭辞＋キャラ外見＋固定サフィックス（no text等）を合成する。
- バッチは直列実行（並列なし）＋リクエスト間インターバル（AROLL_MIN_INTERVAL_SEC、既定3秒）。
  429/5xx/timeout/DNS解決失敗等の接続エラーは指数バックオフで最大3回リトライ → 失敗行は failed マークで続行。
- 1行終わるごとにマニフェストを書き出す＝中断・再開（only_missing）が常に安全。
- OpenRouterへの課金自動退避は allow_paid_fallback=True の時だけ許可（既定OFF）。
- **台本との同期状態（sync）は保存しない**。パネルには「画像を生成した時の台本テキスト」
  （source_text / source_text_hash）だけを刻み、現在の script.json と読み取り時に突き合わせて
  ok/stale/missing/orphan を算出する（保存すると sync 自体が陳腐化するため）。
"""
import asyncio
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core import character_manager, nanobanana_client, project_manager, style_manager

SCHEMA_VERSION = "1.0.0"
MIN_INTERVAL_SEC = float(os.getenv("AROLL_MIN_INTERVAL_SEC", "3"))
RETRY_BACKOFF_SEC = [5, 15, 45]

# 固定サフィックス: 吹き出しはユーザーが後乗せするため画像内の文字を禁止する
PROMPT_SUFFIX = "No text, no letters, no speech bubbles, no watermark in the image."

# 背景はLLMに書かせず常にこれで統一する（時間帯/シチュエーションのブレを防ぐ）。
# ユーザーが後から背景だけ別途生成して合成する運用が前提（2026-07-25方針）。
BACKGROUND_FRAGMENT = "plain solid pastel background, flat single color, no scenery"

_RETRYABLE_MARKERS = ("429", "RESOURCE_EXHAUSTED", "500", "502", "503", "504",
                      "timeout", "Timeout", "timed out")

# 台本との同期状態（保存しない・読み取り時に算出する）
SYNC_OK = "ok"            # 画像あり・生成時テキストと現在の台本が一致
SYNC_STALE = "stale"      # 画像はあるが台本テキストが変わった＝絵が古い
SYNC_MISSING = "missing"  # 行はあるが画像が無い（未生成/失敗/台本に後から追加された行）
SYNC_ORPHAN = "orphan"    # パネルはあるが台本から行が消えた
SYNC_UNKNOWN = "unknown"  # 画像はあるが生成時テキスト未記録（この機能以前に生成された資産）

SYNC_STATES = (SYNC_OK, SYNC_STALE, SYNC_MISSING, SYNC_ORPHAN, SYNC_UNKNOWN)

# 正規化で落とす記号（句読点・括弧・引用符など）。長音「ー」や中黒以外の表意文字は残す。
_PUNCT = "。、，．,.!！?？…‥「」『』〈〉《》【】（）()［］[]｛｝{}\"'“”‘’:：;；"
_WS_RE = re.compile(r"\s+")
_PUNCT_TABLE = {ord(c): None for c in _PUNCT}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_text(text: str | None) -> str:
    """比較用にセリフを正規化する（全半角統一・空白除去・句読点/括弧除去）。

    「、」を「。」に直した程度の推敲で stale 判定が出ないようにするための正規化。
    意味が変わる語句の差し替えは当然ハッシュが変わる。
    """
    t = unicodedata.normalize("NFKC", text or "")
    t = _WS_RE.sub("", t)
    return t.translate(_PUNCT_TABLE)


def text_hash(text: str | None) -> str:
    """正規化後テキストの短縮SHA1。空文字なら空を返す（＝未記録と区別しない）。"""
    n = normalize_text(text)
    return hashlib.sha1(n.encode("utf-8")).hexdigest()[:16] if n else ""


def aroll_dir(project_id: str, episode: int) -> Path | None:
    ep_dir = project_manager.episode_dir(project_id, episode)
    if ep_dir is None:
        return None
    return ep_dir / "a_roll"


def manifest_path(project_id: str, episode: int) -> Path | None:
    d = aroll_dir(project_id, episode)
    return None if d is None else d / "aroll.json"


def load_manifest(project_id: str, episode: int) -> dict | None:
    f = manifest_path(project_id, episode)
    if f is None or not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_manifest(project_id: str, episode: int, manifest: dict) -> bool:
    f = manifest_path(project_id, episode)
    if f is None:
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def get_speaker_map(project_id: str) -> dict[str, dict]:
    """project.json config.tts.speakers[] を {speaker_id: {name, character_id}} で返す（配役の正本）。"""
    pj_dir = project_manager.find_project_dir(project_id)
    if pj_dir is None:
        return {}
    pj = project_manager._read_json(pj_dir / "project.json")
    speakers = ((pj.get("config") or {}).get("tts") or {}).get("speakers") or []
    return {
        s["id"]: {"name": s.get("name", ""), "character_id": s.get("character_id") or ""}
        for s in speakers if s.get("id")
    }


def get_cast_characters(project_id: str) -> dict[str, dict]:
    """配役に登場するキャラの {char_id: {name, appearance_prompt}} を返す。"""
    chars: dict[str, dict] = {}
    for sp in get_speaker_map(project_id).values():
        cid = sp.get("character_id")
        if not cid or cid in chars:
            continue
        c = character_manager.read_character(cid)
        if c is None:
            continue
        chars[cid] = {
            "name": c.get("name", cid),
            "appearance_prompt": c.get("appearance_prompt", ""),
        }
    return chars


def build_or_update_manifest(
    project_id: str, episode: int, script: dict,
    prompts_by_line: dict[str, dict],
    aspect: str = "16:9", style: str = "kamishibai",
    overwrite: bool = False,
) -> dict:
    """script.json の行順にマニフェストを構築/更新する。

    既存パネルは line_id で引き継ぐ:
    - 生成済み画像(status/image)は常に保持
    - prompt は overwrite=True か既存が空の時だけ新プロンプトで置き換える
      （ユーザー編集 prompt_source="user" は overwrite=True でも保持）
    - 台本から消えた行の生成済みパネルは削除せず orphan=True を立てて末尾に残す
      （黙って消すと「削除した行の画像がディスクに残っている」事実が見えなくなるため）
    """
    old = load_manifest(project_id, episode) or {}
    old_panels = {p.get("line_id"): p for p in old.get("panels", [])}
    speaker_map = get_speaker_map(project_id)

    panels = []
    for i, ln in enumerate(script.get("lines", []), 1):
        if not (ln.get("text") or "").strip():
            continue  # 空セリフ行はパネル不要（無駄な生成を防ぐ）
        lid = ln.get("id")
        speaker = speaker_map.get(ln.get("speaker_id"), {})
        prev = old_panels.get(lid, {})
        new = prompts_by_line.get(lid, {})

        keep_prompt = prev.get("prompt", "")
        keep_source = prev.get("prompt_source", "")
        if new.get("prompt") and (overwrite or not keep_prompt) and keep_source != "user":
            prompt, source = new["prompt"], "llm"
            characters = new.get("characters") or prev.get("characters") or []
            # このプロンプトは「今の台本テキスト」から作られた
            prompt_text_hash = text_hash(ln.get("text"))
        else:
            prompt, source = keep_prompt, keep_source or ("llm" if keep_prompt else "")
            characters = prev.get("characters") or new.get("characters") or []
            if not characters and speaker.get("character_id"):
                characters = [speaker["character_id"]]
            prompt_text_hash = prev.get("prompt_text_hash", "")

        panels.append({
            "line_id": lid,
            "order": ln.get("order", i),
            "section": ln.get("section") or "main",
            "speaker_id": ln.get("speaker_id", ""),
            "speaker_name": speaker.get("name") or ln.get("speaker_name", ""),
            "text": ln.get("text", ""),
            "characters": characters,
            "prompt": prompt,
            "prompt_source": source,
            "prompt_text_hash": prompt_text_hash,
            "status": prev.get("status", "pending"),
            "image": prev.get("image"),
            "provider": prev.get("provider"),
            "error": prev.get("error"),
            "generated_at": prev.get("generated_at"),
            # 画像を生成した時点の台本テキスト（stale判定の唯一の根拠）
            "source_text": prev.get("source_text", ""),
            "source_text_hash": prev.get("source_text_hash", ""),
        })

    # 台本から消えた行のうち画像を持つものは証拠として残す（バッチ対象からは常に除外）
    live_ids = {p["line_id"] for p in panels}
    for lid, prev in old_panels.items():
        if lid in live_ids or prev.get("status") != "done":
            continue
        panels.append({**prev, "orphan": True})

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "episode": episode,
        "aspect": (old.get("aspect") if not overwrite else None) or aspect,
        "style": (old.get("style") if not overwrite else None) or style,
        "generated_at": _now(),
        "panels": panels,
    }
    save_manifest(project_id, episode, manifest)
    return manifest


def update_line(
    project_id: str, episode: int, line_id: str,
    prompt: str | None = None, characters: list[str] | None = None,
) -> dict | None:
    """ユーザーによる行編集。promptを書き換えたら prompt_source="user" にする。"""
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        return None
    for p in manifest["panels"]:
        if p.get("line_id") == line_id:
            if prompt is not None:
                p["prompt"] = prompt.strip()
                p["prompt_source"] = "user"
                # 手書きプロンプトは「今の台本テキスト」を見て書かれたものとみなす
                p["prompt_text_hash"] = text_hash(p.get("text"))
            if characters is not None:
                p["characters"] = [c for c in characters if c][:2]
            save_manifest(project_id, episode, manifest)
            return p
    return None


# ---------------------------------------------------------------------------
# 台本との同期判定（読み取り時に算出・マニフェストには保存しない）
# ---------------------------------------------------------------------------

def _script_lines_by_id(project_id: str, episode: int, script: dict | None = None) -> dict[str, dict]:
    """パネル対象になる台本行を {line_id: line} で返す（空セリフ行は対象外）。"""
    if script is None:
        script = project_manager.get_episode_script(project_id, episode)
    return {
        l.get("id"): l
        for l in (script or {}).get("lines", [])
        if l.get("id") and (l.get("text") or "").strip()
    }


def _panel_sync(panel: dict, line: dict | None, out_dir: Path | None) -> str:
    """1パネルの同期状態を判定する（台本行 line が正・panel.orphan は参考にしない）。"""
    if line is None:
        return SYNC_ORPHAN
    img = panel.get("image")
    if panel.get("status") != "done" or not img:
        return SYNC_MISSING
    if out_dir is not None and not (out_dir / img).exists():
        return SYNC_MISSING  # マニフェストはdoneだが実ファイルが無い（手動削除など）
    prev_hash = panel.get("source_text_hash")
    if not prev_hash:
        return SYNC_UNKNOWN
    return SYNC_OK if prev_hash == text_hash(line.get("text")) else SYNC_STALE


def sync_report(project_id: str, episode: int, script: dict | None = None) -> dict:
    """確定台本と aroll.json の差分レポートを返す（生成も保存もしない・純粋な検査）。

    items[] は台本の order 順（orphan は末尾）。UI のバッジと「同期が必要な行」一覧の唯一の供給元。
    """
    manifest = load_manifest(project_id, episode)
    counts = {s: 0 for s in SYNC_STATES}
    if manifest is None:
        return {"has_manifest": False, "has_script": False, "in_sync": False,
                "counts": counts, "prompt_stale_count": 0, "items": []}

    lines_by_id = _script_lines_by_id(project_id, episode, script)
    out_dir = aroll_dir(project_id, episode)
    items: list[dict] = []
    seen: set[str] = set()
    prompt_stale = 0

    for p in manifest.get("panels", []):
        lid = p.get("line_id")
        if not lid:
            continue
        seen.add(lid)
        line = lines_by_id.get(lid)
        state = _panel_sync(p, line, out_dir)
        counts[state] += 1
        cur_text = (line or {}).get("text", "")
        # プロンプト自体も古いテキストから作られていないか（Phase2の再生成範囲の判断材料）
        p_stale = bool(
            line is not None and (p.get("prompt") or "").strip()
            and p.get("prompt_text_hash") and p["prompt_text_hash"] != text_hash(cur_text)
        )
        if p_stale:
            prompt_stale += 1
        items.append({
            "line_id": lid,
            "order": p.get("order"),
            "section": p.get("section", ""),
            "speaker_name": p.get("speaker_name", ""),
            "sync": state,
            "status": p.get("status", "pending"),
            "image": p.get("image"),
            "current_text": cur_text,
            "source_text": p.get("source_text", ""),
            "prompt_stale": p_stale,
        })

    # マニフェストに存在しない台本行＝台本に後から追加された行
    for lid, line in lines_by_id.items():
        if lid in seen:
            continue
        counts[SYNC_MISSING] += 1
        items.append({
            "line_id": lid,
            "order": line.get("order"),
            "section": line.get("section") or "main",
            "speaker_name": line.get("speaker_name", ""),
            "sync": SYNC_MISSING,
            "status": "no_panel",
            "image": None,
            "current_text": line.get("text", ""),
            "source_text": "",
            "prompt_stale": False,
        })

    items.sort(key=lambda it: (it["sync"] == SYNC_ORPHAN, it.get("order") or 0))
    return {
        "has_manifest": True,
        "has_script": bool(lines_by_id),
        "in_sync": counts[SYNC_STALE] == 0 and counts[SYNC_MISSING] == 0
                   and counts[SYNC_ORPHAN] == 0 and counts[SYNC_UNKNOWN] == 0,
        "counts": counts,
        "prompt_stale_count": prompt_stale,
        "items": items,
    }


def annotate_manifest(project_id: str, episode: int, manifest: dict) -> dict:
    """マニフェストのコピーに sync を付けて返す（レスポンス専用・ファイルには書かない）。"""
    lines_by_id = _script_lines_by_id(project_id, episode)
    out_dir = aroll_dir(project_id, episode)
    out = dict(manifest)
    out["panels"] = [
        {**p, "sync": _panel_sync(p, lines_by_id.get(p.get("line_id")), out_dir)}
        for p in manifest.get("panels", [])
    ]
    return out


def accept_current_text(
    project_id: str, episode: int, line_ids: list[str] | None = None,
) -> dict:
    """生成済みパネルの「生成時テキスト」を現在の台本テキストで確定する（画像は再生成しない）。

    - line_ids 省略時は unknown（記録が無い既存資産）だけを対象にする＝安全な移行用。
    - line_ids 指定時は stale も対象にできる＝「この程度の推敲なら絵はこのままでよい」の追認。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        return {"accepted": [], "skipped": []}

    lines_by_id = _script_lines_by_id(project_id, episode)
    out_dir = aroll_dir(project_id, episode)
    wanted = set(line_ids) if line_ids else None
    accepted, skipped = [], []

    for p in manifest.get("panels", []):
        lid = p.get("line_id")
        line = lines_by_id.get(lid)
        state = _panel_sync(p, line, out_dir)
        if wanted is not None and lid not in wanted:
            continue
        if state not in (SYNC_UNKNOWN, SYNC_STALE) or (wanted is None and state != SYNC_UNKNOWN):
            skipped.append({"line_id": lid, "sync": state})
            continue
        h = text_hash(line.get("text"))
        p["source_text"] = line.get("text", "")
        p["source_text_hash"] = h
        # 絵を追認するならプロンプトも現テキスト基準とみなす（旧資産のブートストラップ）
        if (p.get("prompt") or "").strip():
            p["prompt_text_hash"] = h
        accepted.append(lid)

    if accepted:
        save_manifest(project_id, episode, manifest)
    return {"accepted": accepted, "skipped": skipped}


# ---------------------------------------------------------------------------
# 画像生成（1行＋バッチ）
# ---------------------------------------------------------------------------

def _compose_prompt(panel: dict, style_name: str) -> str:
    """スタイル接頭辞＋キャラ外見＋演出プロンプト＋固定サフィックスを合成する。"""
    style = style_manager.get_style(style_name) or {}
    char_parts = []
    for cid in panel.get("characters", [])[:2]:
        c = character_manager.read_character(cid)
        if c and (c.get("appearance_prompt") or "").strip():
            char_parts.append(f"{c.get('name') or cid} — {c['appearance_prompt'].strip()}")
    char_block = ("Featured characters: " + "; ".join(char_parts) + ". ") if char_parts else ""
    prefix = (style.get("prefix") or "").strip()
    return (
        f"{prefix} {char_block}{panel.get('prompt', '')}, {BACKGROUND_FRAGMENT}. {PROMPT_SUFFIX}"
    ).strip()


def _resolve_refs(characters: list[str]) -> list[tuple[bytes, str, str]]:
    """キャラごとの参照画像を解決する（1人=最大2枚、2人=各1枚、合計3枚以内）。

    ラベルにはキャラ名を入れてNanoBananaに役割を伝える。参照が無いキャラはスキップ
    （appearance_promptのみで生成）。
    """
    chars = [c for c in characters if c][:2]
    per_char = 2 if len(chars) <= 1 else 1
    refs: list[tuple[bytes, str, str]] = []
    for cid in chars:
        c = character_manager.read_character(cid)
        name = (c or {}).get("name") or cid
        ref_dir = character_manager.char_dir(cid) / "reference"
        if not ref_dir.exists():
            continue
        files = sorted(
            (p for p in ref_dir.glob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )[:per_char]
        for p in files:
            label = f"{name}: keep this character consistent (same face, hairstyle, outfit)"
            refs.append((p.read_bytes(), nanobanana_client.mime_for(p.name), label))
    return refs[:3]


def _is_retryable(err: Exception) -> bool:
    # DNS解決失敗・接続拒否・接続タイムアウト等の下位ネットワーク層エラーは常にリトライ対象。
    # 文字列マーカーだけだと "[Errno -3] Temporary failure in name resolution" のような
    # OSレベルのDNS一時的失敗（Docker Desktop/WSL2で稀に起きる）が1回で即失敗していた。
    if isinstance(err, httpx.TransportError):
        return True
    s = str(err)
    return any(m in s for m in _RETRYABLE_MARKERS)


async def _generate_with_retry(
    prompt: str, refs: list[tuple], aspect: str, allow_paid_fallback: bool,
    log: list[str] | None = None,
) -> bytes:
    """指数バックオフ付きでNanoBanana生成（最大リトライ3回）。"""
    last: Exception | None = None
    for attempt in range(len(RETRY_BACKOFF_SEC) + 1):
        try:
            return await nanobanana_client.generate_one(
                prompt, refs, aspect=aspect, allow_fallback=allow_paid_fallback,
            )
        except Exception as e:
            last = e
            if attempt >= len(RETRY_BACKOFF_SEC) or not _is_retryable(e):
                raise
            wait = RETRY_BACKOFF_SEC[attempt]
            if log is not None:
                log.append(f"retry {attempt + 1}: {str(e)[:120]} → {wait}s待機")
            await asyncio.sleep(wait)
    raise last  # 到達しない


async def generate_line_image(
    project_id: str, episode: int, line_id: str,
    allow_paid_fallback: bool = False, log: list[str] | None = None,
) -> dict:
    """1行分のパネル画像を生成してマニフェストへ反映する（成功/失敗とも記録）。"""
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found (run /aroll/prompts first)")
    panel = next((p for p in manifest["panels"] if p.get("line_id") == line_id), None)
    if panel is None:
        raise ValueError(f"line not found in aroll.json: {line_id}")
    if panel.get("orphan"):
        raise ValueError(f"台本から削除された行です（生成しません）: {line_id}")
    if not (panel.get("prompt") or "").strip():
        raise ValueError(f"prompt is empty: {line_id}")

    out_dir = aroll_dir(project_id, episode)
    out_dir.mkdir(parents=True, exist_ok=True)
    full_prompt = _compose_prompt(panel, manifest.get("style", "kamishibai"))
    refs = _resolve_refs(panel.get("characters", []))

    try:
        data = await _generate_with_retry(
            full_prompt, refs, manifest.get("aspect", "16:9"), allow_paid_fallback, log,
        )
        filename = f"panel_{int(panel.get('order', 0)):03d}_{line_id}.png"
        (out_dir / filename).write_bytes(data)
        panel.update({
            "status": "done", "image": filename, "provider": "nanobanana",
            "error": None, "generated_at": _now(),
            # この絵が「どのセリフから描かれたか」を刻む＝後で台本が変わったら stale と分かる
            "source_text": panel.get("text", ""),
            "source_text_hash": text_hash(panel.get("text")),
        })
    except Exception as e:
        panel.update({"status": "failed", "error": str(e)[:300]})
        raise
    finally:
        # 成否に関わらず都度書き出す＝レジューム安全
        save_manifest(project_id, episode, manifest)
    return panel


# ---------------------------------------------------------------------------
# バッチジョブ（エピソードごとに1つ。モジュール内状態＝TTSのstatusパターン踏襲）
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}


def _job_key(project_id: str, episode: int) -> str:
    return f"{project_id}:ep{episode:02d}"


def get_job(project_id: str, episode: int) -> dict | None:
    return _JOBS.get(_job_key(project_id, episode))


def is_running(project_id: str, episode: int) -> bool:
    job = get_job(project_id, episode)
    return bool(job and job.get("running"))


def request_stop(project_id: str, episode: int) -> bool:
    job = get_job(project_id, episode)
    if job and job.get("running"):
        job["cancel"] = True
        return True
    return False


def select_targets(
    manifest: dict, line_ids: list[str] | None, only_missing: bool,
) -> list[dict]:
    """バッチ対象パネルを選ぶ。only_missing=True なら done を除外（＝レジューム/失敗再試行）。

    台本から消えた行（orphan）は明示指定を含め常に除外する（消えたセリフの絵に課金しない）。
    """
    panels = [p for p in manifest.get("panels", []) if not p.get("orphan")]
    if line_ids:
        wanted = set(line_ids)
        panels = [p for p in panels if p.get("line_id") in wanted]
    if only_missing:
        panels = [p for p in panels if p.get("status") != "done"]
    return [p for p in panels if (p.get("prompt") or "").strip()]


async def run_batch(
    project_id: str, episode: int,
    line_ids: list[str] | None = None,
    only_missing: bool = True,
    allow_paid_fallback: bool = False,
) -> None:
    """バッチ本体（asyncio.create_task で起動される）。直列＋インターバル＋失敗続行。"""
    key = _job_key(project_id, episode)
    manifest = load_manifest(project_id, episode) or {}
    targets = select_targets(manifest, line_ids, only_missing)
    job = _JOBS[key] = {
        "running": True, "cancel": False,
        "total": len(targets), "done": 0, "failed": 0,
        "current_line": None, "log": [],
        "started_at": _now(), "finished_at": None,
        "allow_paid_fallback": allow_paid_fallback,
    }
    log: list[str] = job["log"]

    try:
        for i, panel in enumerate(targets):
            if job["cancel"]:
                log.append(f"中断しました（{job['done']}枚生成済み）")
                break
            lid = panel["line_id"]
            job["current_line"] = lid
            try:
                await generate_line_image(
                    project_id, episode, lid,
                    allow_paid_fallback=allow_paid_fallback, log=log,
                )
                job["done"] += 1
                log.append(f"✔ {lid} 生成完了 ({job['done']}/{job['total']})")
            except Exception as e:
                job["failed"] += 1
                log.append(f"✘ {lid} 失敗: {str(e)[:150]}")
            if i < len(targets) - 1 and not job["cancel"]:
                await asyncio.sleep(MIN_INTERVAL_SEC)
    finally:
        job["running"] = False
        job["current_line"] = None
        job["finished_at"] = _now()


def status(project_id: str, episode: int) -> dict:
    """ジョブ状態＋マニフェスト集計を返す（ポーリング用）。"""
    manifest = load_manifest(project_id, episode)
    counts = {"total": 0, "done": 0, "failed": 0, "pending": 0, "no_prompt": 0}
    if manifest:
        for p in manifest.get("panels", []):
            if p.get("orphan"):
                continue  # 台本から消えた行は進捗の母数に入れない（sync側で報告する）
            counts["total"] += 1
            if not (p.get("prompt") or "").strip():
                counts["no_prompt"] += 1
            st = p.get("status", "pending")
            counts[st if st in counts else "pending"] += 1
    job = get_job(project_id, episode) or {}
    report = sync_report(project_id, episode)
    return {
        "has_manifest": manifest is not None,
        "counts": counts,
        "sync": report["counts"],
        "in_sync": report["in_sync"],
        "job": {k: v for k, v in job.items() if k != "cancel"},
        "running": bool(job.get("running")),
    }
