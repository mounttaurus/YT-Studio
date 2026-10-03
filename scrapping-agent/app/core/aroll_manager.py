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
    aroll_duplicates, aroll_prompt_generator, background_manager, camera_plan, character_manager,
    cut_planner, cutout_selector, nanobanana_client, panel_library_manager, panel_presets,
    project_manager, shot_meter, slot_rules, stock_health, style_manager,
)

SCHEMA_VERSION = "1.3.0"  # 1.3.0: panels[].parent_line_id を追加（サブ行・SUBLINE_PLAN §4-2）
# 行単位の背景自動割当で「直近使った背景を避ける」窓の大きさ（連続する行での反復感を抑える）
AROLL_BG_RECENT_WINDOW = int(os.getenv("AROLL_BG_RECENT_WINDOW", "6"))
MIN_INTERVAL_SEC = float(os.getenv("AROLL_MIN_INTERVAL_SEC", "3"))
RETRY_BACKOFF_SEC = [5, 15, 45]

# 固定サフィックス: 吹き出しはユーザーが後乗せするため画像内の文字を禁止する
PROMPT_SUFFIX = "No text, no letters, no speech bubbles, no watermark in the image."

# 背景はLLMに書かせず常にこれで統一する（時間帯/シチュエーションのブレを防ぐ）。
# ユーザーが後から背景だけ別途生成して合成する運用が前提（2026-07-25方針）。
# 本籍は panel_presets.BACKGROUND_MODES（切り抜きの前提なので3か所で同じ文字列だった）。
BACKGROUND_FRAGMENT = panel_presets.BACKGROUND_MODES["flat"]

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


def _library_lookup(panel: dict, exclude_slot_ids: set[str] | None = None) -> dict | None:
    """パネルの演技スロットにキャラ所有ライブラリ（Phase 3）の一致があれば返す。

    単独キャラのパネルのみ対象（2ショットはライブラリ非対応）。世代違い
    （appearance_version不一致）は panel_library_manager.find_current 側で除外される。

    exclude_slot_ids: 1話分のバッチ処理中に呼び出し側が蓄積する「既に他の行へ割り当てた
    slot_id」。find_current にそのまま中継する（詳細はそちらのdocstring）。
    """
    chars = [c for c in (panel.get("characters") or []) if c]
    if len(chars) != 1:
        return None
    slot = panel.get("slot") or {}
    emotion, shot, angle = slot.get("emotion"), slot.get("shot"), slot.get("angle")
    if not (emotion and shot and angle):
        return None
    entry = panel_library_manager.find_current(chars[0], emotion, shot, angle,
                                                exclude_slot_ids=exclude_slot_ids)
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
        s["id"]: {
            "name": s.get("name", ""),
            "character_id": s.get("character_id") or "",
            # 派生値: 実際に描く時に使うキャラ（声だけキャラなら引き継ぎ先）。
            # ここで一度だけ解決し、全経路が同じ答えを見るようにする。
            # 空文字 = ナレーション（キャラ無し・背景のみ）。
            "image_char_id": resolve_image_char(s.get("character_id") or ""),
        }
        for s in speakers if s.get("id")
    }


def is_imageable(char_id: str) -> bool:
    """そのキャラをコマに映してよいか（＝画像を使う設定か）。

    ⚠️ **見るのは ``uses_images`` だけ。** 参照画像の有無は見ない ── Aロールは
    「参照が無くても生成を止めない」で決着済み（``CHARACTER_CUTOUT_PLAN.md`` §15-3④）。
    ここで参照必須にすると台本の途中で話数が完走しなくなる。

    ⚠️ **既定は True。** 旧データ（``uses_images`` を持たない）は従来どおり描ける扱い。
    """
    if not char_id:
        return False
    c = character_manager.read_character(char_id)
    return bool(c) and c.get("uses_images", True)


def resolve_image_char(char_id: str) -> str:
    """その話者を**実際に描く時に使うキャラ**を返す。描けないなら空文字。

    声を差し替えるために作ったキャラ（`uses_images:false`）は、
    ``image_source_char_id`` で「絵はこのキャラから引き継ぐ」と宣言できる。
    配役は声で選び、絵は引き継ぎ先から取る ── これで**同じ人物の並行在庫**を防ぐ
    （``CHARACTER_CUTOUT_PLAN.md`` §15 の目的）。

    ⚠️ **辿るのは1段だけ。** 連鎖を許すと循環（A→B→A）と「結局誰の絵か
    分からない」を生む。引き継ぎ先が自身も描けないなら、そこで諦めて空文字を返す。

    戻り値が空文字 = ナレーション（キャラ無し・背景のみのコマ）。
    引き継ぎ先を持たない声だけキャラ＝ナレーターは自然にこちらへ落ちる。
    """
    if not char_id:
        return ""
    c = character_manager.read_character(char_id)
    if not c:
        return ""
    if c.get("uses_images", True):
        return char_id
    src = (c.get("image_source_char_id") or "").strip()
    if not src or src == char_id:
        return ""
    # 1段だけ辿る。引き継ぎ先は「描けるキャラ」でなければならない
    return src if is_imageable(src) else ""


def get_cast_characters(project_id: str) -> dict[str, dict]:
    """配役に登場する**描けるキャラ**の {char_id: {name, appearance_prompt}} を返す。

    ⚠️ **``uses_images`` が False のキャラは除外する。** 声だけのキャラ（ナレーター、
    声を差し替えるために作られたキャラセット）を「映すキャラ」の候補としてLLMへ渡すと、
    参照画像0枚のまま生成が走り、同じ人物の並行在庫ができる
    （``psassist/Docs/CHARACTER_CUTOUT_PLAN.md`` §15）。

    ⚠️ **``can_generate_images`` を丸ごと呼ばない。** Aロールは「参照が無くても生成を
    止めない」で決着済み（§15-3④）── ここで参照必須にすると台本の途中で止まる。
    **見るのは ``uses_images`` だけ。**
    """
    chars: dict[str, dict] = {}
    for sp in get_speaker_map(project_id).values():
        cid = sp.get("character_id")
        if not cid or cid in chars:
            continue
        # 声だけのキャラは引き継ぎ先へ解決する（無ければナレーション＝候補に出さない）
        drawn = resolve_image_char(cid)
        if not drawn or drawn in chars:
            continue
        c = character_manager.read_character(drawn)
        if c is None:
            continue
        chars[drawn] = {
            "name": c.get("name", drawn),
            "appearance_prompt": c.get("appearance_prompt", ""),
        }
    return chars


def cast_warnings(project_id: str) -> list[str]:
    """配役のうち「描けないキャラ」を人が読める形で並べる。

    除外そのものは ``get_cast_characters`` が黙って行うので、**なぜその役のコマに
    キャラが出ないのか**をここで必ず言う（黙って消えるのが一番たちが悪い）。
    """
    out: list[str] = []
    for sid, sp in sorted(get_speaker_map(project_id).items()):
        cid = sp.get("character_id")
        if not cid:
            out.append(f"話者 {sid} にキャラが割り当てられていません（TTS配役を確認）")
            continue
        c = character_manager.read_character(cid)
        if c is None:
            out.append(f"話者 {sid} のキャラ {cid} が見つかりません")
            continue
        if c.get("uses_images", True):
            continue  # 正常な役は黙っている
        name = c.get("name", cid)
        drawn = resolve_image_char(cid)
        if drawn:
            # 引き継ぎは意図された設定なので「警告」ではなく事実の報告にする。
            # ここを警告口調にすると、正しく設定した人が毎回不安になる。
            src = character_manager.read_character(drawn) or {}
            out.append(
                f"話者 {sid}「{name}」は声だけのキャラです。"
                f"絵は「{src.get('name', drawn)}」({drawn}) から引き継ぎます"
            )
        else:
            out.append(
                f"話者 {sid} のキャラ「{name}」({cid}) は『画像を使わない』設定で、"
                "絵の引き継ぎ先も未設定です。この役のコマはナレーション扱い"
                "（キャラ無し・背景のみ）になります"
            )
    return out


def build_or_update_manifest(
    project_id: str, episode: int, script: dict,
    prompts_by_line: dict[str, dict],
    aspect: str = "16:9", style: str = "kamishibai",
    overwrite: bool = False,
) -> dict:
    """script.json の行順にマニフェストを構築/更新する。

    既存パネルは line_id で引き継ぐ:
    - **既存パネルの項目は全部引き継ぎ、ここで再計算する項目だけを上書きする。**
      ⚠️ 引き継ぐ項目を固定リストで列挙しないこと ── 後から足した項目（確定・背景・
      在庫の紐付け）がリストから漏れ、下ごしらえ/台本の再承認のたびに全行から黙って
      消えていた（2026-09-24発見・修正）
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
            # ⚠️ ここは話者へのフォールバック。**必ず is_imageable を通す。**
            # 素通しにすると、声だけのキャラが配役に居る役で
            # get_cast_characters の除外を迂回して characters に入り、
            # 参照0枚のまま課金生成される（2026-08-30 に塞いだ穴）。
            drawn = resolve_image_char(speaker.get("character_id", ""))
            if not characters and drawn:
                characters = [drawn]
            prompt_text_hash = prev.get("prompt_text_hash", "")
            slot = prev.get("slot")
            slot_source = prev.get("slot_source") or "none"

        # 台本に戻ってきた行は orphan ではない
        panel = {k: v for k, v in prev.items() if k != "orphan"}
        panel.update({
            "line_id": lid,
            "order": ln.get("order", i),
            "section": ln.get("section") or "main",
            "speaker_id": ln.get("speaker_id", ""),
            "speaker_name": speaker.get("name") or ln.get("speaker_name", ""),
            "text": ln.get("text", ""),
            # サブ行のグループ名（`Docs/SUBLINE_PLAN.md` I5）。prevからの引き継ぎではなく
            # 常に台本から写す＝台本側でグループが変わったら（分割/結合/削除）追随する
            "parent_line_id": ln.get("parent_line_id"),
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
        panels.append(panel)

    # 台本から消えた行のうち画像を持つものは証拠として残す（バッチ対象からは常に除外）
    # ⚠️ 既に orphan の行は画像が無くても持ち越す（`sync_structure` が孤立扱いにした行。
    #    在庫の割当を `orphaned_cutout` に預けてあり、台本へ戻った時（Undo）に生き返らせる。
    #    ここで落とすと、削除→プロンプト作り直し→Undo で絵の割当が戻らない。LINE_WORKBENCH_PLAN I5）
    live_ids = {p["line_id"] for p in panels}
    for lid, prev in old_panels.items():
        if lid in live_ids or (prev.get("status") != "done" and not prev.get("orphan")):
            continue
        panels.append({**prev, "orphan": True})

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "episode": episode,
        "aspect": (old.get("aspect") if not overwrite else None) or aspect,
        "style": (old.get("style") if not overwrite else None) or style,
        "generated_at": _now(),
        # ⚠️ **カットの手直しは必ず引き継ぐ**（穴9 §11-2「手動の上書きは再計算で壊さない」）。
        # ここは毎回マニフェストを作り直すので、書き忘れるとプロンプト再生成のたびに
        # ユーザーのカット編集が黙って消える。カット自体は保存しない（毎回計算する）。
        "cut_overrides": old.get("cut_overrides") or {},
        "panels": panels,
    }
    save_manifest(project_id, episode, manifest)
    return manifest


# UIの行ごと選択で許す吹き出しの形（横書きのみ＝縦の rect_v/round_v は出さない・
# Docs/BUBBLE_CHOICE_PLAN.md Q1）。psassist の `spec.BUBBLE_CHOICE_KEYS` と同じ集合
#（コンテナが別なので両方に持つ）。
BUBBLE_CHOICE_KEYS = (
    "rect_a", "rect_b", "round_a", "cloud_a", "cloud_b", "spike_a", "spike_b",
)


def update_line(
    project_id: str, episode: int, line_id: str,
    prompt: str | None = None, characters: list[str] | None = None,
    slot: dict | None = None, background_id: str | None = None,
    bubble_key: str | None = None,
) -> dict | None:
    """ユーザーによる行編集。promptを書き換えたら prompt_source="user" にする。

    slotを直接渡すとslot_source="user"になり、promptからの自動再計算より優先される
    （UIの「根拠」欄でemotion/shot/angleを選び直す操作。画像生成LLMの散文とは独立に
    照合キーだけを差し替えられる。Docs/AROLL_ASSET_PLAN.md §18）。

    background_idは空文字を渡すと明示的にnull（未割当）へ戻せる（Noneは「変更しない」の意味）。

    bubble_keyは吹き出しの形の上書き（`BUBBLE_CHOICE_KEYS` のどれか）。空文字で自動
    （話者の既定＋！/？）へ戻す。Noneは「変更しない」。未知のキーは ValueError。
    組版プランは合成のたびに aroll.json から作り直されるので、上書きはここに持つ。
    """
    if bubble_key and bubble_key not in BUBBLE_CHOICE_KEYS:
        raise ValueError(f"未知の bubble_key: {bubble_key}（許可: {', '.join(BUBBLE_CHOICE_KEYS)}）")
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        return None
    for p in manifest["panels"]:
        if p.get("line_id") == line_id:
            if background_id is not None:
                p["background_id"] = background_id or None
            if bubble_key is not None:
                if bubble_key:
                    p["bubble_key"] = bubble_key
                else:
                    p.pop("bubble_key", None)
            if prompt is not None:
                p["prompt"] = prompt.strip()
                p["prompt_source"] = "user"
                # 手書きプロンプトは「今の台本テキスト」を見て書かれたものとみなす
                p["prompt_text_hash"] = text_hash(p.get("text"))
                # slotを明示指定していない時だけ、古いslotが別の演技のdedup対象に誤って
                # 混ざらないよう正規表現で再計算する（LLM呼び出し不要・課金なし）
                if slot is None:
                    derived = aroll_prompt_generator.derive_slot_from_prompt(p["prompt"])
                    if not (derived["emotion"] and derived["shot"] and derived["angle"]):
                        derived = None
                    p["slot"] = derived
                    p["slot_source"] = "derived" if derived else "none"
            if slot is not None:
                p["slot"] = slot
                p["slot_source"] = "user"
            if characters is not None:
                p["characters"] = [c for c in characters if c][:2]
            p["slot_key"] = compute_slot_key(p.get("characters"), p.get("slot"))
            save_manifest(project_id, episode, manifest)
            return p
    return None


def _background_units(panels: list[dict], cuts: list[dict]) -> list[list[dict]]:
    """背景割当の単位を並び順に作る（グループがあればグループ、無ければカット。§8-1）。

    ⚠️ **カットと背景の単位はもう別物。** カットは「絵を共有する区間」、グループ
    （``parent_line_id``）は「背景を共有する区間」で、サブ行のグループは寄り引きが
    変わるためカットが割れて当然（§8-2）だが背景は揃えたい。グループがカットを
    跨ぐ（一部のサブ行だけ短くて束ねられる）場合もあるので、グループ優先で単位を作る。
    """
    cut_of = cut_planner.cut_of_line(cuts)
    units: list[list[dict]] = []
    seen: set[str] = set()
    for p in panels:
        lid = p.get("line_id")
        if lid in seen:
            continue
        parent_id = p.get("parent_line_id")
        if parent_id:
            wanted_ids = {m.get("line_id") for m in panels if m.get("parent_line_id") == parent_id}
        else:
            cut = cut_of.get(lid)
            wanted_ids = set(cut["line_ids"]) if cut else {lid}
        members = [m for m in panels if m.get("line_id") in wanted_ids]
        seen.update(wanted_ids)
        units.append(members)
    return units


def _unit_reference_shot(members: list[dict]) -> tuple[str, bool]:
    """グループの中で一番引きの絵のshotを返す（背景はその画角に合わせる・§8-2）。

    2つ目の戻り値: メンバーのどれか1人でも実物のshotが希望(slot.shot)とズレていたか
    （from_actual_shot の集計用）。
    """
    best_idx, best_shot, from_actual = -1, "", False
    for m in members:
        used = _used_slot_tags(m)  # 実物のタグ（cutout_slot_id未確定ならNone）
        actual_shot = used.get("shot") if used else None
        wished_shot = (m.get("slot") or {}).get("shot") or ""
        shot = actual_shot or wished_shot
        if actual_shot and actual_shot != wished_shot:
            from_actual = True
        idx = shot_meter.scale_index(shot)
        if idx is None:
            idx = -1
        if idx >= best_idx:
            best_idx, best_shot = idx, shot
    return best_shot, from_actual


def auto_assign_backgrounds(project_id: str, episode: int, only_missing: bool = True,
                            line_ids: list[str] | None = None) -> dict:
    """全行に背景を自動割当する（無料・画像は一切生成しない。既存backgroundsアーカイブから選ぶだけ）。

    行の(shot→framing, emotion→mood)から background_manager.suggest_background() で1件選び、
    panel["background_id"] に書き込む。「決定回数を減らす」のではなく「初期割当の精度を上げ、
    外れだけ人が差し替える」方針（Docs/AROLL_ASSET_PLAN.md §19。
    [[aroll-background-per-line-manga-convention]]）。

    ⚠️ **単位はグループ（無ければカット）** （`Docs/SUBLINE_PLAN.md` §8-1・2026-09-26）。
    同じ親を持つサブ行は同じ背景を使う（組版側がそこへ寄り引きに応じて拡大する・§8-2）。
    グループが無い行は従来どおりカット単位（穴9 §9-6・カットは絵の共有単位）。

    ⚠️ **shotの出どころは「実際に使われている絵」を優先する**（穴6・2026-09-14）。
    在庫選定は指紋距離で選ぶためLLMの希望slot.shotとは実測69%（41/59）ズレる
    （[[background-shot-decided-before-cutout]]）。`cutout_slot_id` が既に決まっている行は
    その実物のshotで背景を選び、まだ決まっていない行（承認直後の初回一括割当など）は
    従来どおりslot.shotへフォールバックする。グループでは**一番引きのメンバー**のshotを
    基準にする（組版側の倍率計算がその引きを基準に寄せるため・§8-2）。emotionは在庫選定の
    適格条件そのものなので実物とほぼ一致し続ける（差し替えない・先頭行の値を使う）。

    only_missing=True（既定）: 既にbackground_idを持つ行はスキップ（手動で選んだ行を壊さない）。
    False: 全行を割当し直す（既存の手動選択も上書きする）。

    line_ids: 指定した行だけを対象にする（コマ一覧の一括操作用）。
    ⚠️ **空リストは「対象ゼロ」**（省略＝None が「全行」）。falsy 判定にすると、
    1行も選んでいないのに全行の背景を割り当て直すことになる
    （``CHARACTER_CUTOUT_PLAN.md`` §13-4 と同じ規則）。

    直近 AROLL_BG_RECENT_WINDOW 行で使った背景は避ける（順序どおりに1単位ずつ処理するため、
    同じ背景が連続して出るのを防げる）。times_usedによる最小消費優先ローテーションと合わせて
    「反復感を機械側が担保する」設計（キャラ画像ライブラリのfind_currentと同じ考え方）。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found (run /aroll/prompts first)")

    wanted = None if line_ids is None else set(line_ids)

    panels = [p for p in manifest.get("panels", []) if not p.get("orphan")]
    cuts = cut_report(project_id, episode, manifest)["cuts"]
    units = _background_units(panels, cuts)

    recent: list[str] = []
    assigned = unmatched = skipped = from_actual = 0
    for members in units:
        if not members:
            continue
        head = members[0]
        # 対象外の単位も recent には積む（連続を避ける判定は並び順で効くため）
        if wanted is not None and not any(m.get("line_id") in wanted for m in members):
            if head.get("background_id"):
                recent.append(head["background_id"])
                recent[:] = recent[-AROLL_BG_RECENT_WINDOW:]
            continue
        if only_missing and all(m.get("background_id") for m in members):
            recent.append(head["background_id"])
            recent[:] = recent[-AROLL_BG_RECENT_WINDOW:]
            skipped += len(members)
            continue
        shot, unit_from_actual = _unit_reference_shot(members)
        emotion = (head.get("slot") or {}).get("emotion") or ""
        bg = background_manager.suggest_background(shot, emotion, exclude_ids=set(recent))
        if bg is None:
            unmatched += len(members)
            continue
        for m in members:
            m["background_id"] = bg["bg_id"]
            assigned += 1
        background_manager.record_usage(bg["bg_id"])   # 消費は単位に1回
        recent.append(bg["bg_id"])
        recent[:] = recent[-AROLL_BG_RECENT_WINDOW:]
        if unit_from_actual:
            from_actual += 1

    save_manifest(project_id, episode, manifest)
    return {
        "assigned": assigned, "unmatched": unmatched, "skipped": skipped,
        "from_actual_shot": from_actual,  # 実物のshotで選び直せた件数（希望とズレていた分）
        "total": len([p for p in manifest["panels"] if not p.get("orphan")]),
    }


