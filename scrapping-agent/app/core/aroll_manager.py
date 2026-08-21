"""
Aロール（マンガ形式パネル）のマニフェスト管理＋バッチ生成エンジン。

正本: shared/projects/{id}/episodes/epNN/a_roll/aroll.json
画像: shared/projects/{id}/episodes/epNN/a_roll/panel_{line_id}.png
       （2026-08-09〜。line_id は不変キー。tts-agent の audio/{line_id}.wav と同原則＝
       　台本の行挿入/削除でファイル名が動く導出値(order)を、永続する実体のファイル名に
       　焼かない。旧形式 panel_{order:03d}_{line_id}.png は normalize_panel_filenames で移行）

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
- **order は「保存するもの」ではなく「提示するもの」**。Photoshop作業など人間が順番に触る
  導線が要る場合は export_for_manual_work() が a_roll/export/ に order付きの使い捨てコピーを
  作る（正本 a_roll/ は一切リネームしない＝作業中PSDからのリンクを壊さない）。
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

from app.core import (
    aroll_prompt_generator, character_manager, nanobanana_client, panel_library_manager,
    project_manager, style_manager,
)

SCHEMA_VERSION = "1.1.0"
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


def compute_slot_key(characters: list[str] | None, slot: dict | None) -> str | None:
    """画像再利用の照合キー（2026-08-19新規）。

    emotion/shot/angle の3軸のみ使う（poseを含めると細分化しすぎて重複率が落ちるため実測で
    除外。詳細はDocs/AROLL_SLOT_REUSE_BRIEF.md §2-2）。1軸でも欠けていればNone。
    """
    if not slot:
        return None
    emotion, shot, angle = slot.get("emotion"), slot.get("shot"), slot.get("angle")
    if not (emotion and shot and angle):
        return None
    chars_key = ",".join(sorted(c for c in (characters or []) if c))
    return f"{chars_key}|{emotion}|{shot}|{angle}"


def _library_lookup(panel: dict) -> dict | None:
    """パネルの演技スロットにキャラ所有ライブラリ（Phase 3）の一致があれば返す。

    単独キャラのパネルのみ対象（2ショットはライブラリ非対応）。世代違い
    （appearance_version不一致）は panel_library_manager.find_current 側で除外される。
    """
    chars = [c for c in (panel.get("characters") or []) if c]
    if len(chars) != 1:
        return None
    slot = panel.get("slot") or {}
    emotion, shot, angle = slot.get("emotion"), slot.get("shot"), slot.get("angle")
    if not (emotion and shot and angle):
        return None
    entry = panel_library_manager.find_current(chars[0], emotion, shot, angle)
    if entry is None:
        return None
    return {"char_id": chars[0], **entry}


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
            # スロットも新プロンプトと一緒に更新する（同じ分岐＝promptとslotの世代がズレない）
            slot = new.get("slot")
            slot_source = new.get("slot_source") or ("none" if slot is None else "derived")
        else:
            prompt, source = keep_prompt, keep_source or ("llm" if keep_prompt else "")
            characters = prev.get("characters") or new.get("characters") or []
            if not characters and speaker.get("character_id"):
                characters = [speaker["character_id"]]
            prompt_text_hash = prev.get("prompt_text_hash", "")
            slot = prev.get("slot")
            slot_source = prev.get("slot_source") or "none"

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
            "slot": slot,
            "slot_key": compute_slot_key(characters, slot),
            "slot_source": slot_source,
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
                # 古いslotを残すと別の演技のdedup対象に誤って混ざるため、正規表現で再計算する
                # （LLM呼び出し不要・課金なし。語彙外なら slot=None・slot_source="none" に落ちる）
                slot = aroll_prompt_generator.derive_slot_from_prompt(p["prompt"])
                if not (slot["emotion"] and slot["shot"] and slot["angle"]):
                    slot = None
                p["slot"] = slot
                p["slot_source"] = "derived" if slot else "none"
            if characters is not None:
                p["characters"] = [c for c in characters if c][:2]
            p["slot_key"] = compute_slot_key(p.get("characters"), p.get("slot"))
            save_manifest(project_id, episode, manifest)
            return p
    return None


# ---------------------------------------------------------------------------
# 演技スロット（2026-08-19新規・Phase1）
# 画像再利用の下地。ここではスロットを記録するだけで、生成そのものは変えない
# （再利用ロジックはPhase2で別途実装）。詳細はDocs/AROLL_SLOT_REUSE_BRIEF.md。
# ---------------------------------------------------------------------------

AROLL_COST_PER_IMAGE_USD = float(os.getenv("AROLL_COST_PER_IMAGE_USD", "0.04"))


def backfill_slots(project_id: str, episode: int, force: bool = False) -> dict:
    """既存パネルのpromptから正規表現でslotを後埋めする（画像には一切触れない・冪等）。

    force=False（既定）: 既にslot_keyを持つ行はスキップ（何度呼んでも安全）。
    force=True: 全行を正規表現分類で上書き（llm由来のslotも含めて再計算したい時のみ使う）。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found (run /aroll/prompts first)")

    updated = skipped = orphaned = 0
    for p in manifest.get("panels", []):
        if p.get("orphan"):
            orphaned += 1
            continue
        if not force and p.get("slot_key"):
            skipped += 1
            continue
        prompt_text = p.get("prompt", "")
        if not prompt_text.strip():
            skipped += 1
            continue
        derived = aroll_prompt_generator.derive_slot_from_prompt(prompt_text)
        has_all = bool(derived["emotion"] and derived["shot"] and derived["angle"])
        p["slot"] = derived
        p["slot_source"] = "derived" if has_all else "none"
        p["slot_key"] = compute_slot_key(p.get("characters"), derived if has_all else None)
        updated += 1

    save_manifest(project_id, episode, manifest)
    return {
        "updated": updated, "skipped": skipped, "orphaned": orphaned,
        "total": len(manifest.get("panels", [])),
    }


