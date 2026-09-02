"""
キャラ所有ライブラリ（shared/characters/{char_id}/panel_library/）の索引管理と生成。

Aロールの演技スロット（emotion/shot/angle。panel_presets.pyの語彙・aroll_manager.pyの
slot_key照合ロジックと同じ枠組み）ごとに作り置きした画像を保持する。台本行の画像生成時、
ここに一致するスロットがあれば無料でコピーし、無ければ従来どおりNanoBananaで新規生成する
（Phase 3の「解決順序の追加」。既存のAロール生成は置き換えない。Docs/AROLL_ASSET_PLAN.md）。

保管レイアウト（背景アーカイブと同型。Docs/BACKGROUND_ARCHIVE.md §3）:
  shared/characters/{char_id}/panel_library/
    library.json            ← 索引（本ファイルが読み書きする対象）
    images/{slot_id}.png    ← 実体

★appearance_version（キャラ外見の版）を必ずキーに含める。忘れるとキャラの外見を更新した
瞬間に、古いライブラリ画像と新規生成が同一話に混在して衣装が途中で変わる
（Docs/AROLL_ASSET_PLAN.md §6）。世代違いは黙って使わず「古い」ものとして除外する
（find_currentはappearance_versionが一致する最新世代のみを返す）。

★ハッシュ方式はブリーフ（AROLL_SLOT_REUSE_BRIEF.md §5）が示唆した「mtime」ではなく
「appearance_prompt + reference/内ファイルのバイト内容」を使う。mtimeはファイルコピーや
git checkoutで中身が同じでも変わってしまい、実質無変更なのにライブラリ全体が誤って
「古い」判定されるおそれがあるため、内容ハッシュの方が安定する。
"""
import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from app.core import (
    character_manager, cutout_engine, fingerprint, nanobanana_client, panel_presets,
    style_manager,
)

SCHEMA_VERSION = "1.3.0"  # 1.3.0: mask を追加（analyze_alpha実測・psassistの採寸本籍。追加のみ・後方互換）
#                          1.2.0: rebless_log / diversity を追加
#                          1.1.0: kind="cutout" / cutout / measured / fingerprint を追加

BACKGROUND_FRAGMENT = panel_presets.BACKGROUND_MODES["flat"]  # 本籍は panel_presets
PROMPT_SUFFIX = "No text, no letters, no speech bubbles, no watermark in the image."


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def library_dir(char_id: str) -> Path:
    return character_manager.char_dir(char_id) / "panel_library"


def images_dir(char_id: str) -> Path:
    return library_dir(char_id) / "images"


def index_file(char_id: str) -> Path:
    return library_dir(char_id) / "library.json"


def appearance_version(char_id: str) -> str:
    """appearance_prompt + reference/内ファイルの内容ハッシュ（先頭12桁）。

    キャラの外見が変わった瞬間にこの値も変わる＝ライブラリの世代判定キー。
    """
    c = character_manager.read_character(char_id) or {}
    h = hashlib.sha256()
    h.update((c.get("appearance_prompt") or "").strip().encode("utf-8"))
    ref_dir = character_manager.char_dir(char_id) / "reference"
    if ref_dir.exists():
        for p in sorted(ref_dir.glob("*")):
            if p.is_file():
                h.update(p.name.encode("utf-8"))
                h.update(p.read_bytes())
    return h.hexdigest()[:12]


def load_index(char_id: str) -> dict:
    f = index_file(char_id)
    if not f.exists():
        return {"schema_version": SCHEMA_VERSION, "char_id": char_id, "updated_at": _now(), "entries": []}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        data.setdefault("entries", [])
        return data
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "char_id": char_id, "updated_at": _now(), "entries": []}