def set_library_image(
    project_id: str, episode: int, line_id: str, char_id: str, slot_id: str,
) -> dict:
    """ユーザーがライブラリの特定バリアントを直接選んだ時に使う（ローテーション無視・明示指定）。

    generate_line_imageのライブラリ消費パスと同じ書き込み手順を、find_currentの自動選択ではなく
    ユーザー指定のslot_idで行う。record_usageも呼ぶため、手動選択も使用回数の均等化に参加する。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found (run /aroll/prompts first)")
    panel = next((p for p in manifest["panels"] if p.get("line_id") == line_id), None)
    if panel is None:
        raise ValueError(f"line not found in aroll.json: {line_id}")
    entry = panel_library_manager.get_entry(char_id, slot_id)
    if entry is None:
        raise ValueError(f"panel library entry not found: {char_id}/{slot_id}")

    out_dir = aroll_dir(project_id, episode)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = panel_library_manager.library_dir(char_id) / entry["image"]
    filename = panel_filename(line_id)
    old_image = panel.get("image")
    (out_dir / filename).write_bytes(src.read_bytes())
    if old_image and old_image != filename:
        (out_dir / old_image).unlink(missing_ok=True)
    panel.update({
        "status": "done", "image": filename, "provider": entry.get("provider", "nanobanana"),
        "error": None, "generated_at": _now(),
        "source_text": panel.get("text", ""),
        "source_text_hash": text_hash(panel.get("text")),
        "image_source": "library",
        "library_slot_id": entry.get("slot_id"),
    })
    clear_image_approval(project_id, episode, panel, demote=False)
    save_manifest(project_id, episode, manifest)
    panel_library_manager.record_usage(char_id, slot_id)
    return panel


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


def picture_key(panel: dict) -> str:
    """パネルの「今の絵」の同一性。話者変更の印（`speaker_changed`）が、印を付けた時の絵に
    対してだけ効くようにするための鍵。選び直す・作り直す・在庫を替えると変わる。"""
    return f"{panel.get('cutout_slot_id')}|{panel.get('image')}|{panel.get('generated_at')}"


def _panel_sync(panel: dict, line: dict | None, out_dir: Path | None) -> str:
    """1パネルの同期状態を判定する（台本行 line が正・panel.orphan は参考にしない）。

    絵の実体は2系統ある: 生成/ライブラリ画像（``status=="done"`` かつ ``image``）と、
    在庫の切り抜き割当のみ（``cutout_slot_id``）。組版（Photoshop合成）は aroll.json の
    外で起きて書き戻されないため、後者も ``_panel_decided`` と同じ基準で確定扱いする
    （さもないと組版まで済んだ「在庫だけの行」が永久に missing になる。S0 §14）。
    """
    if line is None:
        return SYNC_ORPHAN
    img = panel.get("image")
    if panel.get("status") == "done" and img:
        if out_dir is not None and not (out_dir / img).exists():
            return SYNC_MISSING  # マニフェストはdoneだが実ファイルが無い（手動削除など）
    elif not panel.get("cutout_slot_id"):
        return SYNC_MISSING  # 生成画像も在庫割当も無い
    # 話者を付け替えた行（行操作の窓口の `speaker`）。絵は前の話者のキャラのまま＝古い。
    # 印を付けた時の絵と今の絵が同じ間だけ効く＝選び直せば（絵が変われば）自然に外れる
    marker = panel.get("speaker_changed")
    if marker and marker.get("picture") == picture_key(panel):
        return SYNC_STALE
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
        if line is None and p.get("orphan") and p.get("status") != "done":
            # 行操作の窓口（`sync_structure`）が孤立扱いにした、絵が無いコマ。残っているのは
            # 「Undoで戻すための預かり物」だけで、直すものも警告する材料も無い（生成画像の
            # PNGが残っている孤立だけが「行が消えた」として数える＝従来どおり）
            continue
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


def load_tts(project_id: str, episode: int) -> dict | None:
    """``tts.json`` を読む（カットの尺に使う。無ければ None＝文字数から推定する）。"""
    ep_dir = project_manager.episode_dir(project_id, episode)
    if ep_dir is None:
        return None
    f = ep_dir / "tts.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def cut_report(project_id: str, episode: int, manifest: dict | None = None) -> dict:
    """この話数のカット割りを返す（**検査のみ・何も保存しない**。穴9 §11-2）。

    ⚠️ **カットは保存しない＝毎回計算する。** 台本が変われば境界も変わるので、
    保存すると `line_id` の増減で簡単に腐る（`cut_id` は順番に振り直される連番であって
    恒久IDではない）。保存するのは**ユーザーの手直しだけ**（``manifest["cut_overrides"]``）。
    """
    manifest = manifest or load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found (run /aroll/prompts first)")
    panels = [p for p in manifest.get("panels", []) if not p.get("orphan")]
    tts = load_tts(project_id, episode)
    durations = cut_planner.durations_from_tts(tts)
    cuts = cut_planner.plan_cuts(
        panels, durations, overrides=manifest.get("cut_overrides") or {})
    return {
        "total_lines": len(panels),
        "total_cuts": len(cuts),
        # 尺の出どころを明示する（推定のまま本番に流れていないかを見るため）
        "duration_source": "tts" if durations else "estimated",
        "short_line_sec": cut_planner.SHORT_LINE_SEC,
        "cuts": cuts,
    }


KEEP = object()   # 「このフィールドは触らない」を表す番兵（None＝「消す」と区別する）


def set_cut_override(project_id: str, episode: int, line_id: str,
                     boundary=KEEP, reset: bool = False) -> dict:
    """カットの境界の手直しを保存する（分ける/前へつなげる）。

    ⚠️ **role（決め台詞の付け替え）は廃止**（`Docs/SUBLINE_PLAN.md` §6-3・2026-09-26）。
    決め台詞の規則自体を廃止したため、付け替える対象が無い。

    boundary: ``"start"``（この行から新しいカット）/ ``"join"``（前のカットへつなげる）/
      ``None``（この項目だけ自動に戻す）/ 省略（触らない）。
    reset: True なら**この行の手直しを全部消す**。

    ⚠️ **手直しは再計算で壊れない**のが要件（§11-2）。カット自体は保存せず、
    ここで保存した上書きだけを毎回の計算に当てる。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")
    if not any(p.get("line_id") == line_id for p in manifest.get("panels", [])):
        raise ValueError(f"line not found: {line_id}")
    if boundary is not KEEP and boundary not in (None, "start", "join"):
        raise ValueError("boundary は start / join / null のいずれか")

    ov = dict(manifest.get("cut_overrides") or {})
    entry = {} if reset else dict(ov.get(line_id) or {})
    if boundary is not KEEP:
        if boundary is None:
            entry.pop("boundary", None)
        else:
            entry["boundary"] = boundary
    if entry:
        ov[line_id] = entry
    else:
        ov.pop(line_id, None)     # 空になったら消す（自動に戻す）
    manifest["cut_overrides"] = ov
    save_manifest(project_id, episode, manifest)
    return {"line_id": line_id, "override": entry or None,
            "cuts": cut_report(project_id, episode, manifest)["total_cuts"]}


def _used_slot_tags(panel: dict, cache: dict | None = None) -> dict | None:
    """コマ一覧が表示する「実際に使われている絵」のタグ（emotion/shot/angle/pose）。

    ⚠️ **`panel["slot"]` とは別物**。`slot` はLLMが決めた**希望**のラベルで、
    在庫選定は指紋距離で「一番使われていない近い絵」を選ぶため、実物とズレることが多い
    （本番64行中56行=87.5%でズレを実測・2026-09-06）。一覧はここではなく
    **在庫エントリ側の実タグ**を出す。行の「希望」を変える入力はモーダル側の
    プルダウン（`panel["slot"]`）に残す ── 表示元と入力先を分ける設計。
    """
    char_id, slot_id = panel.get("cutout_char_id"), panel.get("cutout_slot_id")
    if not char_id or not slot_id:
        return None
    if cache is None:
        e = panel_library_manager.get_entry(char_id, slot_id)
    else:
        # ⚠️ get_entry は呼ぶたびに library.json（本番のルカで約4MB）を丸ごと読む。141コマ×毎回だと
        # 1話の GET /aroll が11秒かかった（2026-09-29 実測）。1回のリクエストではキャラごとに1回だけ読む。
        if char_id not in cache:
            cache[char_id] = {x.get("slot_id"): x for x in panel_library_manager.load_index(char_id).get("entries", [])}
        e = cache[char_id].get(slot_id)
    if not e:
        return None
    return {"emotion": e.get("emotion"), "shot": e.get("shot"),
            "angle": e.get("angle"), "pose": e.get("pose")}


def annotate_manifest(project_id: str, episode: int, manifest: dict) -> dict:
    """マニフェストのコピーに sync 等を付けて返す（レスポンス専用・ファイルには書かない）。

    ⚠️ **表示するセリフは常に確定台本の現在の文面にする**（2026-09-24）。``panel["text"]``は
    下ごしらえ（``build_or_update_manifest``）時点の写しで、台本を編集しただけでは更新されない。
    これを直さないと、「絵が古い」を確定/このままでよいで解消した後もコマ一覧には古いセリフが
    表示され続け、ユーザーが古いセリフを見ながら絵を判断することになる（吹き出しの文字が
    古いまま納品される穴の一部・Docs/AROLL_UNIFIED_FLOW_PLAN.md §19）。
    orphan行（台本から消えた）は lines_by_id に無いので写しのまま残す
    （「消えた行のPNGが残っている」という事実を見せる用途なので、これは正しい）。
    """
    lines_by_id = _script_lines_by_id(project_id, episode)
    out_dir = aroll_dir(project_id, episode)
    out = dict(manifest)
    tag_cache: dict = {}
    out["panels"] = [
        {**p,
         "text": (lines_by_id.get(p.get("line_id")) or {}).get("text", p.get("text", "")),
         "sync": _panel_sync(p, lines_by_id.get(p.get("line_id")), out_dir),
         "used_slot": _used_slot_tags(p, tag_cache)}
        for p in manifest.get("panels", [])
    ]
    return out