def slot_report(project_id: str, episode: int) -> dict:
    """スロット別集計・ユニーク数・削減見込み枚数・$概算を返す（検査のみ・何も変更しない）。"""
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found (run /aroll/prompts first)")

    panels = [
        p for p in manifest.get("panels", [])
        if not p.get("orphan") and (p.get("text") or "").strip()
    ]
    total = len(panels)

    source_counts = {"llm": 0, "derived": 0, "none": 0}
    for p in panels:
        source_counts[p.get("slot_source") or "none"] = (
            source_counts.get(p.get("slot_source") or "none", 0) + 1
        )

    groups: dict[str, int] = {}
    for p in panels:
        key = p.get("slot_key")
        if key:
            groups[key] = groups.get(key, 0) + 1
    keyed = sum(groups.values())
    unique = len(groups)
    reusable = sum(n - 1 for n in groups.values())
    top = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)[:20]

    return {
        "total_panels": total,
        "keyed_panels": keyed,
        "unkeyed_panels": total - keyed,
        "unique_slots": unique,
        "duplicate_rate_pct": round(100 * (1 - unique / keyed), 1) if keyed else 0.0,
        "reusable_count": reusable,
        "estimated_savings_usd": round(reusable * AROLL_COST_PER_IMAGE_USD, 2),
        "cost_per_image_usd": AROLL_COST_PER_IMAGE_USD,
        "slot_source_counts": source_counts,
        "top_slots": [{"slot_key": k, "count": n} for k, n in top],
    }


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
# ファイル名の正規化（移行）と、人間の作業用の書き出し（export）
# ---------------------------------------------------------------------------