def save_index(char_id: str, data: dict) -> None:
    library_dir(char_id).mkdir(parents=True, exist_ok=True)
    images_dir(char_id).mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now()
    index_file(char_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def rebless(char_id: str, *, dry_run: bool = True, note: str = "") -> dict:
    """世代違い(is_stale)の entry を現世代へ付け替える。

    ⚠️ これは「**見た目は変わっていない**」という人の宣言を記録する操作であって、
    機械が判定した結果ではない。``appearance_version`` は appearance_prompt と
    ``reference/`` の全ファイルのバイト列のハッシュなので、**参照画像を1枚足しただけでも
    値が変わり、そのキャラの在庫が一斉に無効化される**（実測 2026-08-27: ルカの
    切り抜き92枚が全滅し、1話あたりの新規生成が 24枚 → 106枚に増えていた）。
    参照画像の追加は生成精度を上げるための日常的で非破壊的な操作なのに、
    資産を全部捨てる副作用がある ── その落差を人の宣言で埋めるための口。

    ⚠️ **デザインを実際に変えた後に実行してはいけない。** 古い外見の絵が現世代の在庫と
    して配られるようになる。判断できるのは人だけなので、UIは必ず確認を挟むこと。

    付け替えの前に索引をバックアップし、``rebless_log`` に事実を残す（誰がいつ何件を
    どの世代から付け替えたかが後から追えるように）。
    """
    cur = appearance_version(char_id)
    idx = load_index(char_id)
    stale = [e for e in idx.get("entries", []) if e.get("appearance_version") != cur]
    by_kind: dict[str, int] = {}
    by_from: dict[str, int] = {}
    for e in stale:
        by_kind[e.get("kind", "panel")] = by_kind.get(e.get("kind", "panel"), 0) + 1
        k = str(e.get("appearance_version"))
        by_from[k] = by_from.get(k, 0) + 1
    result = {"char_id": char_id, "to": cur, "count": len(stale),
              "by_kind": by_kind, "from": by_from, "dry_run": dry_run, "backup": None}
    if dry_run or not stale:
        return result

    src = index_file(char_id)
    if src.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = src.with_name(f"{src.name}.bak_{stamp}_rebless前")
        backup.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        result["backup"] = backup.name
    for e in stale:
        e["appearance_version"] = cur
    idx.setdefault("rebless_log", []).append({
        "at": _now(), "to": cur, "from": by_from, "count": len(stale),
        "by_kind": by_kind, "note": note,
    })
    save_index(char_id, idx)
    return result


def _same_source(src: dict | None, project_id: str, episode: int, line_id: str) -> bool:
    """取り込み元の行が一致するか。

    ⚠️ **3つ揃えて照合する。** `line_id` は話数ごとに 001 から振り直されるので、
    `line_id` だけで突き合わせると**別プロジェクトの同じ行番号と衝突する**
    （CHARACTER_CUTOUT_PLAN.md §11-6 で一度踏んだ穴と同じ）。
    """
    if not isinstance(src, dict):
        return False
    return (src.get("project_id") == project_id
            and int(src.get("episode") or 0) == int(episode)
            and src.get("line_id") == line_id)


def demote_from_line(char_ids: list[str], project_id: str, episode: int,
                     line_id: str) -> list[str]:
    """その行から取り込んだ在庫を ``pending`` へ降格する（**削除はしない**）。

    行を作り直す動機の大半は「絵が気に入らない」なので、その絵を在庫に残したままだと
    **後の話数で自動的に配布される**。降格すれば自動消費（``find_current`` /
    ``cutout_selector.candidates``）から外れる。

    ⚠️ 消さないのは、アーカイブが**話数を越えた共有資産**だから。台本が変わったから
    作り直した場合、前の絵は資産としては無傷。良い絵なら切り抜きタブで承認し直せば戻る。
    機械は「自動配布を止める」ところまでにして、可否の判断は人に残す。
    """
    touched = []
    for char_id in [c for c in char_ids if c]:
        idx = load_index(char_id)
        hit = [e for e in idx.get("entries", [])
               if _same_source(e.get("source"), project_id, episode, line_id)
               and e.get("review_status", "approved") == "approved"]
        if not hit:
            continue
        for e in hit:
            e["review_status"] = "pending"
            e["note"] = (e.get("note") or "") + \
                f"[{_now()}] {line_id} を作り直したため自動降格"
            touched.append(f"{char_id}/{e['slot_id']}")
        save_index(char_id, idx)
    return touched


def usable_as(entry: dict) -> dict:
    """その entry が**何に使えるか**。⚠️ `kind` ではなく実際に持っているもので判定する。

    `kind` は「何であるか」ではなく「**どこから来たか**」の記録でしかない
    （psassist が取り込んだ194枚は `kind="cutout"`、コンテナが作るものは既定の `"panel"`）。
    2026-08-27 に背景除去が入って以降の entry は**1枚の絵から `image` と `cutout` を両方持つ**ので、
    `kind` では用途を答えられなくなった ── panel と cutout は「別の在庫」ではなく、
    同じ絵の「背景付きの姿」と「切り抜いた姿」。詳細は
    `psassist/Docs/CHARACTER_CUTOUT_PLAN.md` §14-0。

    ここが「使えるか」の唯一の定義。`find_current`（完成コマを配る）と
    `cutout_selector.candidates`（合成素材を配る）の両方がこれを使う＝二重定義で食い違わせない。
    """
    return {
        # 背景付きの完成コマ。そのまま1コマとして配れる
        "panel": bool(entry.get("image")),
        # 背景を抜いた合成素材。指紋が無いと「近すぎないか」を測れず選べないので両方を条件にする
        "cutout": bool(entry.get("cutout") and (entry.get("fingerprint") or {}).get("dhash")),
    }


def list_entries(char_id: str, *, emotion: str = "", shot: str = "", angle: str = "",
                 kind: str = "panel") -> list[dict]:
    """索引一覧を返す。各entryに is_stale / review_status / **usable** を正規化して付与する。

    kind: 既定は "panel"＝そのまま1コマになる画像だけ。"cutout"（背景を抜いた合成素材・
    2026-08-25に196枚を取り込み）は `image` を持たないため、既定の一覧に混ぜると
    UIのサムネイルが全滅する。空文字を渡すと全種別を返す。

    ⚠️ **UIは `kind` ではなく `usable` を見ること**（`usable_as` 参照）。`kind` は出自の記録で、
    2026-08-27以降の entry は `image` と `cutout` を両方持つ＝どちらの用途にも使える。
    統合在庫タブは `kind=""` で全種別を取り、`usable` で見せ分ける。
    """
    current = appearance_version(char_id)
    out = []
    for e in load_index(char_id).get("entries", []):
        if kind and e.get("kind", "panel") != kind:
            continue
        if emotion and e.get("emotion") != emotion:
            continue
        if shot and e.get("shot") != shot:
            continue
        if angle and e.get("angle") != angle:
            continue
        out.append({
            **e,
            "is_stale": e.get("appearance_version") != current,
            "review_status": e.get("review_status", "approved"),
            "usable": usable_as(e),
        })
    return out


def find_current(char_id: str, emotion: str, shot: str, angle: str) -> dict | None:
    """slot(emotion/shot/angle)に一致し、appearance_versionが今と同じ・かつ承認済みのentryのうち
    「最も使われていない」1件を返す（ローテーション。2026-08-21）。

    世代違い・review_status="pending"（未承認）は対象外（Noneを返す＝呼び出し側は通常どおり
    新規生成にフォールバックする）。review_status欠落は既存資産の後方互換として承認済み扱い。

    候補が複数ある場合は`times_used`（record_usageで実消費のたびに+1される）が最小のものを選ぶ。
    タイブレークはslot_id昇順（決定的にするため）。新規追加したバリアントはtimes_used=0から
    始まるため自然に優先消費され、シリーズを通算して均等にローテーションする（1話内のdedupに
    閉じない。Docs/AROLL_ASSET_PLAN.md §16参照）。

    ⚠️ この関数自体は状態を変更しない（ドライラン安全）。実際に選んだentryを消費したら、
    呼び出し側が必ず record_usage() を呼ぶこと（generate_line_imageのみが呼ぶ想定。
    generation_plan_estimateのようなドライランは呼んではいけない）。
    """
    current = appearance_version(char_id)
    candidates = [
        e for e in load_index(char_id).get("entries", [])
        # ⚠️ 背景を抜いただけの合成素材（image を持たない）は配らない。そのまま渡すと
        #    背景の無いコマになる。切り抜きの選択は指紋ベースの別系統で行う
        #    （CHARACTER_CUTOUT_PLAN.md §10）。
        #    判定は kind ではなく **image を持つか**（kind は出自の記録であって用途ではない。
        #    2026-08-27以降の entry は image と cutout を両方持つ。usable_as 参照）
        if usable_as(e)["panel"]
        and e.get("emotion") == emotion and e.get("shot") == shot and e.get("angle") == angle
        and e.get("appearance_version") == current
        and e.get("review_status", "approved") == "approved"
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda e: (e.get("times_used", 0), e.get("slot_id", "")))
    return candidates[0]


def record_usage(char_id: str, slot_id: str) -> None:
    """ライブラリentryの使用回数(times_used)を+1する。find_currentのローテーションが参照する。

    実際に画像を消費した時だけ呼ぶ（ドライランでは呼ばない）。
    """
    data = load_index(char_id)
    for e in data.get("entries", []):
        if e.get("slot_id") == slot_id:
            e["times_used"] = e.get("times_used", 0) + 1
            save_index(char_id, data)
            return


def release_usage(char_id: str, slot_id: str) -> None:
    """times_used を-1する（0未満にはしない）。選び直しで前の1枚を解放する時に使う。

    record_usage と対で使わないと、ピッカーで選び直すたびに前の絵の使用回数が
    増えっぱなしになり、生涯上限に嘘の消費で早く到達する。
    """
    data = load_index(char_id)
    for e in data.get("entries", []):
        if e.get("slot_id") == slot_id:
            e["times_used"] = max(0, e.get("times_used", 0) - 1)
            save_index(char_id, data)
            return


def approve_all(char_id: str, kind: str = "cutout") -> int:
    """指定 kind の pending をまとめて承認し、件数を返す。

    取り込み直後は全件 pending（安全弁）で、194件を1枚ずつ承認するのは現実的でない。
    却下は既存の DELETE（1件ずつ）で行う＝**まとめて入れて、要らないものを落とす**運用。
    """
    data = load_index(char_id)
    n = 0
    for e in data.get("entries", []):
        if e.get("kind", "panel") == kind and e.get("review_status") == "pending":
            e["review_status"] = "approved"
            n += 1
    if n:
        save_index(char_id, data)
    return n


def get_entry(char_id: str, slot_id: str) -> dict | None:
    """slot_idで1件直接取得する（ユーザーがUIから特定のバリアントを明示選択する時に使う）。"""
    for e in load_index(char_id).get("entries", []):
        if e.get("slot_id") == slot_id:
            return e
    return None


def approve_entry(char_id: str, slot_id: str) -> dict | None:
    """pending状態のentryを承認する（review_status="approved"）。以後find_currentの対象になる。"""
    data = load_index(char_id)
    for e in data.get("entries", []):
        if e.get("slot_id") == slot_id:
            e["review_status"] = "approved"
            save_index(char_id, data)
            return e
    return None


def update_entry(char_id: str, slot_id: str, *,
                 emotion: str | None = None, shot: str | None = None,
                 angle: str | None = None, pose: str | None = None,
                 note: str | None = None) -> dict | None:
    """entryのラベル（演技スロット）を人が直す。Noneを渡した軸は触らない。

    LLMが付けたラベルは実測で emotion 51%（似た感情の群で見れば76%）しか当たらない。
    分類器は作らないと決めた以上、個別の誤りは人がここで直すしかない
    （memory: emotion-label-accuracy-measured）。

    ⚠️ **slot_idは書き換えない。** slot_idは実体ファイル名（images/・cutouts/）であり、
    aroll.jsonの cutout_slot_id / library_slot_id が指す先でもある。ラベルを直すたびに
    改名すると話数をまたいだ参照が全部切れる。slot_idは「作られた時の分類が残った
    識別子」であって現在のラベルではない ── UIでもそう見せること。

    直した軸があれば ``label_source="user"`` を刻む。以後その1枚は「機械のラベルが
    どれだけ当たるか」を測る母集団から外せる（人が答えを入れた以上、必ず一致するので
    混ぜると精度を水増しする）。

    空文字は「未設定に戻す」＝許可する。emotionが空の在庫は find_current に引かれない
    （感情未指定の行は自動割当を拒否する、という安全弁と同じ側に倒れるだけ）。
    """
    vocab = panel_presets.load_presets()
    axes = {"emotion": emotion, "shot": shot, "angle": angle, "pose": pose}
    for axis, val in axes.items():
        if not val:
            continue
        if val not in {i.get("id") for i in vocab.get(axis, [])}:
            raise ValueError(f"{axis} に無い値です: {val}（panel_presets の語彙から選んでください）")

    data = load_index(char_id)
    for e in data.get("entries", []):
        if e.get("slot_id") != slot_id:
            continue
        changed = []
        for axis, val in axes.items():
            if val is None:
                continue
            # poseだけ「未設定=None」で持つ（register_from_imageの pose or None と同じ表現）
            new = (val or None) if axis == "pose" else val
            if e.get(axis) != new:
                e[axis] = new
                changed.append(axis)
        if note is not None and e.get("note") != note:
            e["note"] = note
            changed.append("note")
        if changed:
            if [c for c in changed if c != "note"]:
                e["label_source"] = "user"
            e["edited_at"] = _now()
            save_index(char_id, data)
        return {**e,
                "is_stale": e.get("appearance_version") != appearance_version(char_id),
                "review_status": e.get("review_status", "approved"),
                "changed": changed}
    return None


def _next_slot_id(emotion: str, shot: str, angle: str, existing: set[str]) -> str:
    base = f"{emotion}_{shot}_{angle}"
    n = 1
    while True:
        sid = f"{base}_{n:03d}"
        if sid not in existing:
            return sid
        n += 1


def trash_dir(char_id: str) -> Path:
    return library_dir(char_id) / "trash"


def delete_entry(char_id: str, slot_id: str) -> bool:
    """索引から除去し、実体ファイルを **trash/ へ退避**する。存在しなければFalse。

    ⚠️ **2026-08-29: 完全削除をやめて、取り消せる形にした。** 多様性検査を撤去して
    「近すぎる絵は人が見て削除する」運用に寄せた以上、削除が日常操作になる。
    日常操作が取り消せないのは危ない（`rebless` には索引のバックアップがあるのに、
    削除だけ無かった＝非対称だった）。

    trash/ は機械が読まない（`load_index` は索引しか見ないので、退避した絵が
    自動配布に復活することはない）。戻したい時は人がファイルを戻して再取り込みする。
    容量が気になったら trash/ を手で空にすればよい。
    """
    data = load_index(char_id)
    entries = data.get("entries", [])
    target = next((e for e in entries if e.get("slot_id") == slot_id), None)
    if not target:
        return False
    data["entries"] = [e for e in entries if e.get("slot_id") != slot_id]
    save_index(char_id, data)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = trash_dir(char_id)
    # 索引の断片も一緒に残す（どのラベル・どの由来の絵だったかが分からないと戻せない）
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{stamp}_{slot_id}.json").write_text(
        json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8")
    # kind="cutout" は image を持たず cutout だけを持つ。Noneをパス結合に渡すと落ちるので両方見る。
    for key, root in (("image", images_dir(char_id)), ("cutout", library_dir(char_id) / "cutouts")):
        rel = target.get(key)
        if not rel:
            continue
        f = (library_dir(char_id) / rel).resolve()
        if f.is_relative_to(root.resolve()) and f.is_file():
            try:
                f.replace(dest / f"{stamp}_{key}_{f.name}")
            except OSError:
                pass
    return True


def _resolve_refs(char_id: str) -> list[tuple[bytes, str, str]]:
    """aroll_manager._resolve_refsと同じ方式（単独キャラなので最大2枚）。"""
    c = character_manager.read_character(char_id)
    name = (c or {}).get("name") or char_id
    ref_dir = character_manager.char_dir(char_id) / "reference"
    refs: list[tuple[bytes, str, str]] = []
    if not ref_dir.exists():
        return refs
    files = sorted(
        (p for p in ref_dir.glob("*") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:2]
    for p in files:
        label = f"{name}: keep this character consistent (same face, hairstyle, outfit)"
        refs.append((p.read_bytes(), nanobanana_client.mime_for(p.name), label))
    return refs


MAX_ATTEMPTS = 3   # 初回＋再試行2回。落ちるたびに課金されるので上限を持つ


async def _generate_and_measure(
    char_id: str, prompt: str, refs: list, model: str,
) -> tuple[bytes, bytes | None, dict | None, dict | None, str]:
    """生成 → 背景除去 → 指紋。**弾かない・作り直さない。**

    ⚠️ **2026-08-29: 受け入れ検査（既存と近すぎたら作り直す）を撤去した。**
    在庫は「似ているが少し違う絵」に価値がある（CHARACTER_CUTOUT_PLAN.md §6）のに、
    検査はそれを捨てていた。実測（本番194枚を1枚ずつ検査に通す再現）:

    - **29枚（15%）が却下**される
    - うち **26枚（90%）は別スロット**に対する却下 ── 指紋は意図的に寸法を捨てる
      （`cutout_selector.distance` 参照）ので、バストアップが顔アップと「似ている」と
      判定される。合成素材としては別物なのに落ちる
    - うち **25枚は実際に本編で使われている**

    つまり検査を通していたら、現に使っている絵の15%が存在しなかった。今の在庫が
    成立しているのは `import_cutouts.py` が検査を通さず一括投入したからでしかない。

    指紋距離は**選択**（隣の行と似ていないか＝`cutout_selector.candidates`）には効くが、
    **保存**の可否には使えない。物差しの目的が違う。近すぎる絵は人が見て削除すればよい
    （`nearest_in_stock` で「似ている在庫」を提示する）。

    背景除去と指紋は**常に行う**（撤去前は check=False が「切り抜きも作らない」を
    兼ねていて、バリアント一括生成が切り抜きを持てなかった。ここを分離した）。
    """
    data = await nanobanana_client.generate_one(
        prompt, refs, aspect="16:9", allow_fallback=False, model=(model.strip() or None),
    )
    rgba, info = cutout_engine.cut_out(Image.open(io.BytesIO(data)))
    fp = fingerprint.for_entry(rgba)
    if not fp.get("dhash"):
        # アルファが空＝切り抜きに失敗。絵自体は課金済みなので捨てない（パネルとしては使える）
        return data, None, None, None, "背景を抜くとアルファが空だった（切り抜き方式=%s）" % info.get("effective")
    # ⚠️ psassist の採寸（バブルの左右・キャラ移動量）はここで測った mask を使う。
    # mask_stats.json（その話数で生成した絵）と混同しないこと ── 混同すると
    # 在庫の絵を貼った行でも生成画像の採寸で左右が決まり、バブルが逆側に出る
    # （2026-09-02実測: ep01 28行中11行で発生。詳細 Docs/AROLL_PSASSIST_REFACTOR_PLAN.md §0-1）
    mask = cutout_engine.analyze_alpha(rgba)
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    return data, buf.getvalue(), fp, mask, ""


async def generate_and_register(
    char_id: str, *, emotion: str, shot: str, angle: str, pose: str = "",
    style_name: str = "kamishibai", model: str = "", replace_stale: bool = True,
    review_status: str = "approved",
) -> dict:
    """1スロット生成し、panel_library/images/へ保存・索引登録して返す。

    背景を抜いて指紋を計算し、透過PNGを cutouts/ に保存する＝**その場で切り抜きの在庫にもなる**。
    ⚠️ 2026-08-29に受け入れ検査（既存と近すぎたら作り直す）を撤去した。理由は
    ``_generate_and_measure`` の docstring（実測で本番在庫の15%を捨てていた）。
    生成した絵は**必ず登録する**。近すぎる絵は人が見て削除する運用に変えた。

    replace_stale=True（既定）: 同じ(emotion,shot,angle)を持つ旧世代（appearance_version不一致）
    のentryがあれば実体ごと削除してから追加する（世代混在を索引に残さない）。

    review_status="approved"（既定）: 1件だけ明示的に生成する通常の呼び出しは、呼び出した人が
    その場で結果を見て判断できるためそのまま承認済み扱いにする。generate_variants()経由の
    一括バリアント生成だけは"pending"で登録し、人の目視確認（approve_entry）を経てから
    find_current（Aロール消費）の対象になる（同一キャラなのに瞳の色が違う等の個体差が
    無審査で本番に流れるのを防ぐ。Docs/AROLL_ASSET_PLAN.md §14参照）。
    """
    # ⚠️ 空だと fragment() が黙って断片を落とし、_next_slot_id が "__001" のような
    # 壊れたslot_idを作る。emotion/shot/angleはfind_currentの照合キーそのものなので、
    # 1つでも空だとその在庫はfind_currentから永久に選ばれない（無言で課金が捨てられる）。
    # generate_variants() もこの関数を1呼び出しにつき1回呼ぶので、ここ1箇所で両方を守る。
    if not (emotion and shot and angle):
        raise ValueError("emotion・shot・angle は全て必須です"
                         "（空だと在庫が自動割当から見えなくなり、課金だけ発生します）")

    # 画像生成の可否は character_manager.can_generate_images が本籍
    # （uses_images ・ appearance_prompt ・ reference の3段。判定順で理由が変わる）。
    # ⚠️ ここで自前の判定を書かないこと ── 以前は旧の紙芝居経路とここで別々に書いていて、
    # 片方だけ直した結果「外見があれば通す」判定が残り、外見だけ複製した Voice バリアント
    # キャラを通してしまっていた（同じキャラの並行在庫ができる穴）。
    # generate_variants() もこの関数を1バリアントにつき1回呼ぶので、ここ一箇所で両方を守る。
    ok, why = character_manager.can_generate_images(char_id)
    if not ok:
        raise ValueError(why)
    c = character_manager.read_character(char_id)
    appearance = (c.get("appearance_prompt") or "").strip()
    style = style_manager.get_style(style_name) or {}
    prefix = (style.get("prefix") or "").strip()

    body = panel_presets.build_panel_prompt(
        appearance, prefix, emotion_id=emotion, shot_id=shot, angle_id=angle,
        pose_id=pose, background_mode="flat",
    )
    prompt = f"{body}, {BACKGROUND_FRAGMENT}. {PROMPT_SUFFIX}"
    refs = _resolve_refs(char_id)

    data, cut_png, fp, mask, cut_note = await _generate_and_measure(char_id, prompt, refs, model)

    ver = appearance_version(char_id)
    idx = load_index(char_id)

    if replace_stale:
        stale = [
            e for e in idx["entries"]
            if e.get("emotion") == emotion and e.get("shot") == shot and e.get("angle") == angle
            and e.get("appearance_version") != ver
        ]
        for e in stale:
            img = (library_dir(char_id) / e.get("image", "")).resolve()
            if img.is_relative_to(images_dir(char_id).resolve()) and img.is_file():
                try:
                    img.unlink()
                except OSError:
                    pass
        stale_ids = {e["slot_id"] for e in stale}
        idx["entries"] = [e for e in idx["entries"] if e.get("slot_id") not in stale_ids]

    existing_ids = {e["slot_id"] for e in idx["entries"]}
    slot_id = _next_slot_id(emotion, shot, angle, existing_ids)
    images_dir(char_id).mkdir(parents=True, exist_ok=True)
    (images_dir(char_id) / f"{slot_id}.png").write_bytes(data)
    if cut_png:
        (library_dir(char_id) / "cutouts").mkdir(parents=True, exist_ok=True)
        (library_dir(char_id) / "cutouts" / f"{slot_id}.png").write_bytes(cut_png)

    entry = {
        "slot_id": slot_id,
        "emotion": emotion, "shot": shot, "angle": angle, "pose": pose or None,
        "appearance_version": ver,
        "aspect": "16:9",
        "image": f"images/{slot_id}.png",
        "style": style_name,
        "model": model.strip() or nanobanana_client.MODEL,
        "prompt": prompt,
        "provider": "nanobanana",
        "created_at": _now(),
        # 切り抜きに失敗した場合だけ理由を残す（絵はパネルとして使えるので登録は続ける）
        "note": cut_note,
        "review_status": review_status,
        "times_used": 0,
    }
    if cut_png:
        entry |= {"cutout": f"cutouts/{slot_id}.png", "fingerprint": fp, "mask": mask}
    idx["entries"].append(entry)
    save_index(char_id, idx)
    return entry


def register_from_image(
    char_id: str, data: bytes, *, emotion: str, shot: str, angle: str,
    pose: str = "", prompt: str = "", style_name: str = "kamishibai",
    model: str = "", provider: str = "nanobanana", source: dict | None = None,
    review_status: str = "approved",
) -> dict:
    """**既にある画像**を切り抜いて在庫へ登録する（生成はしない）。

    Aロールで作った絵を、人が承認した時点で資産化するための入口。
    ``generate_and_register`` との違いは「新しく描かない」ことだけで、
    背景除去 → 指紋 → 登録の流れは同じ。

    ⚠️ **2026-08-29に多様性検査を撤去した。** 撤去前はここで「既存と近すぎる」と判定された絵が
    ``registered: False`` で**黙ってアーカイブから外れていた** ── 人が目視して承認した直後の絵なのに。
    実測では本番在庫の15%（うち25枚は実際に本編で使用中）がこれで消える計算だった。
    理由の詳細は ``_generate_and_measure`` の docstring。

    ⚠️ **冪等**。同じ行の同じ画像は二度登録しない（``source.image_hash`` で照合）。
    承認 → 別の編集 → 再承認、が日常的に起きるため、ここが冪等でないと在庫が重複で膨らむ。
    """
    img_hash = hashlib.sha256(data).hexdigest()[:16]
    src = dict(source or {}, image_hash=img_hash)
    idx = load_index(char_id)
    for e in idx.get("entries", []):
        if (e.get("source") or {}).get("image_hash") == img_hash:
            return {"registered": False, "reason": "同じ画像が既に在庫にある",
                    "slot_id": e.get("slot_id")}

    rgba, info = cutout_engine.cut_out(Image.open(io.BytesIO(data)))
    fp = fingerprint.for_entry(rgba)
    if not fp.get("dhash"):
        # 切り抜きに失敗した絵だけは積まない（cutout も指紋も無い entry は用途が無い）
        return {"registered": False,
                "reason": "背景を抜くとアルファが空だった（方式=%s）" % info.get("effective")}
    # ⚠️ psassist の採寸（バブルの左右等）はここで測る mask を使う。詳細は
    # _generate_and_measure の同種コメント参照。
    mask = cutout_engine.analyze_alpha(rgba)

    ver = appearance_version(char_id)
    slot_id = _next_slot_id(emotion, shot, angle, {e["slot_id"] for e in idx["entries"]})
    images_dir(char_id).mkdir(parents=True, exist_ok=True)
    (images_dir(char_id) / f"{slot_id}.png").write_bytes(data)
    (library_dir(char_id) / "cutouts").mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    (library_dir(char_id) / "cutouts" / f"{slot_id}.png").write_bytes(buf.getvalue())

    entry = {
        "slot_id": slot_id, "emotion": emotion, "shot": shot, "angle": angle,
        "pose": pose or None, "appearance_version": ver, "aspect": "16:9",
        "image": f"images/{slot_id}.png", "cutout": f"cutouts/{slot_id}.png",
        "fingerprint": fp, "mask": mask, "cutout_method": info.get("effective"),
        "style": style_name, "model": model or None, "prompt": prompt,
        "provider": provider, "source": src, "created_at": _now(), "note": "",
        "review_status": review_status, "times_used": 0,
    }
    idx["entries"].append(entry)
    save_index(char_id, idx)
    return {"registered": True, "reason": "", "slot_id": slot_id, "entry": entry}


async def generate_variants(
    char_id: str, *, emotion: str, shot: str, angle: str, poses: list[str],
    style_name: str = "kamishibai", model: str = "",
) -> list[dict]:
    """同じ(emotion,shot,angle)にposeだけを変えて複数バリアントを一括生成する。

    matching key（slot_key）はemotion/shot/angleの3軸のまま変えない（組み合わせ爆発を避ける。
    Docs/AROLL_SLOT_REUSE_BRIEF.md §2-2の踏襲）。poseは既存の固定語彙（panel_presets.py）から
    選ぶ想定＝真に自由なLLM即興文にはしない（識別情報のブレを最小化するため）。

    生成物は全てreview_status="pending"で登録される。承認するまでfind_current（Aロール消費）
    からは見えない＝人が目視確認してapprove_entry()を呼ぶまで本番に流れない設計。
    ⚠️ これは**個体差（同一キャラなのに瞳の色が違う等）に対する人のレビュー**であって、
    撤去した多様性検査とは別物。こちらは残す。

    2026-08-29: 多様性検査の撤去に伴い、この経路も**切り抜きと指紋を持つようになった**
    （撤去前は check_diversity=False が「背景除去もしない」を兼ねていたため、バリアントは
    ✂️切り抜きの在庫にならなかった）。承認すれば合成素材としても使える。
    """
    entries = []
    for pose in poses:
        entry = await generate_and_register(
            char_id, emotion=emotion, shot=shot, angle=angle, pose=pose,
            style_name=style_name, model=model, replace_stale=False,
            review_status="pending",
        )
        entries.append(entry)
    return entries