def _image_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else ""


def clear_image_approval(project_id: str, episode: int, panel: dict,
                         *, demote: bool) -> list[str]:
    """絵が変わったら承認を外す（台本/TTSの「上流が動けば下流は未承認」と同じ）。

    demote=True は**作り直した時だけ**。作り直す動機の大半は「絵が気に入らない」なので、
    その絵から取り込んだ在庫を ``pending`` へ落として自動配布を止める。
    在庫からの差し替えや台本テキストの変更では落とさない ── 前の絵は資産としては無傷。
    """
    panel.pop("image_approved_at", None)
    panel.pop("image_approved_hash", None)
    if not demote:
        return []
    return panel_library_manager.demote_from_line(
        panel.get("characters") or [], project_id, episode, panel.get("line_id") or "")


def _stock_picture_bytes(panel: dict) -> bytes | None:
    """コマが指す在庫の切り抜きの中身（実在すれば）。自前の画像ファイルが無い行の「絵の実体」。"""
    sid = panel.get("cutout_slot_id")
    cid = panel.get("cutout_char_id") or next((c for c in (panel.get("characters") or []) if c), None)
    if not sid or not cid:
        return None
    f = panel_library_manager.library_dir(cid) / "cutouts" / f"{sid}.png"
    return f.read_bytes() if f.is_file() else None


def approve_images(project_id: str, episode: int, line_ids: list[str] | None = None,
                   *, register: bool = True) -> dict:
    """行の絵を**確定**する（``image_approved_at`` を立てる）。「この行の絵はこれでいい」の答え。

    T2（Docs/AROLL_UNIFIED_FLOW_PLAN.md §3）: 「承認」は2つの別の判断に割れている。
    ここは**行の確定**だけを担う。「この絵を他の行にも使い回してよい」という
    **資産側の許可**（``review_status: pending → approved``）は別物で、
    📚キャラ在庫タブの ``approve_entry`` / ``approve_all`` が担う（ここでは触らない）。

    ⚠️ **在庫登録は原則ここではもう行わない。** T1で生成の瞬間に
    ``register_from_image(review_status="pending")`` 済み（``cutout_slot_id`` を持つ）なので、
    確定はその絵の身元には触れず ``image_approved_at`` を立てるだけでよい。

    **過去データ互換**: T1より前に生成され ``cutout_slot_id`` を持たない絵は、
    ここが唯一の登録の入口なので**従来どおり**登録する
    （``register_from_image`` に ``review_status`` を渡さず既定の "approved" で入れる＝
    人が今その絵を見て確定したので、切り抜きタブで二度承認させない）。

    ⚠️ **承認と在庫化は別物**。積めない行は ``skipped`` に理由を載せて承認だけ通す
    （絵はそのまま使える・課金ゼロ）。積まない条件は
    「キャラが1人に確定していない」「slotが揃っていない」「切り抜きに失敗した」に加えて
    **``can_generate_images`` が False**（参照画像が無い等）── ここが同じキャラの
    並行在庫ができる唯一の入口なので硬く拒否する（ユーザー判断 2026-08-29）。

    **台本との同期（sync）もここで一緒に解消する**（2026-09-24統合）。生成/在庫差し替えの
    全経路は絵を書き換えるたびに ``source_text_hash`` を今の台本テキストで焼き直しており
    （「絵が変われば同期記録も更新する」という不変条件）、承認だけがそこから外れていた。
    承認は「この絵を今の台本に対する最終稿として使う」という人の意思表示そのものなので、
    対象行が stale/unknown（≒旧 ``POST .../aroll/sync/accept`` の対象）なら
    ``source_text``/``source_text_hash``/``prompt_text_hash`` も今のテキストで更新する。
    line が見つからない（orphan＝台本から消えた行）場合は現在のテキストが無いので触らない。

    ⚠️ **行を明示しない（全行）承認では stale を解消しない**（unknown＝記録が無いだけの旧資産は直す）。
    「人が絵を見て押した」という根拠は行を指定した時にしか成り立たない。全行承認で stale まで
    消すと、セリフが変わったのに誰も絵を見ていない行が黙って「一致」になる
    （旧 sync/accept の「staleを黙って飲まない」を引き継ぐ。MCP の既定は全行なので要注意）。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")
    out_dir = aroll_dir(project_id, episode)
    lines_by_id = _script_lines_by_id(project_id, episode)
    # ⚠️ 空リストは「1行も選んでいない」。falsy判定にすると全行が対象になってしまう
    #（省略＝None が「全行」で、[] とは別物）
    wanted = None if line_ids is None else set(line_ids)
    approved, registered, skipped, synced = [], [], [], []

    for p in manifest.get("panels", []):
        lid = p.get("line_id")
        if wanted is not None and lid not in wanted:
            continue
        img = p.get("image")
        if p.get("status") == "done" and img and (out_dir / img).exists():
            data = (out_dir / img).read_bytes()
        else:
            # 在庫の絵を指しているだけの行（自前の画像ファイルは無い・status は pending のまま）。
            # 絵は在庫の切り抜きで実在するので、人が見て「これでいい」と確定できる（本番の MK 回は 153 コマ中 146 がこの形だった）
            data = _stock_picture_bytes(p)
            if data is None:
                skipped.append({"line_id": lid, "reason": "画像が無い"})
                continue
        p["image_approved_at"] = _now()
        p["image_approved_hash"] = hashlib.sha256(data).hexdigest()[:16]
        line = lines_by_id.get(lid)
        if wanted is not None and p.pop("speaker_changed", None) and lid not in synced:
            synced.append(lid)   # 話者を替えた行の絵を、人が見て「これでいい」と確定した
        if line is not None:
            h = text_hash(line.get("text"))
            prev_hash = p.get("source_text_hash")
            if prev_hash != h and (wanted is not None or not prev_hash):
                p["source_text"] = line.get("text", "")
                p["source_text_hash"] = h
                if (p.get("prompt") or "").strip():
                    p["prompt_text_hash"] = h
                if lid not in synced:
                    synced.append(lid)
        approved.append(lid)
        if not register or p.get("cutout_slot_id"):
            # T1で既に登録済み（pending）。確定は image_approved_at を立てるだけで、
            # 資産側の許可（pending→approved）はキャラ在庫タブの操作に任せる。
            continue
        chars = [c for c in (p.get("characters") or []) if c]
        slot = p.get("slot") or {}
        if len(chars) != 1:
            skipped.append({"line_id": lid, "reason": "キャラが1人に確定していない（2人写り等）"})
            continue
        if not (slot.get("emotion") and slot.get("shot") and slot.get("angle")):
            skipped.append({"line_id": lid, "reason": "slot が揃っていない（分類できていない行）"})
            continue
        # ⚠️ **二重在庫の入口はここ**。参照画像が無いキャラの絵を在庫に積むと、
        # 「ルカっぽい別人」が同じキャラの在庫として次の話数へ配られる
        #（外見だけ複製したVoiceバリアントキャラで実際に起きうる。§15-0）。
        # 生成は止めない代わりにここを塞ぐ、が2026-08-29のユーザー判断。
        # 承認自体は済ませる（絵はそのまま使える）＝積まないだけ。
        can_stock, why = character_manager.can_generate_images(chars[0])
        if not can_stock:
            skipped.append({"line_id": lid, "reason": f"在庫に積めません: {why}"})
            continue
        try:
            r = panel_library_manager.register_from_image(
                chars[0], data,
                emotion=slot["emotion"], shot=slot["shot"], angle=slot["angle"],
                pose=slot.get("pose") or "", prompt=p.get("prompt") or "",
                style_name=manifest.get("style", "kamishibai"),
                model=p.get("model") or "", provider=p.get("provider") or "nanobanana",
                source={"project_id": project_id, "episode": episode, "line_id": lid},
                emotion_tag=slot.get("emotion_tag") or "",
            )
        except Exception as e:  # noqa: BLE001 — 1行の失敗で承認全体を落とさない
            skipped.append({"line_id": lid, "reason": f"取り込み失敗: {type(e).__name__}: {e}"})
            continue
        (registered if r.get("registered") else skipped).append(
            {"line_id": lid, "char_id": chars[0], **r} if r.get("registered")
            else {"line_id": lid, "reason": r.get("reason")})

    save_manifest(project_id, episode, manifest)
    return {"approved": len(approved), "registered": len(registered),
            "line_ids": approved, "entries": registered, "skipped": skipped,
            "synced": len(synced)}


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
            text_rows.append(f"{order}\t★台本とズレ(未書き出し・line_id={lid})\t{speaker}\t{l['text']}")
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
        f"台本とズレ（台本が変わったが未再生成・書き出していません）: {len(stale)}行",
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


def _resolve_refs(characters: list[str], log: list[str] | None = None) -> list[tuple[bytes, str, str]]:
    """キャラごとの参照画像を解決する（1人=最大3枚、2人=各1枚、合計3枚以内）。

    ラベルにはキャラ名を入れてNanoBananaに役割を伝える。参照が無いキャラはスキップ
    （appearance_promptのみで生成）。

    ⚠️ **参照が無くても生成は止めない**（その行に絵は必要で、台本の途中で止まると
    その話数のAロールが完走しない）。代わりに log へ警告を出す。参照が無いと
    ``nanobanana_client._with_ref_instruction()`` が一貫性の指示ごと落とすので、
    生成のたびに別人が出る ── 気づかずに進めるのが一番まずい。
    二重在庫の入口は「承認による在庫への取り込み」の側なので、そちらは
    ``approve_images`` が硬く拒否する（ユーザー判断 2026-08-29）。

    ★2026-09-25変更（Docs/CHARACTER_CONSISTENCY_PLAN.md §4 P2）: 「更新日時が新しい順」を
    やめ**ファイル名順**にした（`character_manager.reference_files()` が既に名前順を返すので、
    ここでmtime再ソートしていたのを削除しただけ）。理由・上限2→3の経緯は
    `panel_library_manager._resolve_refs` の docstring 参照。
    """
    chars = [c for c in characters if c][:2]
    per_char = 3 if len(chars) <= 1 else 1
    refs: list[tuple[bytes, str, str]] = []
    for cid in chars:
        c = character_manager.read_character(cid)
        name = (c or {}).get("name") or cid
        files = [character_manager.char_dir(cid) / "reference" / fn
                 for fn in character_manager.reference_files(cid)][:per_char]
        if not files:
            if log is not None:
                log.append(f"⚠️ {name}（{cid}）は参照画像が無いため一貫性が担保されません"
                           "（生成のたびに別人になります）")
            continue
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


def _propagate_cut_result(
    project_id: str, episode: int, manifest: dict, head: dict, sibling_ids: list[str],
    log: list[str] | None = None,
) -> None:
    """カットの先頭行(head)が確定した絵を、同じカットの兄弟行にも反映する（穴9 §15-1）。

    apply_cutout_plan/set_cutout_selection と同じ「カット単位で1枚を共有する」原則を
    生成経路にも適用する。画像ファイルは _apply_copy と同じ手順でコピーする
    （``image_source="copied"``・``copied_from`` は Phase2 のバッチ内コピーと同じ値を使い回す。
    「代表行の画像を頂く」という意味は同じなので出自を分ける schema 追加はしない）。
    cutout_slot_id の消費（times_used）はカットにつき1回に抑える ── head 側で既に
    record_usage 済みなので、ここでは count=False で used_by の記録だけ行う
    （set_cutout_selection の consumption ルールと同じ）。

    ⚠️ **呼び出し側が最後に save_manifest すること。** ここでは保存しない
    （generate_line_image が head/兄弟をまとめて1回で書き出す）。
    """
    panels_by_id = {p.get("line_id"): p for p in manifest.get("panels", [])}
    head_line_id = head.get("line_id")
    new_slot_id = head.get("cutout_slot_id")
    new_char_id = head.get("cutout_char_id")
    new_source = head.get("cutout_source")
    new_assigned_at = head.get("cutout_assigned_at")

    for sid in sibling_ids:
        sib = panels_by_id.get(sid)
        if sib is None:
            continue
        if not _apply_copy(project_id, episode, manifest,
                           {"line_id": sid, "copy_from": head_line_id, "variant_id": None}, log=log):
            continue
        prev_slot_id, prev_char_id = sib.get("cutout_slot_id"), sib.get("cutout_char_id")
        if prev_slot_id and prev_char_id and prev_slot_id != new_slot_id:
            panel_library_manager.release_usage(
                prev_char_id, prev_slot_id, project_id=project_id, episode=episode, line_id=sid)
        if new_slot_id and new_char_id:
            sib["cutout_slot_id"] = new_slot_id
            sib["cutout_char_id"] = new_char_id
            sib["cutout_source"] = new_source
            sib["cutout_assigned_at"] = new_assigned_at
            panel_library_manager.record_usage(
                new_char_id, new_slot_id, project_id=project_id, episode=episode,
                line_id=sid, count=False)   # 消費はカットに1回（headで数え済み）
        else:
            # headが2ショット等で在庫を持たない（cutout_slot_id無し）場合は兄弟も同じく持たない
            sib["cutout_slot_id"] = None
            sib["cutout_char_id"] = None
            sib["cutout_source"] = None
            sib["cutout_assigned_at"] = None
        clear_image_approval(project_id, episode, sib, demote=False)


async def generate_line_image(
    project_id: str, episode: int, line_id: str,
    allow_paid_fallback: bool = False, log: list[str] | None = None,
    use_library: bool = True, library_only: bool = False,
    exclude_slot_ids: set[str] | None = None,
    ensure_prompt: bool = True,
) -> dict:
    """1行分のパネル画像を生成してマニフェストへ反映する（成功/失敗とも記録）。

    ``ensure_prompt``: 新規生成の直前にプロンプトが無ければ LLM で作る（E4・`ensure_prompts`）。
    在庫の引用はプロンプトを使わないので、プロンプトが要るのは新規生成の時だけ。
    バッチ（`run_batch`）は先にまとめて作るので False を渡す（行ごとに拒否を繰り返さない）。

    use_library=True（既定）: 先にキャラ所有ライブラリ（Phase 3）を引き、一致すれば
    無料でコピーして即返す（NanoBananaは呼ばない）。「この行だけ作り直す」時は
    use_library=False を渡してライブラリを迂回し、必ず新規生成させる。

    library_only=True: ライブラリに一致が無かった場合、課金生成へフォールバックせず
    ValueErrorを返す（無音の意図しない課金を防ぐ）。「根拠（slot）を変更したら自動で
    再解決する」UI操作のように、ユーザーがドロップダウンを触っただけで課金が走ると
    驚かせてしまう場面で使う。use_library=Falseと同時指定は矛盾するため呼び出し禁止。

    exclude_slot_ids: 1話分のバッチ処理中に呼び出し側（run_batch）が蓄積する「既に他の
    行へ割り当てたslot_id」。_library_lookup にそのまま中継する
    （詳細 panel_library_manager.find_current のdocstring）。単発の1行呼び出し
    （手動の「作り直す」等）では省略してよい。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found (run /aroll/prompts first)")
    panel = next((p for p in manifest["panels"] if p.get("line_id") == line_id), None)
    if panel is None:
        raise ValueError(f"line not found in aroll.json: {line_id}")
    if panel.get("orphan"):
        raise ValueError(f"台本から削除された行です（生成しません）: {line_id}")
    # プロンプトが要るのは新規生成の時だけ（在庫の引用は使わない）。無ければ生成の直前に作る

    out_dir = aroll_dir(project_id, episode)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 穴9 §15-1: この行が属するカットの兄弟行。生成/ライブラリ引用の結果は
    # 兄弟行にも伝播させる（1カット1枚の原則。select_targets が先頭行だけを渡す前提だが、
    # 手動の「この行だけ作り直す」1行エンドポイントから直接呼ばれた時も同じ扱いにする ──
    # カットは定義上「同じ絵を共有する区間」なので、片方だけ絵が変わるとカットが割れる）。
    cut = cut_planner.cut_of_line(cut_report(project_id, episode, manifest)["cuts"]).get(line_id)
    sibling_ids = [lid for lid in (cut or {}).get("line_ids", []) if lid != line_id]

    if use_library:
        if exclude_slot_ids is None:
            # 単発の呼び出し（行ごとの「作り直す」・在庫で埋める等）も、この話数で既に他の行が使っている
            # 絵は引かない（除外集合がバッチの中だけで積まれていた穴。Docs/AROLL_DUPLICATE_CHECK_PLAN.md §5 D0）
            own = set((cut or {}).get("line_ids", [line_id]))
            mine = [c for c in (panel.get("characters") or []) if c]
            if len(mine) == 1:
                exclude_slot_ids = {
                    q["cutout_slot_id"] for q in manifest["panels"]
                    if not q.get("orphan") and q.get("line_id") not in own
                    and q.get("cutout_slot_id") and q.get("cutout_char_id") == mine[0]}
        lib_hit = _library_lookup(panel, exclude_slot_ids=exclude_slot_ids)
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
            clear_image_approval(project_id, episode, panel, demote=False)

            # T5（穴4）: この経路（find_current の完全一致）が当てた entry が
            # 2026-08-27以降の形式（cutout+fingerprintを併せ持つ）なら、T1と同じく
            # cutout_slot_id も直接指させる。⚠️ **これをやらないと「ライブラリに
            # 一致したのに背景付きのまま」＝下流がPhotoshop切り抜きへ回ってしまう
            # （batch_cutout.py が対象ゼロになったT1の恩恵をこの経路だけ受けられない）。
            # find_current と cutout_selector.candidates は別の一致基準（完全一致 vs
            # 指紋距離）で選ぶ別系統のままでよい（索引統合はS1で見送り済み）── ここは
            # 「選んだ後、同じ絵の切り抜きも一緒に使う」という出口の一本化だけを行う。
            prev_slot_id, prev_char_id = panel.get("cutout_slot_id"), panel.get("cutout_char_id")
            if prev_slot_id and prev_char_id and prev_slot_id != lib_hit.get("slot_id"):
                panel_library_manager.release_usage(
                    prev_char_id, prev_slot_id, project_id=project_id, episode=episode,
                    line_id=line_id)
            if panel_library_manager.usable_as(lib_hit)["cutout"]:
                panel["cutout_slot_id"] = lib_hit["slot_id"]
                panel["cutout_char_id"] = lib_hit["char_id"]
                panel["cutout_source"] = "library"
                panel["cutout_assigned_at"] = _now()
            else:
                # 旧形式（背景付きの image のみ・cutout 無し）。ここだけは今も
                # Photoshop切り抜きが要る。件数は自然に減っていく（新規生成は全てT1で
                # cutout を伴って登録されるため、旧形式のまま残るのは移行前の資産だけ）。
                panel["cutout_slot_id"] = None
                panel["cutout_char_id"] = None
                panel["cutout_source"] = None
                panel["cutout_assigned_at"] = None
                if log is not None:
                    log.append(f"ℹ️ {line_id} は旧形式のライブラリ資産（切り抜き無し）のため"
                              "Photoshop切り抜きが必要です")

            panel_library_manager.record_usage(
                lib_hit["char_id"], lib_hit["slot_id"],
                project_id=project_id, episode=episode, line_id=line_id)
            if sibling_ids:
                _propagate_cut_result(project_id, episode, manifest, panel, sibling_ids, log=log)
            save_manifest(project_id, episode, manifest)
            if log is not None:
                log.append(f"📚 {line_id} ライブラリから引用: {lib_hit['char_id']}/{lib_hit.get('slot_id')}")
            return panel
        if library_only:
            raise ValueError(
                f"ライブラリに一致するスロットがありません（{line_id}）。"
                "課金生成するにはキャラ画像タブでバリアントを作るか、「この行を新規生成」を使ってください。"
            )

    if _panel_needs_prompt(panel):
        got = None
        if ensure_prompt:
            got = await ensure_prompts(project_id, episode, [line_id], log=log)
            manifest = load_manifest(project_id, episode) or manifest
            panel = next((p for p in manifest["panels"] if p.get("line_id") == line_id), panel)
        if _panel_needs_prompt(panel):
            why = "; ".join(f["error"] for f in (got or {}).get("failed", []))
            raise ValueError(f"prompt is empty: {line_id}" + (f"（演出プロンプトを作れませんでした: {why}）" if why else ""))

    full_prompt = _compose_prompt(panel, manifest.get("style", "kamishibai"))
    refs = _resolve_refs(panel.get("characters", []), log)

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
        # 作り直した＝この絵は人の承認を経ていない。承認を外し、前の絵から取り込んだ
        # 在庫は自動配布を止める（気に入らなくて描き直した絵が後の話数で出るのを防ぐ）
        demoted = clear_image_approval(project_id, episode, panel, demote=True)
        if demoted and log is not None:
            log.append(f"⬇️ {line_id} 作り直しに伴い在庫を未承認へ降格: {', '.join(demoted)}")

        # T1（Docs/AROLL_UNIFIED_FLOW_PLAN.md）: 生成物を自動で在庫へ通す。
        # 「1行の絵は必ず在庫から来る」を満たすため、生成した瞬間にpendingで在庫登録し、
        # この行はその slot_id を直接指す（他の行からは find_current 経由でしか
        # 引かれない＝pending除外が効き、この行専用のまま。§3-1参照）。
        # ⚠️ 単独キャラの行のみ対象。2ショットはライブラリ非対応（_library_lookup と同じ制約、
        # §6「やらないこと」）なので、複数キャラの行は従来どおり image のみで扱う。
        #
        # ⚠️ 作り直すたびに古い cutout_slot_id は必ず外す。新しい登録が成功すればすぐ
        # 上書きするが、対象外/失敗の時もここで外さないと「image は新しいのに
        # cutout_slot_id だけ前の絵を指す」不整合が残る（plan_builder は cutout_slot_id を
        # 優先するため、せっかく作り直した絵が無視されてしまう）。
        prev_slot_id, prev_char_id = panel.get("cutout_slot_id"), panel.get("cutout_char_id")
        if prev_slot_id and prev_char_id:
            panel_library_manager.release_usage(
                prev_char_id, prev_slot_id, project_id=project_id, episode=episode, line_id=line_id)
        panel["cutout_slot_id"] = None
        panel["cutout_char_id"] = None
        panel["cutout_source"] = None
        panel["cutout_assigned_at"] = None

        chars = [c for c in (panel.get("characters") or []) if c]
        slot = panel.get("slot") or {}
        emotion, shot, angle = slot.get("emotion"), slot.get("shot"), slot.get("angle")
        if len(chars) == 1 and emotion and shot and angle:
            char_id = chars[0]
            # ⚠️ **二重在庫の入口はここ**（approve_images と同じ理由・同じガード）。参照画像が
            # 無いキャラの絵を在庫に積むと「似た別人」が同じキャラの在庫として配られてしまう
            # （外見だけ複製したVoiceバリアントキャラで実際に起きうる。§15-0）。生成自体は
            # 止めない（この行の絵は必要）が、在庫への登録だけを塞ぐ。
            can_stock, why = character_manager.can_generate_images(char_id)
            if not can_stock:
                if log is not None:
                    log.append(f"ℹ️ {line_id} は在庫に積めません（{why}）。絵はこの行にだけ使われます")
            else:
                reg = panel_library_manager.register_from_image(
                    char_id, data, emotion=emotion, shot=shot, angle=angle,
                    pose=slot.get("pose") or "", prompt=full_prompt,
                    style_name=manifest.get("style", "kamishibai"), provider="nanobanana",
                    source={"project_id": project_id, "episode": episode, "line_id": line_id},
                    review_status="pending", emotion_tag=slot.get("emotion_tag") or "",
                )
                if reg.get("slot_id"):
                    # set_cutout_selection() は review_status="approved" を要求するため使えない
                    # （他の行が在庫を借りる時の承認ゲートであって、この行が自分の生成物を
                    # 直接指すのとは別の操作。ここは意図的にゲートを通さない）。
                    panel["cutout_slot_id"] = reg["slot_id"]
                    panel["cutout_char_id"] = char_id
                    panel["cutout_source"] = "generated"
                    panel["cutout_assigned_at"] = _now()
                    panel_library_manager.record_usage(
                        char_id, reg["slot_id"],
                        project_id=project_id, episode=episode, line_id=line_id)
                    if log is not None:
                        log.append(f"📦 {line_id} 生成物を在庫へ登録(pending): {char_id}/{reg['slot_id']}")
                elif log is not None:
                    log.append(f"⚠️ {line_id} 切り抜きに失敗し在庫登録できませんでした: {reg.get('reason')}")
        elif len(chars) > 1 and log is not None:
            log.append(f"ℹ️ {line_id} は2人以上写る行のため在庫登録の対象外です（キャラ単独限定）")

        if sibling_ids:
            _propagate_cut_result(project_id, episode, manifest, panel, sibling_ids, log=log)
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