def normalize_panel_filenames(project_id: str, episode: int) -> dict:
    """旧形式 panel_{order}_{line_id}.ext を panel_{line_id}.ext へリネームする（冪等）。

    line_id は一意なので新形式同士の衝突は起きない。既に新形式のパネルはスキップする。
    aroll.json の image フィールドも同時に更新する。export/ の書き出しはこの正規化を前提とする
    （正規化されていないと export の欠番判定がずれるため）。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        return {"renamed": [], "skipped": [], "errors": []}

    out_dir = aroll_dir(project_id, episode)
    renamed, skipped, errors = [], [], []
    changed = False

    for p in manifest.get("panels", []):
        img = p.get("image")
        lid = p.get("line_id")
        if not img or not lid:
            continue
        ext = img.rsplit(".", 1)[-1] if "." in img else "png"
        new_name = panel_filename(lid, ext)
        if img == new_name:
            continue
        src = out_dir / img
        dst = out_dir / new_name
        if not src.exists():
            skipped.append({"line_id": lid, "reason": "file not found", "image": img})
            continue
        if dst.exists():
            # 通常起きない（line_id一意のため）が、万一の衝突は上書きせず報告する
            errors.append({"line_id": lid, "reason": "target already exists", "target": new_name})
            continue
        try:
            src.rename(dst)
        except OSError as e:
            errors.append({"line_id": lid, "reason": str(e)[:200]})
            continue
        p["image"] = new_name
        renamed.append({"line_id": lid, "from": img, "to": new_name})
        changed = True

    if changed:
        save_manifest(project_id, episode, manifest)
    return {"renamed": renamed, "skipped": skipped, "errors": errors}


def export_for_manual_work(project_id: str, episode: int) -> dict:
    """Photoshop等の手作業向けに a_roll/export/ へ order 付きの使い捨てコピーを作る。

    正本 a_roll/*.png は一切リネームしない（作業中PSDからのリンクを壊さないため）。
    export/ は毎回クリーンして作り直す＝前回の番号が残って混乱することがない。
    欠番（画像未生成の行）はそのまま飛ばす。stale/orphan は README に列挙するだけで
    コピーはしない（stale=古い絵をそのまま書き出すと気付かず使ってしまうため）。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")

    # 正規化されていないと現在のorderとファイル名が食い違ったまま書き出してしまう
    normalize_panel_filenames(project_id, episode)
    manifest = load_manifest(project_id, episode)

    script = project_manager.get_episode_script(project_id, episode)
    if script is None:
        raise ValueError("approved script.json not found")
    lines = [
        l for l in script.get("lines", [])
        if l.get("id") and (l.get("text") or "").strip()
    ]

    out_dir = aroll_dir(project_id, episode)
    export_dir = out_dir / "export"
    if export_dir.exists():
        for f in export_dir.iterdir():
            if f.is_file():
                f.unlink()
    else:
        export_dir.mkdir(parents=True)

    panels_by_id = {p.get("line_id"): p for p in manifest.get("panels", [])}
    report = sync_report(project_id, episode, script)
    sync_by_id = {it["line_id"]: it["sync"] for it in report["items"]}

    speaker_names = {}
    for l in lines:
        speaker_names.setdefault(l.get("speaker_id"), l.get("speaker_name", ""))

    text_rows = [
        "# 台本 ⇔ Aロール画像 対応表（export/ 書き出し時に自動生成・毎回作り直されます）",
        "#",
    ]
    exported, missing, stale = [], [], []

    for l in lines:
        lid = l["id"]
        order = l["order"]
        panel = panels_by_id.get(lid)
        state = sync_by_id.get(lid)
        img = panel.get("image") if panel else None
        speaker = panel.get("speaker_name") if panel else l.get("speaker_name", "")

        if img and (out_dir / img).exists() and state != "stale":
            ext = img.rsplit(".", 1)[-1] if "." in img else "png"
            dst_name = f"{order:03d}_{lid}.{ext}"
            (export_dir / dst_name).write_bytes((out_dir / img).read_bytes())
            exported.append({"order": order, "line_id": lid, "file": dst_name})
            text_rows.append(f"{order}\t{dst_name}\t{speaker}\t{l['text']}")
        elif state == "stale":
            stale.append({"order": order, "line_id": lid})
            text_rows.append(f"{order}\t★絵が古い(未書き出し・line_id={lid})\t{speaker}\t{l['text']}")
        else:
            missing.append({"order": order, "line_id": lid})
            text_rows.append(f"{order}\t★未生成(line_id={lid})\t{speaker}\t{l['text']}")

    (export_dir / "script_lines.txt").write_text(
        "\n".join(text_rows) + "\n", encoding="utf-8-sig",
    )

    readme = [
        "Aロール Photoshop作業用 書き出し",
        f"生成日時: {_now()}",
        "",
        "このフォルダは書き出しのたびに全消去→作り直されます。ここにあるファイルへの",
        "作業結果（PSD等）は別フォルダに保存してください（このフォルダ自体には保存しない）。",
        "",
        f"書き出し済み: {len(exported)}枚",
        f"欠番（画像未生成・番号を飛ばしています）: {len(missing)}行",
        (", ".join(str(m['order']) for m in missing) if missing else "なし"),
        f"絵が古い（台本が変わったが未再生成・書き出していません）: {len(stale)}行",
        (", ".join(str(s['order']) for s in stale) if stale else "なし"),
    ]
    (export_dir / "_README.txt").write_text("\n".join(readme) + "\n", encoding="utf-8-sig")

    return {
        "export_dir": str(export_dir),
        "exported_count": len(exported),
        "missing": missing,
        "stale": stale,
    }


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


def panel_filename(line_id: str, ext: str = "png") -> str:
    """パネル画像の正本ファイル名（line_idのみ・orderを含まない＝不変）。"""
    return f"panel_{line_id}.{ext}"


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
    use_library: bool = True,
) -> dict:
    """1行分のパネル画像を生成してマニフェストへ反映する（成功/失敗とも記録）。

    use_library=True（既定）: 先にキャラ所有ライブラリ（Phase 3）を引き、一致すれば
    無料でコピーして即返す（NanoBananaは呼ばない）。「この行だけ作り直す」時は
    use_library=False を渡してライブラリを迂回し、必ず新規生成させる。
    """
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

    if use_library:
        lib_hit = _library_lookup(panel)
        if lib_hit is not None:
            src = panel_library_manager.library_dir(lib_hit["char_id"]) / lib_hit["image"]
            filename = panel_filename(line_id)
            old_image = panel.get("image")
            (out_dir / filename).write_bytes(src.read_bytes())
            if old_image and old_image != filename:
                (out_dir / old_image).unlink(missing_ok=True)
            panel.update({
                "status": "done", "image": filename, "provider": lib_hit.get("provider", "nanobanana"),
                "error": None, "generated_at": _now(),
                "source_text": panel.get("text", ""),
                "source_text_hash": text_hash(panel.get("text")),
                "image_source": "library",
                "library_slot_id": lib_hit.get("slot_id"),
            })
            save_manifest(project_id, episode, manifest)
            panel_library_manager.record_usage(lib_hit["char_id"], lib_hit["slot_id"])
            if log is not None:
                log.append(f"📚 {line_id} ライブラリから引用: {lib_hit['char_id']}/{lib_hit.get('slot_id')}")
            return panel

    full_prompt = _compose_prompt(panel, manifest.get("style", "kamishibai"))
    refs = _resolve_refs(panel.get("characters", []))

    try:
        data = await _generate_with_retry(
            full_prompt, refs, manifest.get("aspect", "16:9"), allow_paid_fallback, log,
        )
        filename = panel_filename(line_id)
        old_image = panel.get("image")
        (out_dir / filename).write_bytes(data)
        if old_image and old_image != filename:
            # 旧形式(panel_{order}_{line_id}.png)や作り直し前の孤児を残さない
            (out_dir / old_image).unlink(missing_ok=True)
        panel.update({
            "status": "done", "image": filename, "provider": "nanobanana",
            "error": None, "generated_at": _now(),
            # この絵が「どのセリフから描かれたか」を刻む＝後で台本が変わったら stale と分かる
            "source_text": panel.get("text", ""),
            "source_text_hash": text_hash(panel.get("text")),
            # 実際に画像生成したことを刻む（コピーで済ませた行 image_source="copied" と区別する）
            "image_source": "generated",
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


# ---------------------------------------------------------------------------
# 生成プラン（2026-08-19新規・Phase2）: 同一バッチ内で同じ演技スロットを使い回す。
# max_reuse=1（既定）なら全行が個別生成＝現行と完全に同一挙動。
# クールダウンは別ロジックにせず「ラウンドロビン割当」に埋め込む（同一variantの間隔が
# 自動的にvariants個ぶん空く）。既存の生成済み画像（バッチ対象外）は再利用元にしない
# （今回のスコープ外。将来拡張はDocs/AROLL_SLOT_REUSE_BRIEF.md §4-5参照）。
# ---------------------------------------------------------------------------

def _assign_variants(group_panels: list[dict], max_reuse: int, min_gap: int) -> dict[str, int]:
    """スロットが同じグループ内でvariantをラウンドロビン割当する。

    orderでソートし i % variants で割り振る＝同一variantの最小間隔は自動的にvariants個ぶん空く。
    その間隔(order差)がmin_gap未満ならvariant数を増やして割り直す（グループ全員が別variantに
    なれば重複は起きないので、最大でグループ人数まで増やせば必ず収束する）。
    """
    ordered = sorted(group_panels, key=lambda p: p.get("order", 0))
    n = len(ordered)
    variants = max(1, -(-n // max_reuse))  # ceil(n / max_reuse)
    while variants < n:
        assign = {p["line_id"]: i % variants for i, p in enumerate(ordered)}
        by_variant: dict[int, list[int]] = {}
        for p in ordered:
            by_variant.setdefault(assign[p["line_id"]], []).append(p.get("order", 0))
        gap_ok = all(
            b - a >= min_gap
            for orders in by_variant.values()
            for a, b in zip(sorted(orders), sorted(orders)[1:])
        )
        if gap_ok:
            return assign
        variants += 1
    return {p["line_id"]: i for i, p in enumerate(ordered)}


def build_generation_plan(
    targets: list[dict], max_reuse: int = 1, min_gap: int = 8, use_library: bool = True,
) -> dict:
    """targets(select_targetsの出力)を「ライブラリ引用」「実生成する代表行」「コピーで済む行」に振り分ける。

    ライブラリ引用（Phase 3・use_library）が最優先: 単独キャラのパネルでキャラ所有ライブラリに
    一致（かつappearance_versionが最新）があれば、バッチ内dedupより先にそちらを使う（$0）。
    残りについて、slot_key を持たない行・max_reuse<=1 の時は常に個別生成（安全側）。
    Returns: {"library": [{"line_id","char_id","slot_id"}...],
              "generate": [{"line_id","variant_id"}...], "copy": [{"line_id","copy_from","variant_id"}...],
              "generate_count", "copy_count", "library_count", "max_reuse", "min_gap"}
    """
    max_reuse = max(1, max_reuse)
    min_gap = max(0, min_gap)
    order_by_id = {p["line_id"]: p.get("order", 0) for p in targets}

    library_entries: list[dict] = []
    remaining: list[dict] = []
    for p in targets:
        lib_hit = _library_lookup(p) if use_library else None
        if lib_hit is not None:
            library_entries.append({
                "line_id": p["line_id"], "char_id": lib_hit["char_id"], "slot_id": lib_hit["slot_id"],
            })
        else:
            remaining.append(p)

    groups: dict[str, list[dict]] = {}
    generate_entries: list[dict] = []
    copy_entries: list[dict] = []

    for p in remaining:
        key = p.get("slot_key")
        if not key or max_reuse <= 1:
            generate_entries.append({"line_id": p["line_id"], "variant_id": None})
        else:
            groups.setdefault(key, []).append(p)

    for key, group in groups.items():
        assign = _assign_variants(group, max_reuse, min_gap)
        ordered = sorted(group, key=lambda p: p.get("order", 0))
        reps: dict[int, str] = {}
        for p in ordered:
            v = assign[p["line_id"]]
            variant_id = f"{key}#{v}"
            if v not in reps:
                reps[v] = p["line_id"]
                generate_entries.append({"line_id": p["line_id"], "variant_id": variant_id})
            else:
                copy_entries.append({
                    "line_id": p["line_id"], "copy_from": reps[v], "variant_id": variant_id,
                })

    generate_entries.sort(key=lambda e: order_by_id.get(e["line_id"], 0))
    return {
        "library": library_entries, "generate": generate_entries, "copy": copy_entries,
        "generate_count": len(generate_entries), "copy_count": len(copy_entries),
        "library_count": len(library_entries),
        "max_reuse": max_reuse, "min_gap": min_gap,
    }


def generation_plan_estimate(
    project_id: str, episode: int,
    line_ids: list[str] | None = None, only_missing: bool = True,
    max_reuse: int = 1, min_gap: int = 8, use_library: bool = True,
) -> dict:
    """課金前のドライラン。画像には一切触れない・何も保存しない。"""
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found (run /aroll/prompts first)")
    targets = select_targets(manifest, line_ids, only_missing)
    plan = build_generation_plan(targets, max_reuse=max_reuse, min_gap=min_gap, use_library=use_library)
    unkeyed = sum(1 for e in plan["generate"] if e["variant_id"] is None)
    return {
        "target_count": len(targets),
        "library_count": plan["library_count"],
        "generate_count": plan["generate_count"],
        "copy_count": plan["copy_count"],
        "unkeyed_count": unkeyed,
        "estimated_cost_usd": round(plan["generate_count"] * AROLL_COST_PER_IMAGE_USD, 2),
        "cost_per_image_usd": AROLL_COST_PER_IMAGE_USD,
        "max_reuse": plan["max_reuse"], "min_gap": plan["min_gap"],
    }


def _apply_copy(project_id: str, episode: int, manifest: dict, entry: dict, log: list[str] | None = None) -> bool:
    """代表行の画像をコピーして対象行のマニフェストへ反映する（マニフェストの保存は呼び出し側）。"""
    panels_by_id = {p.get("line_id"): p for p in manifest.get("panels", [])}
    src = panels_by_id.get(entry["copy_from"])
    dst = panels_by_id.get(entry["line_id"])
    if src is None or dst is None or not src.get("image"):
        if log is not None:
            log.append(f"✘ {entry['line_id']} コピー元 {entry.get('copy_from')} の画像が無いためスキップ")
        return False
    out_dir = aroll_dir(project_id, episode)
    src_path = out_dir / src["image"]
    if not src_path.exists():
        if log is not None:
            log.append(f"✘ {entry['line_id']} コピー元ファイルが見つかりません: {src['image']}")
        return False
    filename = panel_filename(entry["line_id"])
    old_image = dst.get("image")
    (out_dir / filename).write_bytes(src_path.read_bytes())
    if old_image and old_image != filename:
        (out_dir / old_image).unlink(missing_ok=True)
    dst.update({
        "status": "done", "image": filename, "provider": src.get("provider"),
        "error": None, "generated_at": _now(),
        # コピーでも「今の台本テキスト」を刻む＝sync判定は通常の行と同じロジックで正しく動く
        "source_text": dst.get("text", ""), "source_text_hash": text_hash(dst.get("text")),
        "variant_id": entry.get("variant_id"), "image_source": "copied",
        "copied_from": entry["copy_from"],
    })
    if log is not None:
        log.append(f"⧉ {entry['line_id']} ← {entry['copy_from']} をコピー")
    return True


def _stamp_variant(project_id: str, episode: int, line_id: str, variant_id: str) -> None:
    """実生成した代表行にvariant_idを刻む（generate_line_imageは汎用のため単独では書かない）。"""
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        return
    for p in manifest.get("panels", []):
        if p.get("line_id") == line_id:
            p["variant_id"] = variant_id
            save_manifest(project_id, episode, manifest)
            return


async def run_batch(
    project_id: str, episode: int,
    line_ids: list[str] | None = None,
    only_missing: bool = True,
    allow_paid_fallback: bool = False,
    max_reuse: int = 1,
    min_gap: int = 8,
    use_library: bool = True,
) -> None:
    """バッチ本体（asyncio.create_task で起動される）。直列＋インターバル＋失敗続行。

    max_reuse=1（既定）なら生成プランは全行が個別生成＝Phase2導入前と完全に同一挙動。
    max_reuse>1 の時だけ、同じ演技スロットの代表行を生成→即座に残りへコピーする。
    use_library=True（既定・Phase 3）: キャラ所有ライブラリの一致を最優先で消費する（$0・
    インターバルなし）。job["total"] は実生成枚数のみ（コピー/ライブラリ引用は一瞬で終わる
    ため母数に入れると進捗が嘘になる）。
    """
    key = _job_key(project_id, episode)
    manifest = load_manifest(project_id, episode) or {}
    targets = select_targets(manifest, line_ids, only_missing)
    plan = build_generation_plan(targets, max_reuse=max_reuse, min_gap=min_gap, use_library=use_library)
    generate_entries = plan["generate"]
    copy_by_source: dict[str, list[dict]] = {}
    for entry in plan["copy"]:
        copy_by_source.setdefault(entry["copy_from"], []).append(entry)

    job = _JOBS[key] = {
        "running": True, "cancel": False,
        "total": len(generate_entries), "done": 0, "failed": 0,
        "copy_total": len(plan["copy"]), "copy_done": 0,
        "library_total": plan["library_count"], "library_done": 0,
        "current_line": None, "log": [],
        "started_at": _now(), "finished_at": None,
        "allow_paid_fallback": allow_paid_fallback,
        "max_reuse": max_reuse,
    }
    log: list[str] = job["log"]

    for entry in plan["library"]:
        if job["cancel"]:
            break
        lid = entry["line_id"]
        job["current_line"] = lid
        try:
            await generate_line_image(project_id, episode, lid, log=log, use_library=True)
            job["library_done"] += 1
            log.append(f"📚 {lid} ライブラリ引用完了 ({job['library_done']}/{job['library_total']})")
        except Exception as e:
            job["failed"] += 1
            log.append(f"✘ {lid} ライブラリ引用失敗: {str(e)[:150]}")

    try:
        for i, entry in enumerate(generate_entries):
            if job["cancel"]:
                log.append(f"中断しました（{job['done']}枚生成済み）")
                break
            lid = entry["line_id"]
            job["current_line"] = lid
            try:
                await generate_line_image(
                    project_id, episode, lid,
                    allow_paid_fallback=allow_paid_fallback, log=log,
                    use_library=use_library,
                )
                if entry.get("variant_id"):
                    _stamp_variant(project_id, episode, lid, entry["variant_id"])
                job["done"] += 1
                log.append(f"✔ {lid} 生成完了 ({job['done']}/{job['total']})")
                # このコマを代表(コピー元)とする行があれば即座にコピーする（中断しても
                # ここまでのコピーは残る＝レジューム安全の原則を崩さない）
                dependents = copy_by_source.get(lid, [])
                if dependents:
                    m = load_manifest(project_id, episode)
                    if m is not None:
                        for c in dependents:
                            if _apply_copy(project_id, episode, m, c, log=log):
                                job["copy_done"] += 1
                        save_manifest(project_id, episode, m)
            except Exception as e:
                job["failed"] += 1
                log.append(f"✘ {lid} 失敗: {str(e)[:150]}")
                for c in copy_by_source.get(lid, []):
                    log.append(f"  └ {c['line_id']} はコピー元({lid})失敗のためスキップ")
            if i < len(generate_entries) - 1 and not job["cancel"]:
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