def _panel_decided(p: dict) -> bool:
    """その行の絵が決まっているか（実生成済み or 在庫の切り抜きを適用済み）。"""
    return p.get("status") == "done" or bool(p.get("cutout_slot_id"))


def select_targets(
    project_id: str, episode: int, manifest: dict,
    line_ids: list[str] | None, only_missing: bool,
    require_prompt: bool = True,
) -> list[dict]:
    """バッチ対象パネルを選ぶ。**対象は行ではなくカットの先頭行**（穴9 §15-1・2026-09-21）。

    カットは同じ絵を共有する区間（``cut_planner`` 参照）。1カット1枚の原則を生成経路にも
    揃えるため、対象はカットにつき1行（先頭）だけを返す。実際の生成/ライブラリ引用は
    その先頭行だけが行い、兄弟行への反映は ``generate_line_image`` が担う
    （``apply_cutout_plan``/``set_cutout_selection`` と同じ役割分担。これをしないと、
    在庫が薄いカットは行の数だけ NanoBanana を呼び、兄弟行に別々の絵が入ってしまう
    ＝1カット1枚の原則が新規生成時だけ効かない）。

    only_missing=True なら「カット全体がもう決まっている」カットを除外する
    （＝レジューム/失敗再試行）。``auto_assign_backgrounds`` と同じ判定を踏襲し、
    カット内のどれか1行でも未決定なら、カット全体をやり直し対象として先頭行を返す。
    「決まっている」の定義は以前と同じ ── ``status=="done"``（実生成済み）または
    ``cutout_slot_id`` あり（在庫の切り抜きを適用済み。実測: 承認3行のつもりが11行課金・
    約$0.32過剰だった事故の再発防止。詳細 memory/aroll-batch-ignores-cutout-plan）。

    台本から消えた行（orphan）は ``cut_report`` が最初から除外する。line_ids を明示した
    場合、カットのどの行が指定されていてもそのカット（の先頭）が対象になる。

    ⚠️ **ナレーション行（``characters`` が空）も常に除外する。** 話者が変われば必ず
    カットの境界になるため（``cut_planner._runs``）、先頭行だけ見れば足りる。ここを
    外さないと、参照画像0枚のまま「誰でもない人物」が課金生成される。

    ``require_prompt=False``: プロンプトが無いカットも対象に含める（`Docs/AROLL_EMOTION_LOCAL_PLAN.md`
    §5 E4）。独立した新しい行はプロンプトを持たない（確定の下ごしらえは LLM を呼ばない）ので、
    既定のままだと「残りを生成」・見積もりから**黙って落ちる**。生成の直前に `ensure_prompts` が作る。
    在庫から選べる行はプロンプトを使わない（`build_generation_plan` が先に振り分ける）。
    """
    panels_by_id = {p.get("line_id"): p for p in manifest.get("panels", []) if not p.get("orphan")}
    cuts = cut_report(project_id, episode, manifest)["cuts"]
    # ⚠️ 空リストは「1行も選んでいない」＝対象ゼロ（省略＝None が「全行」）。
    # falsy判定にすると、行を1つも選んでいないのに全行へ課金生成が走る。
    wanted = None if line_ids is None else set(line_ids)

    targets = []
    for c in cuts:
        members = [panels_by_id[lid] for lid in c["line_ids"] if lid in panels_by_id]
        if not members:
            continue
        head = members[0]
        if not head.get("characters"):
            continue
        if require_prompt and not (head.get("prompt") or "").strip():
            continue
        if wanted is not None and not any(m.get("line_id") in wanted for m in members):
            continue
        if only_missing and all(_panel_decided(m) for m in members):
            continue
        targets.append(head)
    return targets


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


def _episode_used_slots(project_id: str, episode: int, manifest: dict,
                        targets: list[dict]) -> dict[str, set[str]]:
    """この話数で**既に他の行が使っている絵**（キャラごと）。生成の在庫引きの除外集合の初期値。

    生成対象（`targets`＝カットの先頭行）のカットの行は除く（その行自身の今の絵は差し替える側）。
    ⚠️ これを渡さないと、一括生成の在庫引き（`find_current`）が**話数で既に使われている絵**を
    もう一度当てる（除外集合がバッチの中だけで積まれていた＝Docs/AROLL_DUPLICATE_CHECK_PLAN.md §5 D0）。
    """
    cuts = cut_report(project_id, episode, manifest)["cuts"]
    cut_of = cut_planner.cut_of_line(cuts)
    own: set[str] = set()
    for t in targets:
        own.update((cut_of.get(t["line_id"]) or {"line_ids": [t["line_id"]]})["line_ids"])
    out: dict[str, set[str]] = {}
    for p in manifest.get("panels", []):
        if p.get("orphan") or p.get("line_id") in own:
            continue
        cid, sid = p.get("cutout_char_id"), p.get("cutout_slot_id")
        if cid and sid:
            out.setdefault(cid, set()).add(sid)
    return out


def build_generation_plan(
    targets: list[dict], max_reuse: int = 1, min_gap: int = 8, use_library: bool = True,
    already_used: dict[str, set[str]] | None = None,
) -> dict:
    """targets(select_targetsの出力＝カットの先頭行のみ)を「ライブラリ引用」「実生成する代表行」
    「コピーで済む行」に振り分ける。

    ライブラリ引用（Phase 3・use_library）が最優先: 単独キャラのパネルでキャラ所有ライブラリに
    一致（かつappearance_versionが最新）があれば、バッチ内dedupより先にそちらを使う（$0）。
    残りについて、slot_key を持たない行・max_reuse<=1 の時は常に個別生成（安全側）。
    ⚠️ ここで言う「コピー」は同一バッチ内の演技スロット使い回し（Phase2）。カットの兄弟行への
    伝播は別経路（generate_line_image が担う。穴9 §15-1）で、ここには出てこない。
    Returns: {"library": [{"line_id","char_id","slot_id"}...],
              "generate": [{"line_id","variant_id"}...], "copy": [{"line_id","copy_from","variant_id"}...],
              "generate_count", "copy_count", "library_count", "max_reuse", "min_gap"}
    """
    max_reuse = max(1, max_reuse)
    min_gap = max(0, min_gap)
    order_by_id = {p["line_id"]: p.get("order", 0) for p in targets}

    # ⚠️ **1話の中で同じslot_idを二度割り当てない**（2026-09-23）。times_usedの生涯累計
    # だけでは、新しく追加したバリアントに複数行が引き寄せられて同じ絵に収束することがある
    # （詳細 memory/aroll-duplicate-cutout-same-batch）。ここでの判定は実際に消費する
    # run_batch のループと**同じアルゴリズム（順番にexclude_slot_idsを蓄積）**でなければ、
    # 見積もり（ここ）と実際の課金結果がズレる事故になるので、両方を必ず対で直すこと。
    # already_used: この話数で既に他の行が使っている絵（run_batch と同じ初期値を渡す＝見積もりと実適用を揃える）
    used_slots: dict[str, set[str]] = {c: set(s) for c, s in (already_used or {}).items()}
    library_entries: list[dict] = []
    remaining: list[dict] = []
    for p in targets:
        lib_hit = None
        if use_library:
            chars = [c for c in (p.get("characters") or []) if c]
            cid = chars[0] if len(chars) == 1 else None
            exclude = used_slots.get(cid) if cid else None
            lib_hit = _library_lookup(p, exclude_slot_ids=exclude)
        if lib_hit is not None:
            library_entries.append({
                "line_id": p["line_id"], "char_id": lib_hit["char_id"], "slot_id": lib_hit["slot_id"],
            })
            used_slots.setdefault(lib_hit["char_id"], set()).add(lib_hit["slot_id"])
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
    # プロンプトの無い行も数える（実行側 `run_batch` と同じ対象。在庫から選べない行だけ演出プロンプトを作る＝E4）
    targets = select_targets(project_id, episode, manifest, line_ids, only_missing, require_prompt=False)
    plan = build_generation_plan(targets, max_reuse=max_reuse, min_gap=min_gap, use_library=use_library,
                                 already_used=_episode_used_slots(project_id, episode, manifest, targets))
    unkeyed =sum(1 for e in plan["generate"] if e["variant_id"] is None)
    panels = {p.get("line_id"): p for p in manifest.get("panels", [])}
    prompt_needed = sum(1 for e in plan["generate"] if _panel_needs_prompt(panels.get(e["line_id"])))
    return {
        "target_count": len(targets),
        # 生成の直前に LLM で演出プロンプトを作る行の数（拒否されたら有料の最終フォールバックが動く環境もある）
        "prompt_needed_count": prompt_needed,
        "paid_prompt_fallback": bool(aroll_prompt_generator.paid_fallback_model()),
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
    # プロンプトの無い行も対象に含める（独立した新しい行。在庫で賄えない行だけ、下で生成の直前に作る＝E4）
    targets = select_targets(project_id, episode, manifest, line_ids, only_missing, require_prompt=False)
    seed_used = _episode_used_slots(project_id, episode, manifest, targets)
    plan = build_generation_plan(targets, max_reuse=max_reuse, min_gap=min_gap, use_library=use_library,
                                 already_used=seed_used)
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

    # E4: 新しく生成する行のうちプロンプトの無い行だけ、LLM で演出プロンプトを作る（在庫から選ぶ行・
    # プロンプトがある行は呼ばない）。作れなかった行は下の生成で「失敗」として理由つきで記録される
    panels_now = {p.get("line_id"): p for p in (load_manifest(project_id, episode) or {}).get("panels", [])}
    need_prompt = [e["line_id"] for e in generate_entries if _panel_needs_prompt(panels_now.get(e["line_id"]))]
    if need_prompt:
        job["current_line"] = need_prompt[0]
        log.append(f"✍ プロンプトの無い {len(need_prompt)}行の演出プロンプトを作ります…")
        try:
            await ensure_prompts(project_id, episode, need_prompt, log=log)
        except Exception as e:  # noqa: BLE001
            log.append(f"✘ 演出プロンプトの作成に失敗: {str(e)[:150]}")

    # ⚠️ build_generation_plan の見積もりと同じアルゴリズム（char_idごとに使用済み
    # slot_idを蓄積）で実適用する。片方だけ直すと見積もりと実際の課金結果がズレる
    # （詳細 memory/aroll-duplicate-cutout-same-batch・2026-09-23）。
    used_slots: dict[str, set[str]] = {c: set(s) for c, s in seed_used.items()}
    for entry in plan["library"]:
        if job["cancel"]:
            break
        lid = entry["line_id"]
        job["current_line"] = lid
        try:
            char_hint = entry.get("char_id")
            exclude = used_slots.get(char_hint) if char_hint else None
            result_panel = await generate_line_image(
                project_id, episode, lid, log=log, use_library=True,
                exclude_slot_ids=exclude, ensure_prompt=False,
            )
            got_char, got_slot = result_panel.get("cutout_char_id"), result_panel.get("cutout_slot_id")
            if got_char and got_slot:
                used_slots.setdefault(got_char, set()).add(got_slot)
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
                # ⚠️ 計画が「在庫に使える絵が無い」と判断した行なので、ここで在庫をもう一度引く時も
                # **同じ除外集合**を渡す（渡さないと、計画が除外した「この話数で使用済みの絵」を
                # 生成の直前に引き直して当ててしまう）
                p_chars = [c for c in ((panels_now.get(lid) or {}).get("characters") or []) if c]
                await generate_line_image(
                    project_id, episode, lid,
                    allow_paid_fallback=allow_paid_fallback, log=log,
                    use_library=use_library, ensure_prompt=False,
                    exclude_slot_ids=used_slots.get(p_chars[0]) if len(p_chars) == 1 else None,
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


def _fixed_entries(cuts: list[dict], panels_by_id: dict, reselect: set[str] | None) -> list[dict | None]:
    """既に実際の絵が決まっているカットの在庫 entry（`cutout_selector.plan_episode(fixed=)` へ渡す）。

    選び直す行（`reselect`）を含むカットは None（選び直す）。在庫に無い entry も None。
    ⚠️ library.json は呼ぶたびに丸ごと読むのでキャラごとに1回だけ読む。
    """
    cache: dict[str, dict] = {}
    out: list[dict | None] = []
    for c in cuts:
        if reselect and reselect & set(c["line_ids"]):
            out.append(None)
            continue
        head = next((panels_by_id[l] for l in c["line_ids"]
                     if l in panels_by_id and panels_by_id[l].get("cutout_slot_id")
                     and panels_by_id[l].get("cutout_char_id")), None)
        if head is None:
            out.append(None)
            continue
        cid = head["cutout_char_id"]
        if cid not in cache:
            cache[cid] = {e.get("slot_id"): e for e in panel_library_manager.load_index(cid).get("entries", [])}
        out.append(cache[cid].get(head["cutout_slot_id"]))
    return out


def cutout_plan(project_id: str, episode: int, reselect_line_ids: set[str] | None = None) -> dict:
    """切り抜き在庫から全行を割り当ててみる（**検査のみ・何も変更しない**）。

    ⚠️ **既に絵が決まっているカットは、その実際の絵を入力として渡す**（`plan_episode(fixed=)`）。
    選び直さない＝試算の `used`・直近の窓が現実と一致するので、後から足した行だけを埋める部分適用が
    決定済みの行と同じ絵を当てない（Docs/AROLL_DUPLICATE_CHECK_PLAN.md §5 D0）。
    `reselect_line_ids` に含まれる行のカットだけは、決定済みでも選び直す（行を明示した適用）。

    「在庫で賄える行」と「新規生成が要る行」を分ける。UI の予算のつまみはこの結果を使う ──
    モードを選ばせるのではなく、**新規生成が要ると出た行のうち何枚を実際に作るか**を
    決めるのがユーザーの操作（Docs/../CHARACTER_CUTOUT_PLAN.md §7-4・§10-9）。

    ⚠️ times_used は増やさない。実際に消費した時だけ record_usage を呼ぶこと
       （find_current と同じ約束。ドライランで増やすとローテーションが狂う）。

    ⚠️ **件数（from_stock / need_generation とその _cuts）は「まだ絵が1枚も決まっていない
    カット」だけを数える**（2026-09-24）。選定は文脈（直前との距離・カメラプラン）のため
    全カットで行うが、決定済みのカットまで数えると「在庫でN行」が実際に埋まる数より多く出て、
    UI の課金見積り（生成対象 − from_stock_cuts）が実際より安く出ていた。
    一部だけ決まっているカット（既存カットに新しい行が入った等）も決定済みとして扱う
    ── 在庫で選び直すと確定済みの行の絵まで替わるため。各行の ``decided`` で区別できる。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")

    panels = [p for p in manifest.get("panels", []) if not p.get("orphan")]
    panels_by_id = {p.get("line_id"): p for p in panels}

    # ⚠️ **選定はカット単位で行う**（穴9 §11-3・2026-09-20）。行ごとに選ぶと、TTSの都合で
    # 割った同一話者の連続行に別々の絵が当たり、細切れのカットが並ぶ。
    # 1カット＝1枚を選び、そのカットの全行が同じ `cutout_slot_id` を指す。
    cuts = cut_report(project_id, episode, manifest)["cuts"]
    seq = []
    for c in cuts:
        # カットの代表は先頭行。⚠️ 感情が適格性の主軸なので、カット内で感情が割れていると
        # 先頭行の感情で引くことになる（実測ではランの18組中17組が同一感情なので実害は小さい）。
        head = panels_by_id.get(c["line_ids"][0], {})
        chars = head.get("characters") or []
        seq.append((chars[0] if len(chars) == 1 else None, head.get("slot")))

    # ⚠️ **カメラプラン（U2）は「希望」を渡すだけ**。ハード制約にすると、その段の在庫が
    # 無いカットが新規生成へ落ちて課金が増える。選定は従来どおり指紋で決め、
    # 同点のときにプランの段を優先する（cutout_selector._select_from の off_plan）。
    char_of_cut = {}
    for c in cuts:
        head = panels_by_id.get(c["line_ids"][0], {})
        chars = head.get("characters") or []
        if len(chars) == 1:
            char_of_cut[c["cut_id"]] = chars[0]
    stock_by_char = {
        cid: cutout_selector.candidates(cid, None, allow_unknown_emotion=True)
        for cid in set(char_of_cut.values())
    }
    cam = camera_plan.plan_episode(cuts, stock_by_char, char_of_cut)
    # 段と向きの両方を渡す（向きを落とすと正面ばかりが選ばれる。2026-09-20実測）
    want_shots = [{"shot": cam.get(c["cut_id"], {}).get("shot"),
                   "facing": cam.get(c["cut_id"], {}).get("facing")} for c in cuts]

    plan = cutout_selector.plan_episode(
        seq, want_shots, fixed=_fixed_entries(cuts, panels_by_id, reselect_line_ids))
    lines, from_stock_cuts, open_cuts = [], 0, 0
    for c, r in zip(cuts, plan):
        e = r.get("entry")
        decided = any(_panel_decided(panels_by_id[lid])
                      for lid in c["line_ids"] if lid in panels_by_id)
        if not decided:
            open_cuts += 1
            if e:
                from_stock_cuts += 1
        for lid in c["line_ids"]:
            lines.append({
                "line_id": lid,
                "cut_id": c["cut_id"],
                "cut_role": c["role"],
                "decided": decided,
                # カメラプランが欲しがった段（希望）。実物とズレていれば代用が起きた印
                "planned_shot": cam.get(c["cut_id"], {}).get("shot"),
                "planned_facing": cam.get(c["cut_id"], {}).get("facing"),
                # カットの先頭行だけが実際に在庫を消費する（残りは同じ絵を共有するだけ）。
                # apply 側がこれを見て record_usage を1回に抑える。
                "cut_head": lid == c["line_ids"][0],
                "char_id": r.get("char_id"),
                "emotion": r.get("emotion"),
                "slot_id": e.get("slot_id") if e else None,
                "cutout": e.get("cutout") if e else None,
                "times_used": e.get("times_used", 0) if e else None,
                "reason": r.get("reason"),
            })
    open_lines = [ln for ln in lines if not ln["decided"]]
    from_stock = sum(1 for ln in open_lines if ln["slot_id"])
    return {
        "thresholds": cutout_selector.thresholds(),
        "total": len(lines),
        "total_cuts": len(cuts),
        "decided_cuts": len(cuts) - open_cuts,
        "from_stock": from_stock,
        "from_stock_cuts": from_stock_cuts,
        # ⚠️ 課金の見積りは**行数ではなくカット数**で見る（1カット＝1枚）
        "need_generation": len(open_lines) - from_stock,
        "need_generation_cuts": open_cuts - from_stock_cuts,
        "stock_warnings": _stock_warnings(manifest),
        "lines": lines,
    }


def _stock_warnings(manifest: dict) -> list[dict]:
    """この話で薄い在庫の事前警告（`stock_health`・Docs/STOCK_LABEL_ACCURACY_PLAN.md §4-7 L6）。
    例「アオイ thoughtful（物思い） 13行／在庫10枚」。監査の失敗で試算を止めない（空で返す）。"""
    try:
        return stock_health.episode_health(manifest.get("panels", []))["thin"]
    except Exception:
        return []


def cutout_candidates(project_id: str, episode: int, line_id: str, limit: int = 12) -> dict:
    """1行分の切り抜き候補を「直近から遠い順」に返す（検査のみ・何も変更しない）。

    slot(emotion/shot/angle)一致では並べない ── 実測でスロット軸は使い回し感を
    説明しなかった（CHARACTER_CUTOUT_PLAN.md §10-1）。`distance` は直近W行との最短距離で、
    大きいほど「新鮮」。閾値未満のものは `too_close` を立てて返す（**隠さない**）:
    ユーザーが承知で選ぶ場合があるし、「全部近い＝生成すべき」と一目で分かる方がよい。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")
    panels = manifest.get("panels", [])
    idx = next((i for i, p in enumerate(panels) if p.get("line_id") == line_id), None)
    if idx is None:
        raise ValueError(f"line not found: {line_id}")

    p = panels[idx]
    chars = p.get("characters") or []
    char_id = chars[0] if len(chars) == 1 else None
    emotion = (p.get("slot") or {}).get("emotion")
    tag = (p.get("slot") or {}).get("emotion_tag")
    th = cutout_selector.thresholds()
    if not char_id:
        return {"line_id": line_id, "char_id": None, "emotion": emotion,
                "threshold": th["repetitive_below"], "recent": [], "items": [],
                "reason": "キャラが1人に確定していない行（2人写り等）"}

    # 直近W行に実際に割り当たっている切り抜きを集める（無ければ空＝どれでも新鮮）
    recent_ids, recent = [], []
    for q in panels[max(0, idx - th["recent_window"]):idx]:
        # ⚠️ 直近の行が使っている切り抜きは `cutout_slot_id`。`library_slot_id` は
        #    パネル画像（背景込み）の方で、ここで見ると直近が常に空になり
        #    「近すぎ」判定が一度も発火しない（実際にそのバグを出した）。
        sid = q.get("cutout_slot_id")
        if not sid or (q.get("cutout_char_id") or (q.get("characters") or [None])[0]) != char_id:
            continue
        e = panel_library_manager.get_entry(char_id, sid)
        # ⚠️ 適格判定の本籍は usable_as（`kind` は出自の記録であって用途ではない。
        #    panel_library_manager.usable_as 参照）。kind で見ていた時は、背景除去を
        #    Python化した2026-08-27以降の entry（kind="panel" のまま cutout を持つ）が
        #    直近リストから丸ごと抜け落ち、「近すぎ」判定が発火しなかった。
        if e and panel_library_manager.usable_as(e)["cutout"]:
            recent_ids.append(q.get("line_id"))
            recent.append(e)

    items = []
    # 手動ピッカーなので感情未指定でも全候補を出す（人が見て選ぶなら制約は要らない）
    for e in cutout_selector.candidates(char_id, emotion, allow_unknown_emotion=True, tag=tag):
        d = min((cutout_selector.distance(e.get("fingerprint"), r.get("fingerprint")) for r in recent),
                default=1.0)
        items.append({
            "slot_id": e["slot_id"], "cutout": e.get("cutout"),
            "facing": cutout_selector.orientation(e),
            "times_used": e.get("times_used", 0), "distance": round(d, 3),
            "too_close": d < th["repetitive_below"],
            "current": e["slot_id"] == p.get("library_slot_id"),
            # 当たり方（0 主タグ／1 副タグ／2 同じ系統／3 副タグの系統・§14）と絵のタグ（UIの表示用）
            "match": cutout_selector.match_level(e, emotion, tag),
            "emotion": e.get("emotion"), "emotion_tag": cutout_selector.entry_tag(e),
            "emotion_sub_tags": e.get("emotion_sub_tags") or [],
        })
    items.sort(key=lambda x: (-x["distance"], x["times_used"], x["slot_id"]))
    return {"line_id": line_id, "char_id": char_id, "emotion": emotion,
            "threshold": th["repetitive_below"], "recent": recent_ids,
            "items": items[:limit], "total_candidates": len(items)}


def _duplicate_inputs(project_id: str, episode: int, protected_line_ids: list[str] | None,
                      window: int, near_threshold: float | None) -> dict:
    """検査と直しが共有する入力（マニフェスト・絵を持つカット・在庫 entry の引き方）。"""
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")
    th = cutout_selector.thresholds()
    near_th = near_threshold if near_threshold is not None else th["repetitive_below"]
    panels_by_id = {p.get("line_id"): p for p in manifest.get("panels", []) if not p.get("orphan")}
    cuts = cut_report(project_id, episode, manifest)["cuts"]
    pcuts = aroll_duplicates.collect_picture_cuts(cuts, panels_by_id, set(protected_line_ids or []))
    # ⚠️ get_entry は呼ぶたびに library.json を丸ごと読む（本番のルカで約4MB）。キャラごとに1回だけ読む
    index_cache: dict[str, dict] = {}

    def entry_of(char_id: str, slot_id: str) -> dict | None:
        if char_id not in index_cache:
            index_cache[char_id] = {e.get("slot_id"): e
                                    for e in panel_library_manager.load_index(char_id).get("entries", [])}
        return index_cache[char_id].get(slot_id)

    report = aroll_duplicates.compute_report(
        pcuts, entry_of, cutout_selector.distance, cutout_selector.orientation,
        near_th, window=window)
    return {"manifest": manifest, "pcuts": pcuts, "entry_of": entry_of, "report": report,
            "panels_by_id": panels_by_id}


def duplicate_report(project_id: str, episode: int, protected_line_ids: list[str] | None = None,
                     window: int = aroll_duplicates.DEFAULT_WINDOW,
                     near_threshold: float | None = None) -> dict:
    """同じ絵・よく似た絵の繰り返しを検査する（**検査のみ・何も変更しない・無料**）。

    判定と除外の規則は `aroll_duplicates` の docstring（`Docs/AROLL_DUPLICATE_CHECK_PLAN.md` §2）。
    範囲は**この話数の中だけ**（他の話数で使った絵は避けない・§4 Q2）。
    protected_line_ids: 手直し済み（✋）の行。✋ は PSD の中身から director が判定するので、
      呼び出し側（director の窓口）が渡す。ここでは判定できない。
    near_threshold: 省略時は `cutout_selector` の `repetitive_below`（`cutout_candidates` の
      `too_close` と同じ）。
    """
    ctx = _duplicate_inputs(project_id, episode, protected_line_ids, window, near_threshold)
    fixes = [{"fix_id": f["fix_id"], "at": f["at"], "mode": f["mode"], "cuts": len(f["changes"]),
              "undone": bool(f.get("undone"))}
             for f in (ctx["manifest"].get("duplicate_fixes") or [])][-3:]
    return {"project_id": project_id, "episode": episode, **ctx["report"], "fixes": fixes}


_FIX_MODES = ("reselect", "unassign")
_APPROVAL_FIELDS = ("image_approved_at", "image_approved_hash")
_FIX_HISTORY = 10   # 話数ごとに残す「重複を直した」履歴の数（Undo 用・古いものから消す）


def fix_duplicates(project_id: str, episode: int, protected_line_ids: list[str] | None = None,
                   window: int = aroll_duplicates.DEFAULT_WINDOW, mode: str = "reselect",
                   line_ids: list[str] | None = None, apply: bool = False,
                   near_threshold: float | None = None) -> dict:
    """検査の指摘を直す（Docs/AROLL_DUPLICATE_CHECK_PLAN.md D2）。**既定は案を返すだけ（apply=False）**。

    mode:
      reselect … 在庫から、**この話数で使っていない**絵へ選び直す（無料）。選び方は既存の選択アルゴリズム
                 （除外集合を渡すだけ）。替えが無い行は `generate`（生成が要る）として案に出るだけで触らない。
      unassign … 替えの絵が在庫に無い行の**絵を外して未決定にする**（無料）。そのあと**その行（応答の
                 `changed`）だけを生成**する（既存の「残りを生成」＝見積もり・確認つき）＝**ここでは生成しない**。
                 ⚠️ **外した行に「在庫で埋める」（`fill_missing`/`apply_cutout_plan`）を使わない**: 在庫が
                 尽きた行へは選択アルゴリズムの段3「この話で再使用」が**同じ絵を当て直す**（仕様）ので、
                 重複が戻る（コピー環境の通しで実測・3/5行）。
    line_ids: 指定した行（そのカットのどの行でもよい）だけを直す。省略は検査の指摘すべて。
    apply=True で書く。選び直した行は絵の「確定」（`image_approved_at`）が外れる（絵が変わるので）。
    Undo は `undo_duplicate_fix`。履歴はマニフェストの `duplicate_fixes`。
    """
    if mode not in _FIX_MODES:
        raise ValueError(f"mode は {' / '.join(_FIX_MODES)} のいずれか")
    ctx = _duplicate_inputs(project_id, episode, protected_line_ids, window, near_threshold)
    items = ctx["report"]["items"]
    if line_ids is not None:
        wanted = set(line_ids)
        items = [it for it in items if wanted & set(it["line_ids"])]
    plan = aroll_duplicates.plan_fixes(
        ctx["pcuts"], items, ctx["entry_of"], cutout_selector.select_replacement, window=window)
    todo = [a for a in plan if a["action"] == ("reselect" if mode == "reselect" else "generate")]
    summary = {
        "found": len(items), "reselect": sum(1 for a in plan if a["action"] == "reselect"),
        "generate": sum(1 for a in plan if a["action"] == "generate"),
        "skip": sum(1 for a in plan if a["action"] == "skip"),
        "keep": sum(1 for a in plan if a["action"] == "keep"),
        "will_apply": len(todo),
        # 概算（確定の見積もりは「残りを生成」の見積もりが正。ここは目安）
        "estimated_cost_usd": round(sum(1 for a in plan if a["action"] == "generate") * AROLL_COST_PER_IMAGE_USD, 2),
    }
    out = {"project_id": project_id, "episode": episode, "mode": mode, "applied": False,
           "plan": plan, "summary": summary, "conflicts": ctx["report"]["conflicts"]}
    if not apply or not todo:
        return out

    manifest = ctx["manifest"]
    panels_by_id = {p.get("line_id"): p for p in manifest.get("panels", [])}
    changes, failed = [], []
    for a in todo:
        members = [panels_by_id[l] for l in a["line_ids"] if l in panels_by_id]
        before = {"slot": a["from_slot"], "char": a["char_id"],
                  "source": next((m.get("cutout_source") for m in members if m.get("cutout_slot_id")), None),
                  "assigned_at": next((m.get("cutout_assigned_at") for m in members if m.get("cutout_slot_id")), None),
                  "approval": {m["line_id"]: {k: m[k] for k in _APPROVAL_FIELDS if m.get(k)} for m in members}}
        target = a["to_slot"] if mode == "reselect" else None
        try:
            set_cutout_selection(project_id, episode, a["line_id"], target, source="plan")
        except ValueError as e:
            failed.append({"line_id": a["line_id"], "reason": str(e)})
            continue
        changes.append({"line_ids": a["line_ids"], "head": a["line_id"], "cut_id": a["cut_id"],
                        "char_id": a["char_id"], "from": a["from_slot"], "to": target, "before": before})
    if changes:
        m2 = load_manifest(project_id, episode) or {}
        fix_id = "fix_" + _now().replace(":", "").replace("-", "")[:15]
        hist = list(m2.get("duplicate_fixes") or [])
        hist.append({"fix_id": fix_id, "at": _now(), "mode": mode, "changes": changes})
        m2["duplicate_fixes"] = hist[-_FIX_HISTORY:]
        save_manifest(project_id, episode, m2)
        out["fix_id"] = fix_id
    out.update(applied=bool(changes), changed=[c["head"] for c in changes], failed=failed)
    if changes and mode == "unassign":
        out["next_step"] = ("changed の行だけを生成する（run_aroll_batch の line_ids・見積もりを確認してから）。"
                            "在庫で埋める（fill_missing）は使わない＝同じ絵が再使用で戻る")
    return out


def undo_duplicate_fix(project_id: str, episode: int, fix_id: str | None = None) -> dict:
    """直前（または fix_id）の「重複を直した」を戻す（絵の割当と確定を元に戻す）。

    **その後に別の変更があった行は戻さない**（今の割当が直した結果と違う行は skipped に理由つきで残す）。
    在庫から消えた絵・未承認になった絵へは戻せない（skipped）。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")
    hist = manifest.get("duplicate_fixes") or []
    cand = [f for f in hist if not f.get("undone") and (fix_id is None or f["fix_id"] == fix_id)]
    if not cand:
        raise ValueError("戻せる「重複を直した」履歴がありません")
    fix = cand[-1]
    restored, skipped = [], []
    for ch in reversed(fix["changes"]):
        head = next((p for p in load_manifest(project_id, episode)["panels"] if p.get("line_id") == ch["head"]), None)
        if head is None or (head.get("cutout_slot_id") or None) != (ch["to"] or None):
            skipped.append({"line_id": ch["head"], "reason": "その後に絵が変わっているので戻さない"})
            continue
        b = ch["before"]
        try:
            set_cutout_selection(project_id, episode, ch["head"], b["slot"], source=b.get("source") or "plan")
        except ValueError as e:
            skipped.append({"line_id": ch["head"], "reason": str(e)})
            continue
        m = load_manifest(project_id, episode)
        by_id = {p.get("line_id"): p for p in m["panels"]}
        for lid in ch["line_ids"]:
            p = by_id.get(lid)
            if p is None:
                continue
            if b.get("assigned_at"):
                p["cutout_assigned_at"] = b["assigned_at"]
            for k, v in (b.get("approval", {}).get(lid) or {}).items():
                p[k] = v
        save_manifest(project_id, episode, m)
        restored.append(ch["head"])
    m = load_manifest(project_id, episode)
    for f in m.get("duplicate_fixes") or []:
        if f["fix_id"] == fix["fix_id"]:
            f["undone"] = True
            f["undone_at"] = _now()
    save_manifest(project_id, episode, m)
    return {"fix_id": fix["fix_id"], "mode": fix["mode"], "restored": restored, "skipped": skipped}


def set_cutout_selection(project_id: str, episode: int, line_id: str, slot_id: str | None,
                         source: str = "user") -> dict:
    """その行が属する**カット全体**で使う切り抜きを決める（`panel["cutout_slot_id"]`）。

    ⚠️ **パネル画像を差し替えるのではない。** 切り抜きは背景を持たないので、そのままでは
    コマにならない。ここで決めるのは「psassist の合成プランにどの素材を渡すか」だけ。
    背景は `background_id`、生成画像は `image` と、行ごとに3つが対になる。

    ⚠️ **1行だけ替えるのではなくカット全体に当てる**（穴9 §9-3・2026-09-20）。
    カットは「同じ絵のまま吹き出しだけ変わる」区間なので、途中の1行だけ絵が変わると
    カットが割れて見える。UIから1行を指定しても、同じカットの兄弟行に同じ絵が入る。

    ⚠️ **消費（times_used）はカットにつき1回**。カット内の行数ぶん数えると、
    絵が画面に出た回数と食い違い、生涯上限（既定3）へ不当に早く到達する。
    逆引きの `used_by` は行ごとに積む（どの行が参照しているかは全部知りたいため）。

    slot_id=None で選択を解除する。前の選択があれば times_used を戻す
    （戻さないと選び直すたびに嘘の消費が積もり、生涯上限へ早く到達する）。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")
    panels_by_id = {p.get("line_id"): p for p in manifest.get("panels", [])}
    panel = panels_by_id.get(line_id)
    if panel is None:
        raise ValueError(f"line not found: {line_id}")
    chars = panel.get("characters") or []
    if len(chars) != 1:
        raise ValueError("キャラが1人に確定していない行には切り抜きを割り当てられません")
    char_id = chars[0]

    if slot_id:
        entry = panel_library_manager.get_entry(char_id, slot_id)
        # ⚠️ ここを `kind` で見ると、cutout_plan（usable_as 経由）が候補に出した entry を
        #    apply が拒否する＝試算と実行が別基準になる（実データで287枚中93枚が該当）。
        if not entry or not panel_library_manager.usable_as(entry)["cutout"]:
            raise ValueError(f"cutout entry not found: {char_id}/{slot_id}")
        if entry.get("review_status", "approved") != "approved":
            raise ValueError(f"未承認の切り抜きは割り当てられません: {slot_id}")

    # このカットに属する行すべてが対象（1行のカットなら従来どおりその行だけ）
    cut = cut_planner.cut_of_line(
        cut_report(project_id, episode, manifest)["cuts"]).get(line_id)
    targets = [panels_by_id[l] for l in (cut or {}).get("line_ids", [line_id])
               if l in panels_by_id]

    if all(t.get("cutout_slot_id") == slot_id for t in targets):
        return {"line_id": line_id, "cutout_slot_id": slot_id, "changed": False,
                "line_ids": [t.get("line_id") for t in targets]}

    # 解放は「この絵を手放す最後の行」でだけ消費を戻す。カット内で共有している間は
    # 消費1のままなので、行ごとに戻すと負の方向へ狂う。
    released: set[str] = set()
    for t in targets:
        prev = t.get("cutout_slot_id")
        prev_char = t.get("cutout_char_id") or char_id
        if prev and prev != slot_id:
            panel_library_manager.release_usage(
                prev_char, prev, project_id=project_id, episode=episode,
                line_id=t.get("line_id"), count=prev not in released)
            released.add(prev)

    for i, t in enumerate(targets):
        if slot_id:
            panel_library_manager.record_usage(
                char_id, slot_id, project_id=project_id, episode=episode,
                line_id=t.get("line_id"), count=(i == 0))   # 消費はカットに1回だけ
        t["cutout_slot_id"] = slot_id
        t["cutout_char_id"] = char_id if slot_id else None
        t["cutout_source"] = source if slot_id else None
        # 合成済みPSDより後に差し替えたかを判定するための時刻。これが無いと
        # 「絵を替えたのに古い合成サムネが出たまま」に気付けない
        t["cutout_assigned_at"] = _now() if slot_id else None
        if slot_id:
            # 組版(Photoshop合成)はaroll.jsonの外で起きて書き戻されないため、
            # 割当の時点で台本テキストを刻んでおく（_panel_sync が「在庫割当済み・
            # 組版前」を判定する唯一の手がかりになる。S0 §14）
            t["source_text"] = t.get("text", "")
            t["source_text_hash"] = text_hash(t.get("text"))
        clear_image_approval(project_id, episode, t, demote=False)
    save_manifest(project_id, episode, manifest)
    return {"line_id": line_id, "cutout_slot_id": slot_id, "changed": True,
            "line_ids": [t.get("line_id") for t in targets],
            "cut_id": (cut or {}).get("cut_id")}


def reject_current_image(project_id: str, episode: int, line_id: str) -> dict:
    """行の絵を「絵そのものが失敗」として、割当を外した上で在庫からも削除する（T2 §4-2）。

    ⚠️ **「この行には合わない」（絵は良い・在庫に残す）場合はこちらを使わない。**
    その場合は ``set_cutout_selection(..., None)`` （行モーダルの「✕ この絵を外す」）だけでよい。
    ここは「指が6本ある」「破綻している」等、**どの行でも使えない絵**の逃がし道。
    `delete_entry` は trash_dir への退避（可逆）なので事故コストは低いが、控えめに置くこと。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")
    panel = next((p for p in manifest.get("panels", []) if p.get("line_id") == line_id), None)
    if panel is None:
        raise ValueError(f"line not found: {line_id}")
    slot_id, char_id = panel.get("cutout_slot_id"), panel.get("cutout_char_id")
    if not (slot_id and char_id):
        raise ValueError("この行には在庫割当がありません（外す絵がありません）")
    # 先に割当を外す（used_by を解放してから delete_entry を通す。順序を逆にすると
    # 「使用中なので削除できません」に自分自身の使用でブロックされる）
    set_cutout_selection(project_id, episode, line_id, None)
    deleted = panel_library_manager.delete_entry(char_id, slot_id)
    return {"line_id": line_id, "char_id": char_id, "slot_id": slot_id, "deleted": deleted}


def _confirm_split_panel(panel: dict, cur_text: str) -> bool:
    """分割の前半のパネルの同期記録を今のテキストへ焼き直す（絵も在庫割当も無ければ何もしない）。"""
    if panel.get("status") != "done" and not panel.get("cutout_slot_id"):
        return False
    panel["source_text"] = cur_text
    panel["source_text_hash"] = text_hash(cur_text)
    if (panel.get("prompt") or "").strip():
        panel["prompt_text_hash"] = text_hash(cur_text)
    return True


def confirm_split_sync(project_id: str, episode: int, line_id: str) -> dict | None:
    """行が分割された直後、前半(line_id)の同期記録を今のテキストへ焼き直す
    （`Docs/SUBLINE_PLAN.md` §5「分ける」・S2 §14）。

    分割は絵の内容を変える操作ではない（後半へ渡した分だけテキストが短くなるだけ）
    ので、`source_text_hash` を古いまま放置すると `_panel_sync` が stale（絵が古い）
    と誤判定する。ここは「絵を見て確定した」わけではないので `image_approved_*` は
    触らない（``fill_missing_images`` と同じ区別）。

    ⚠️ **呼び出し側が分割の直後に呼ぶこと。** scripting-agent 側の操作なので、
    ここでは分割そのものは検知できない。絵も在庫割当も無い行（missing）には
    追認する記録が無いので None を返す。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")
    panel = next((p for p in manifest.get("panels", []) if p.get("line_id") == line_id), None)
    if panel is None:
        raise ValueError(f"line not found: {line_id}")
    line = _script_lines_by_id(project_id, episode).get(line_id)
    if not _confirm_split_panel(panel, (line or {}).get("text", "")):
        return None
    save_manifest(project_id, episode, manifest)
    return panel


def _other_live_holder(manifest: dict, panel: dict, char_id: str, slot_id: str) -> list[dict]:
    """同じ在庫の絵（char_id/slot_id）を今も持っている、`panel` 以外の生きた行。"""
    return [q for q in manifest.get("panels", [])
            if q is not panel and not q.get("orphan")
            and q.get("cutout_slot_id") == slot_id
            and (q.get("cutout_char_id") or char_id) == char_id]


def _orphan_panel(project_id: str, episode: int, manifest: dict, panel: dict) -> bool:
    """パネルを孤立扱いにする（`manifest` を in-place で書き換える。保存は呼び出し側）。

    在庫の割当は**捨てずに** `orphaned_cutout` へ預ける（台本へ行が戻った時＝Undo に
    `_restore_panel_cutout` が生き返らせる。LINE_WORKBENCH_PLAN I5）。使用回数は戻す。
    同じ絵を共有する生きた兄弟行（束ねたカット）が居る間は、消費（times_used）は
    カットにつき1回なので戻さない（`set_cutout_selection` と同じ規則）。
    既に孤立のパネルは何もしない。
    """
    if panel.get("orphan"):
        return False
    slot_id, char_id = panel.get("cutout_slot_id"), panel.get("cutout_char_id")
    if slot_id and char_id:
        kw = {"count": False} if _other_live_holder(manifest, panel, char_id, slot_id) else {}
        panel_library_manager.release_usage(
            char_id, slot_id, project_id=project_id, episode=episode,
            line_id=panel.get("line_id"), **kw)
        panel["orphaned_cutout"] = {
            "cutout_slot_id": slot_id, "cutout_char_id": char_id,
            "cutout_source": panel.get("cutout_source"),
            "cutout_assigned_at": panel.get("cutout_assigned_at"),
        }
    panel["cutout_slot_id"] = None
    panel["cutout_char_id"] = None
    panel["cutout_source"] = None
    panel["cutout_assigned_at"] = None
    panel["orphan"] = True
    return True


def _restore_panel_cutout(project_id: str, episode: int, manifest: dict, panel: dict) -> str | None:
    """`orphaned_cutout` に預けた在庫割当を、台本へ戻った行に返す（`manifest` を in-place で更新）。

    戻せない時は割当を戻さず理由を返す（行は「絵が無い」状態＝在庫で埋め直せる）:
    - 在庫の絵が消えた・承認が外れた（在庫側の都合が変わった）
    - 外している間に、同じ絵が**別のカットの**行へ割り当たった（1話で同じ絵を二度使わない規則）
    戻せた時は使用回数を数え直す（外した時に戻した分）。同じカットの兄弟行が持っている間は
    消費を数えない（`_orphan_panel` と対称）。戻せたら None。
    """
    stash = panel.pop("orphaned_cutout", None)
    if not stash:
        return None
    slot_id, char_id = stash.get("cutout_slot_id"), stash.get("cutout_char_id")
    entry = panel_library_manager.get_entry(char_id, slot_id) if (slot_id and char_id) else None
    if (not entry or not panel_library_manager.usable_as(entry)["cutout"]
            or entry.get("review_status", "approved") != "approved"):
        return f"在庫の絵 {slot_id} が使えなくなっていたため、絵の割当は戻していません（在庫で埋め直せます）"

    holders = _other_live_holder(manifest, panel, char_id, slot_id)
    same_cut = False
    if holders:
        cut = cut_planner.cut_of_line(
            cut_report(project_id, episode, manifest)["cuts"]).get(panel.get("line_id"))
        cut_ids = set((cut or {}).get("line_ids", []))
        if all(h.get("line_id") in cut_ids for h in holders):
            same_cut = True
        else:
            return (f"外している間に同じ絵 {slot_id} が別の行へ割り当たっていたため、"
                    "絵の割当は戻していません（1話で同じ絵を二度使わない）")
    panel_library_manager.record_usage(
        char_id, slot_id, project_id=project_id, episode=episode,
        line_id=panel.get("line_id"), count=not same_cut)
    panel["cutout_slot_id"] = slot_id
    panel["cutout_char_id"] = char_id
    panel["cutout_source"] = stash.get("cutout_source")
    panel["cutout_assigned_at"] = stash.get("cutout_assigned_at")
    return None


def orphan_line(project_id: str, episode: int, line_id: str) -> dict | None:
    """台本から消えた行（削除・結合で吸収された行）の在庫の使用記録を戻す
    （`Docs/SUBLINE_PLAN.md` §5「削除」「結合」・S2 §14）。パネル自体は証拠として
    残す（``orphan=True``。``build_or_update_manifest`` と同じ方針）。

    ⚠️ **呼び出し側が削除・結合の直後に呼ぶこと。** ``build_or_update_manifest`` は
    次回のマニフェスト再構築で同じ行を自動で orphan 化するが、それまで
    times_used が解放されないままになる（生涯上限に嘘の消費で早く到達する）。
    既に orphan の行・パネルが無い行は何もせず None を返す。

    在庫の割当は `orphaned_cutout` に預ける（`_orphan_panel`）。行操作の窓口は
    台本全体の構造を見る `sync_structure` を使う（こちらは1行だけの後始末）。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")
    panel = next((p for p in manifest.get("panels", []) if p.get("line_id") == line_id), None)
    if panel is None or not _orphan_panel(project_id, episode, manifest, panel):
        return None
    save_manifest(project_id, episode, manifest)
    return panel


def _follows_speaker(panel: dict, speaker_id: str, speaker_map: dict) -> bool:
    """パネルの `characters` が、その話者の描くキャラ（単独）に従っているか。"""
    drawn = (speaker_map.get(speaker_id) or {}).get("image_char_id") or ""
    return (panel.get("characters") or []) == ([drawn] if drawn else [])


def _inherit_donor(panels: list[dict], panel: dict) -> dict | None:
    """サブ行のコマが引き継ぐ元＝同じグループ（`parent_line_id`）の、プロンプトを持つ別のコマ。

    分けた行の直前にあるものを優先し（s2 を s1 から分けたら s1）、無ければグループ内で最初のもの。
    グループに属さない行・グループにプロンプトを持つコマが無い行は None（ルールで埋める）。
    """
    parent = panel.get("parent_line_id")
    if not parent:
        return None
    mates = [q for q in panels
             if q is not panel and not q.get("orphan") and q.get("parent_line_id") == parent
             and (q.get("prompt") or "").strip()]
    if not mates:
        return None
    order = lambda q: q.get("order") or 0  # noqa: E731
    before = [q for q in mates if order(q) < order(panel)]
    return max(before, key=order) if before else min(mates, key=order)


def fill_slots_without_llm(manifest: dict, line_ids, lines_by_id: dict) -> dict:
    """プロンプトの無いコマを **LLM を呼ばずに** 埋める（`Docs/AROLL_EMOTION_LOCAL_PLAN.md` §5・E2/E3）。

    - **サブ行 → 親（同じグループ）のコマを引き継ぐ**（E2）: 英語の演出プロンプト・描くキャラ・slot を写す。
      寄り引き・向きは何もしない（在庫選定では `camera_plan` が話者の連続を単位に決める）。
      `prompt_text_hash` はサブ行自身の本文で焼く＝引き継いだプロンプトが「古い」扱いにならない。
    - **独立した新しい行 → ルールの slot**（E3）: 台本の感情＋記号から emotion を決める。
      プロンプトは作らない（新しく絵を生成する時に、無い行だけ作る＝E4）。

    触らないコマ: 孤立・プロンプトがある・絵が出来ている/在庫を割り当て済み・手で slot を直した
    （`slot_source=="user"` は slot を残す）。`manifest` をその場で書き換える（保存は呼び出し側）。
    戻り値: {"inherited": [line_id], "rule": [line_id]}
    """
    out = {"inherited": [], "rule": []}
    panels = [p for p in manifest.get("panels", []) if not p.get("orphan")]
    wanted = set(line_ids or [])
    for p in panels:
        lid = p.get("line_id")
        if lid not in wanted:
            continue
        if (p.get("prompt") or "").strip() or p.get("status") == "done" or p.get("cutout_slot_id"):
            continue
        line = lines_by_id.get(lid) or {}
        donor = _inherit_donor(panels, p)
        if donor is not None:
            p["prompt"] = donor["prompt"]
            p["prompt_source"] = "inherited"
            p["prompt_text_hash"] = text_hash(line.get("text") or p.get("text"))
            if donor.get("characters"):
                p["characters"] = list(donor["characters"])
            if p.get("slot_source") != "user" and donor.get("slot"):
                p["slot"] = dict(donor["slot"])
                p["slot_source"] = "inherited"
            elif p.get("slot") is None and p.get("slot_source") != "user":
                p["slot"], p["slot_source"] = slot_rules.rule_slot(line), "rule"
            p["slot_key"] = compute_slot_key(p.get("characters"), p.get("slot"))
            out["inherited"].append(lid)
        elif p.get("slot") is None and p.get("slot_source") != "user":
            p["slot"], p["slot_source"] = slot_rules.rule_slot(line), "rule"
            p["slot_key"] = compute_slot_key(p.get("characters"), p.get("slot"))
            out["rule"].append(lid)
    return out


def _panel_needs_prompt(panel: dict | None) -> bool:
    return panel is not None and not panel.get("orphan") and not (panel.get("prompt") or "").strip()


async def ensure_prompts(project_id: str, episode: int, line_ids: list[str],
                         model: str | None = None, log: list[str] | None = None) -> dict:
    """**新しく絵を生成する行のうち、プロンプトの無い行だけ** LLM で英語の演出プロンプトを作る
    （`Docs/AROLL_EMOTION_LOCAL_PLAN.md` §5 E4）。確定の下ごしらえは LLM を呼ばないので、
    独立した新しい行（プロンプト無し・ルールの slot だけ）は、生成の直前にここで作る。

    - 章ごとに問い合わせる（章の全セリフを文脈として渡す）が、**保存するのは `line_ids` の行だけ**
      （既存のプロンプト・手で直したプロンプトには触れない＝`overwrite=False`）
    - 拒否・失敗しても例外にしない。作れなかった行は `failed` に入れ、呼び出し側が生成をスキップする
    - 有料の最終フォールバック（`LLM_FALLBACK_ALLOW_PAID`）と「どのモデルで通ったか」は
      `generate_section_prompts` が担う。`models` に章ごとの結果を返す
    戻り値: {"generated": [line_id], "failed": [{"section","refused","error"}], "models": {章: モデル}, "warnings": []}
    """
    out: dict = {"generated": [], "failed": [], "models": {}, "warnings": []}
    manifest = load_manifest(project_id, episode)
    script = project_manager.get_episode_script(project_id, episode)
    if manifest is None or script is None:
        return out
    panels = {p.get("line_id"): p for p in manifest.get("panels", [])}
    need = [lid for lid in dict.fromkeys(line_ids) if _panel_needs_prompt(panels.get(lid))]
    if not need:
        return out

    by_section: dict[str, list[dict]] = {}
    for ln in script.get("lines", []):
        if (ln.get("text") or "").strip():
            by_section.setdefault(ln.get("section") or "main", []).append(ln)
    need_set = set(need)
    sections = [s for s, ls in by_section.items() if any(l.get("id") in need_set for l in ls)]

    speaker_map, known_chars = get_speaker_map(project_id), get_cast_characters(project_id)
    out["warnings"] += list(cast_warnings(project_id))
    sem = asyncio.Semaphore(aroll_prompt_generator.SECTION_CONCURRENCY)

    async def one(section: str):
        async with sem:
            try:
                return section, await aroll_prompt_generator.generate_section_prompts(
                    section, by_section[section], speaker_map, known_chars, model=model), None
            except Exception as e:  # noqa: BLE001
                return section, None, e

    prompts_by_line: dict[str, dict] = {}
    for section, res, err in await asyncio.gather(*(one(s) for s in sections)):
        if err is not None:
            refused = isinstance(err, aroll_prompt_generator.PromptRefused)
            project_manager.append_error(project_id, f"aroll prompt generation failed ({section}): {err}")
            out["failed"].append({"section": section, "refused": refused, "error": str(err)[:300]})
            out["warnings"].append(f"❌ 章 '{section}': {str(err)[:300]}")
            continue
        result, warns = res
        out["warnings"] += warns
        used = next((v.get("model") for v in result.values() if v.get("model")), None)
        if used:
            out["models"][section] = used
        # 保存するのは、プロンプトが無かった行だけ（同じ章の他の行は触らない）
        prompts_by_line.update({lid: v for lid, v in result.items() if lid in need_set})

    if prompts_by_line:
        build_or_update_manifest(project_id, episode, script, prompts_by_line,
                                 aspect=manifest.get("aspect") or "16:9",
                                 style=manifest.get("style") or "kamishibai", overwrite=False)
        out["generated"] = [lid for lid in need if lid in prompts_by_line]
    if log is not None:
        for section, used in out["models"].items():
            log.append(f"✍ 章 '{section}' の演出プロンプトを {used} で作りました")
        for f in out["failed"]:
            log.append(f"❌ 章 '{f['section']}' のプロンプトを作れませんでした: {f['error']}")
    return out


def sync_structure(project_id: str, episode: int,
                   split_front_line_ids: list[str] | None = None,
                   fill_line_ids: list[str] | None = None) -> dict:
    """台本の行構造へコマ一覧を合わせる（director の行操作の窓口・LINE_WORKBENCH_PLAN §3-3・W1）。

    **LLMも画像生成も呼ばない。** 冪等（台本が変わっていなければ何も変わらない）。
    aroll.json が無い話数（Aロール未着手）は何もしない。

    1. 台本から外れた行 → 孤立扱い（在庫の使用回数を戻す・絵のPNGは残す・割当は預ける）
    2. 台本にあってコマが無い行 → プロンプト無しのコマを追加（`build_or_update_manifest`）。
       order・section・parent_line_id・text の追随もここで効く。台本へ戻った行は孤立を外す
    3. 戻ってきた行 → 預けた在庫の割当と使用回数を戻す（Undo）
    4. 話者を付け替えた行 → 絵を「古い」にする（`speaker_changed` の印）＋描くキャラを新しい話者へ
    5. `split_front_line_ids`（分割の前半） → 同期記録を今のテキストへ焼き直す（絵が古くならない）
    6. プロンプトの無いコマを LLM なしで埋める（`fill_slots_without_llm`）: 今回足したコマ＋`fill_line_ids`
       （director の確定が、確定した行を渡す）。サブ行は親から引き継ぎ、独立した新しい行はルールの slot
    """
    empty = {"added": [], "orphaned": [], "restored": [], "speaker_changed": [],
             "split_confirmed": [], "filled": {"inherited": [], "rule": []}, "warnings": []}
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        return {"has_manifest": False, "skipped": "aroll.json not found", **empty}
    script = project_manager.get_episode_script(project_id, episode)
    if script is None:
        return {"has_manifest": True, "skipped": "script.json not found", **empty}

    lines_by_id = _script_lines_by_id(project_id, episode, script)
    speaker_map = get_speaker_map(project_id)
    warnings: list[str] = []

    # 話者の付け替えは、再構築が speaker_id を上書きする前に検出する
    speaker_moves: dict[str, str] = {}
    for p in manifest.get("panels", []):
        lid = p.get("line_id")
        if p.get("orphan") or lid not in lines_by_id:
            continue
        old, new = p.get("speaker_id") or "", lines_by_id[lid].get("speaker_id") or ""
        if old and new and old != new:
            speaker_moves[lid] = old

    had_orphan = {p.get("line_id") for p in manifest.get("panels", []) if p.get("orphan")}
    had_panel = {p.get("line_id") for p in manifest.get("panels", [])}

    orphaned = []
    for p in manifest.get("panels", []):
        if p.get("line_id") in lines_by_id:
            continue
        if _orphan_panel(project_id, episode, manifest, p):
            orphaned.append(p.get("line_id"))
    if orphaned:
        save_manifest(project_id, episode, manifest)

    manifest = build_or_update_manifest(project_id, episode, script, {}, overwrite=False)
    panels_by_id = {p.get("line_id"): p for p in manifest["panels"] if not p.get("orphan")}
    added = [lid for lid in lines_by_id if lid not in had_panel]

    restored = [lid for lid in lines_by_id if lid in had_orphan and lid in panels_by_id]
    for lid, p in panels_by_id.items():
        if p.get("orphaned_cutout"):
            why = _restore_panel_cutout(project_id, episode, manifest, p)
            if why:
                warnings.append(f"{lid}: {why}")

    speaker_changed = []
    for lid, old in speaker_moves.items():
        p = panels_by_id.get(lid)
        if p is None:
            continue
        new = lines_by_id[lid].get("speaker_id") or ""
        marker = p.get("speaker_changed")
        # 描くキャラは、話者に従っていた行だけ新しい話者へ付け替える（手で選んだキャラは尊重）
        if _follows_speaker(p, old, speaker_map):
            drawn = (speaker_map.get(new) or {}).get("image_char_id") or ""
            p["characters"] = [drawn] if drawn else []
            p["slot_key"] = compute_slot_key(p["characters"], p.get("slot"))
        if marker and marker.get("from") == new:
            p.pop("speaker_changed", None)     # 元の話者へ戻った（Undo）＝絵はまた正しい
        elif p.get("cutout_slot_id") or (p.get("status") == "done" and p.get("image")):
            p["speaker_changed"] = {"from": (marker or {}).get("from") or old,
                                    "picture": picture_key(p), "at": _now()}
            speaker_changed.append(lid)

    split_confirmed = []
    for lid in split_front_line_ids or []:
        p = panels_by_id.get(lid)
        if p is not None and _confirm_split_panel(p, lines_by_id.get(lid, {}).get("text", "")):
            split_confirmed.append(lid)

    # 分割・サブ行追加・挿入でできた（プロンプトの無い）コマを、LLM を呼ばずに埋める
    filled = fill_slots_without_llm(manifest, [*added, *(fill_line_ids or [])], lines_by_id)

    save_manifest(project_id, episode, manifest)
    return {"has_manifest": True, "added": added, "orphaned": orphaned, "restored": restored,
            "speaker_changed": speaker_changed, "split_confirmed": split_confirmed,
            "filled": filled, "warnings": warnings}


def apply_cutout_plan(project_id: str, episode: int, line_ids: list[str] | None = None) -> dict:
    """試算（cutout_plan）の結果を実際に書き込む。**在庫で賄える行だけ**。

    賄えない行は触らない ── そこは新規生成の担当で、無理に在庫から埋めると
    ワンパターンの発生源になる（在庫から選ばないこと自体が設計）。

    ⚠️ **空リストは「対象ゼロ」**（省略＝None が「全行」）。falsy 判定にすると、
    1行も選んでいないのに在庫で賄える全行へ適用してしまう
    （``CHARACTER_CUTOUT_PLAN.md`` §13-4 と同じ規則。approve_images/auto_assign_backgrounds と揃える）。

    ⚠️ **行を明示しない時は、絵が既に決まっているカットに触らない**（``decided``。2026-09-24）。
    ``set_cutout_selection`` はカット全体を無条件に上書きするので、ここを飛ばさないと
    「在庫N行を割り当てる」を押すたびに確定済みのカットまで選び直され、確定も外れる。
    行を明示した時は従来どおり（呼び出し側がその行を選び直すと決めている）。
    """
    targets = None if line_ids is None else set(line_ids)
    plan = cutout_plan(project_id, episode, reselect_line_ids=targets)
    applied, skipped, kept, cuts_done = [], 0, 0, 0
    for line in plan["lines"]:
        if targets is None and line["decided"]:
            kept += 1
            continue
        if not line["slot_id"] or (targets is not None and line["line_id"] not in targets):
            skipped += 1
            continue
        # ⚠️ **カットの先頭行でだけ呼ぶ。** set_cutout_selection はカット全体へ当てるので、
        # 兄弟行でも呼ぶと同じ仕事を人数分繰り返すことになる（消費の数え方も狂いやすい）。
        # 先頭行が line_ids で選ばれていない場合に備え、既に当たっていれば飛ばす。
        if not line["cut_head"]:
            applied.append(line["line_id"])
            continue
        res = set_cutout_selection(project_id, episode, line["line_id"], line["slot_id"],
                                   source="plan")
        cuts_done += 1
        applied.extend(res.get("line_ids") or [line["line_id"]])
    applied = list(dict.fromkeys(applied))   # カット共有で重複するので畳む
    return {"applied": len(applied), "applied_cuts": cuts_done,
            "skipped": skipped, "kept_decided": kept, "line_ids": applied}


def fill_missing_images(project_id: str, episode: int, line_ids: list[str]) -> dict:
    """選択行のうち絵が無いものを、無料の手段だけで埋める（Step C・2026-09-24）。

    ① 同じカットに**既に画像を持つ**メンバーがいれば、その絵を無料でコピーする
       （``_propagate_cut_result``。台本に新しい行が既存の確定済みカットへ合流した時の経路。
       §18-3で指摘した「生成経路はカット全体を巻き込むが在庫割当は巻き込まない」非対称を、
       生成に回す前にここで解消する）。
    ② それでも埋まらない行は ``apply_cutout_plan`` で在庫を試す（未決定の行だけを渡すので、
       C0で入れた「decided なカットは触らない」判定とは無関係に動く＝行を明示した通常の適用）。
    ③ それでも埋まらない行は生成が要る。**カットの先頭行だけ**を返す（1カット1枚の原則。
       課金はここでは発生しない・呼び出し側が確認の上で ``/aroll/generate`` に渡す）。

    ⚠️ **人が絵を見て確定したわけではないので `image_approved_at` は立てない**
    （承認は別操作・呼び出し側の判断。合意 2026-09-24）。
    ⚠️ カット内に ``cutout_slot_id`` はあるが実画像が無い決定形（在庫割当のみ済み・
    Photoshop合成前）のメンバーしかいない場合はコピー元にできないため、そのカットには
    触れず ``select_targets`` 側の既存動作に委ねる（安全側・中途半端な複製をしない）。
    """
    manifest = load_manifest(project_id, episode)
    if manifest is None:
        raise ValueError("aroll.json not found")
    panels_by_id = {p.get("line_id"): p for p in manifest.get("panels", []) if not p.get("orphan")}
    cuts = cut_report(project_id, episode, manifest)["cuts"]
    cut_of = cut_planner.cut_of_line(cuts)

    handled_cuts: set[str] = set()
    filled_by_copy: list[str] = []
    still_undecided: list[str] = []
    for lid in line_ids:
        p = panels_by_id.get(lid)
        if p is None or _panel_decided(p):
            continue
        cut = cut_of.get(lid) or {"cut_id": lid, "line_ids": [lid]}
        if cut["cut_id"] in handled_cuts:
            continue
        handled_cuts.add(cut["cut_id"])
        members = [panels_by_id[l] for l in cut.get("line_ids", [lid]) if l in panels_by_id]
        undecided_ids = [m["line_id"] for m in members if not _panel_decided(m)]
        if not undecided_ids:
            continue
        donor = next((m for m in members if m.get("status") == "done" and m.get("image")), None)
        if donor is not None:
            _propagate_cut_result(project_id, episode, manifest, donor, undecided_ids)
            filled_by_copy.extend(undecided_ids)
        elif any(_panel_decided(m) for m in members):
            continue   # cutout_slot_idのみの決定形はコピー元にできない。触らない
        else:
            still_undecided.extend(undecided_ids)
    if filled_by_copy:
        save_manifest(project_id, episode, manifest)

    # ② 在庫（未決定の行だけを明示して渡す＝行を明示した通常の適用と同じ経路）
    stock = apply_cutout_plan(project_id, episode, list(dict.fromkeys(still_undecided))) \
        if still_undecided else {"line_ids": []}
    filled = list(dict.fromkeys(filled_by_copy + stock["line_ids"]))
    still_open = [lid for lid in still_undecided if lid not in set(stock["line_ids"])]

    # ③ 残りは生成が要る（カットの先頭行だけ・ナレーション/プロンプト未生成は対象外）
    manifest = load_manifest(project_id, episode)   # ②の保存後を読み直す
    panels_by_id = {p.get("line_id"): p for p in manifest.get("panels", []) if not p.get("orphan")}
    need_generation: list[str] = []
    seen_cuts: set[str] = set()
    for lid in still_open:
        cut = cut_of.get(lid) or {"cut_id": lid, "line_ids": [lid]}
        if cut["cut_id"] in seen_cuts:
            continue
        seen_cuts.add(cut["cut_id"])
        head = panels_by_id.get(cut["line_ids"][0]) or panels_by_id.get(lid)
        if head and head.get("characters") and (head.get("prompt") or "").strip():
            need_generation.append(head["line_id"])

    return {"filled": filled, "need_generation": need_generation}
