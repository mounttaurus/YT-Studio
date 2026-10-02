"""行の操作の「派生物」の登録制（Docs/LINE_WORKBENCH_PLAN.md §3・§7-2・W1）。

台本が変わると、台本から作られた**派生物**（音声・絵・仕上がり）が影響を受ける。
窓口（`line_ops.py`）は「台本が変わった → 登録された派生物それぞれに通知」という形で
書いてあり、**派生物の中身を知らない**。各派生物が実装するのは2つだけ:

- `describe(ctx)` … 影響の文面（dry_run の確認欄）。ファイルを読むだけで何も変えない
- `apply(ctx)`    … 後処理（実行）。**消さない・課金しない**（I5・I6）。失敗しても台本は確定済み（I3）
- `confirm(ctx)`  … **行の確定**（W2・§4-3）の時の後処理（任意。既定は何もしない）。音声＝要再生成・未生成の
                    行を作り直す（ローカルGPU・無料＝I6 の例外・D9）／絵＝新しい行の下ごしらえ。画像の生成（課金）は走らない

今の派生物は ja音声（`tts`）・Aロール（`aroll`）・仕上がり（`final`）の3つ。将来の
`訳（lang）`・`訳の音声（lang）`・`訳の吹き出し（lang）` は、`register()` を1回呼ぶだけで
**窓口を書き換えずに**足せる（レーン名 `lane` が応答の `impact` のキーになる）。
影響の文面は §2-3 の表（項目が何に効くか）から作る＝表を変えたら文面も変わる（このファイル1か所）。

`kind`: auto＝窓口がこの場で済ませる／later＝行に印が付き、確定や各タブの一括操作で片付ける／keep＝影響なし。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core import downstream


@dataclass
class OpContext:
    """1回の操作（または Undo）を派生物へ伝える入れ物。"""
    op: str                         # edit / emotion / speaker / split / merge / add-subline / insert / move / delete / undo
    project_id: str
    episode: int
    line_id: str | None
    params: dict
    before: list[dict]              # 操作前の台本の行（ドラフト優先＝画面と同じ）
    after: list[dict]               # 操作後の台本の行
    diff: dict                      # `line_fingerprint.diff_lines` の結果
    ep_dir: Path | None
    hints: dict = field(default_factory=dict)   # 例 {"split_front_line_ids": [...]}
    dry_run: bool = False


@dataclass
class ConfirmContext:
    """行の確定を派生物へ伝える入れ物（W2）。`line_ids` は今回確定した行（台本の並び順）。"""
    project_id: str
    episode: int
    line_ids: list[str]
    script_lines: list[dict]        # 正本 script.json の行
    ep_dir: Path | None


class Derivative:
    key = ""      # 内部名
    lane = ""     # 応答 `impact` のキー
    label = ""    # 人向けの名前（警告の頭に付く）

    def describe(self, ctx: OpContext) -> list[dict]:
        raise NotImplementedError

    async def apply(self, ctx: OpContext) -> dict:
        raise NotImplementedError

    async def confirm(self, ctx: ConfirmContext) -> dict:
        """行の確定の後処理。既定は何もしない。失敗は raise でよい（`confirm_all` が warnings にする）。"""
        return {}


_REGISTRY: list[Derivative] = []


def register(d: Derivative) -> None:
    """派生物を登録する（同じ key は置き換える）。"""
    for i, cur in enumerate(_REGISTRY):
        if cur.key == d.key:
            _REGISTRY[i] = d
            return
    _REGISTRY.append(d)


def registered() -> list[Derivative]:
    return list(_REGISTRY)


def describe_all(ctx: OpContext) -> dict:
    out = {}
    for d in _REGISTRY:
        try:
            out[d.lane] = d.describe(ctx)
        except Exception as e:  # noqa: BLE001 — 文面の失敗で確認欄全体を落とさない
            out[d.lane] = [item("keep", f"（{d.label}の影響を調べられませんでした: {type(e).__name__}）")]
    return out


async def apply_all(ctx: OpContext) -> tuple[dict, list[str]]:
    """全派生物の後処理。**1つ失敗しても残りは続け、台本の変更は取り消さない**（I3）。
    失敗と、派生物自身が返した注意（例: 在庫の絵を戻せなかった）を `warnings` にまとめる。"""
    applied, warnings = {}, []
    for d in _REGISTRY:
        try:
            res = await d.apply(ctx)
            applied[d.lane] = res
            for w in (res or {}).get("warnings") or []:
                warnings.append(f"{d.label}: {w}")
        except Exception as e:  # noqa: BLE001
            applied[d.lane] = {"error": f"{type(e).__name__}: {e}"}
            warnings.append(f"{d.label}の後処理に失敗しました（台本の変更は確定済み）: {type(e).__name__}: {e}")
    return applied, warnings


def by_key(key: str) -> Derivative | None:
    return next((d for d in _REGISTRY if d.key == key), None)


async def confirm_all(ctx: ConfirmContext, only: str | None = None) -> tuple[dict, list[str]]:
    """行の確定の後処理を全派生物へ（`only` で1つに絞れる＝音声の待ちだけ流す等）。
    1つ失敗しても残りは続ける。確定そのもの（指紋の記録）は済んでいる（I3 と同じ理屈）。"""
    applied, warnings = {}, []
    for d in _REGISTRY:
        if only and d.key != only:
            continue
        try:
            res = await d.confirm(ctx)
            applied[d.lane] = res
            for w in (res or {}).get("warnings") or []:
                warnings.append(f"{d.label}: {w}")
        except Exception as e:  # noqa: BLE001
            applied[d.lane] = {"error": f"{type(e).__name__}: {e}"}
            warnings.append(f"{d.label}の確定後処理に失敗しました（確定は記録済み）: {type(e).__name__}: {e}")
    return applied, warnings


# ─── 文面のための小道具 ─────────────────────────────────────────────

def item(kind: str, text: str) -> dict:
    return {"kind": kind, "text": text}


def names(ids: list[str], limit: int = 3) -> str:
    ids = list(ids)
    head = "・".join(ids[:limit])
    return head if len(ids) <= limit else f"{head} ほか{len(ids) - limit}件"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _tts_state(ctx: OpContext) -> dict | None:
    return _read_json(ctx.ep_dir / "tts.json") if ctx.ep_dir else None


def _aroll_panels(ctx: OpContext) -> dict | None:
    """{line_id: panel}（孤立を除く）。aroll.json が無ければ None（Aロール未着手）。"""
    doc = _read_json(ctx.ep_dir / "a_roll" / "aroll.json") if ctx.ep_dir else None
    if doc is None:
        return None
    return {p.get("line_id"): p for p in doc.get("panels", []) if not p.get("orphan")}


def _has_picture(panel: dict | None) -> bool:
    return bool(panel) and bool((panel.get("status") == "done" and panel.get("image"))
                                or panel.get("cutout_slot_id"))


def _psd_ids(ctx: OpContext) -> set:
    d = (ctx.ep_dir / "psassist" / "psd_final") if ctx.ep_dir else None
    if d is None or not d.is_dir():
        return set()
    return {f.stem[len("panel_"):] for f in d.glob("panel_*.psd")}


SPLIT_OPS = ("split", "split-apply", "split-apply-all")


def _split_front_ids(ctx: OpContext) -> set:
    if ctx.hints.get("split_front_line_ids"):
        return set(ctx.hints["split_front_line_ids"])
    if ctx.op == "split":
        return {ctx.line_id} if ctx.line_id else set()
    if ctx.op in SPLIT_OPS:            # 自動区切り: 本文が短くなって新しいサブ行が出来た元の行が「前半」
        return {lid for lid, fields in ctx.diff["changed"].items() if "text" in fields}
    return set()


# ─── 派生物1: ja音声（tts-agent） ───────────────────────────────────

# §2-3「項目が何に効くか」の音声の列。台本の項目 → 要再生成の理由
_TTS_REASON = {
    "text": "本文が変わった",
    "emotion": "声の演技が変わる",
    "speaker_id": "声が変わる",
    "speed": "速度が変わる",
}


class TtsDerivative(Derivative):
    key, lane, label = "tts", "tts", "音声"

    def describe(self, ctx: OpContext) -> list[dict]:
        tts = _tts_state(ctx)
        have = {f.get("line_id") for f in (tts or {}).get("audio_files", [])}
        d, out = ctx.diff, []

        gone = [i for i in d["removed_ids"] if i in have]
        if gone:
            out.append(item("auto", f"{names(gone)} の音声は消さずに保管する（孤立扱い・元に戻すと復活）"))

        by_reason: dict[str, list[str]] = {}
        pause_only = []
        for lid, fields in d["changed"].items():
            if lid not in have:
                continue
            for f in fields:
                if f in _TTS_REASON:
                    by_reason.setdefault(_TTS_REASON[f], []).append(lid)
            if "pause_after_sec" in fields:
                pause_only.append(lid)
        for reason, ids in by_reason.items():
            head = "前半 " if ctx.op in SPLIT_OPS and reason == _TTS_REASON["text"] else ""
            out.append(item("later", f"{head}{names(ids)} は要再生成（{reason}）"))
        if pause_only:
            out.append(item("later", f"{names(pause_only)} の間が変わる → タイムラインを作り直すと反映"
                                     "（音声の作り直しは不要）"))

        if d["new_ids"]:
            label = "後半 " if ctx.op in SPLIT_OPS else "新しい行 "
            out.append(item("later", f"{label}{names(d['new_ids'])} は音声が未生成"))
        moved = [i for i in d["moved_ids"] if i in have]
        if moved:
            out.append(item("auto", "タイムラインの並びを台本に合わせる（音声ファイルはそのまま）"))
        if not out:
            out.append(item("keep", "音声への影響なし" if have else "音声は未生成のため影響なし"))
        return out

    async def apply(self, ctx: OpContext) -> dict:
        res = await downstream.call(
            "tts", "POST", f"/projects/{ctx.project_id}/audio/sync-structure",
            params={"episode": ctx.episode}, timeout=30.0)
        if res.status_code >= 400:
            raise RuntimeError(downstream.error_detail(res))
        return res.json()

    async def confirm(self, ctx: ConfirmContext) -> dict:
        """確定した行のうち、要再生成・未生成の行だけを tts-agent に作り直させる（D9）。
        最新の行は作り直さない。エンジン（GPU）が止まっていれば作れず、その行は『作り直し待ち』
        （`GET .../audio/pending`）に残る＝起動後に `audio-catch-up` で流せる。"""
        res = await downstream.call(
            "tts", "POST", f"/projects/{ctx.project_id}/run/lines", params={"episode": ctx.episode},
            json={"line_ids": ctx.line_ids}, timeout=30.0)
        if res.status_code >= 400:
            raise RuntimeError(downstream.error_detail(res)
                               + "（作れなかった行は待ちに残る。あとで audio-catch-up で流せます）")
        out = res.json()
        warnings = []
        if out.get("unassigned"):
            warnings.append(f"{names(out['unassigned'])} は声が未割当のため音声を作れません（キャラタブで配役してください）")
        return {**out, "warnings": warnings}


# ─── 派生物2: Aロール（scrapping-agent） ────────────────────────────

class ArollDerivative(Derivative):
    key, lane, label = "aroll", "aroll", "Aロール"

    def describe(self, ctx: OpContext) -> list[dict]:
        panels = _aroll_panels(ctx)
        if panels is None:
            return [item("keep", "Aロール未着手（aroll.json が無い）ため何もしない")]
        d, out = ctx.diff, []
        fronts = _split_front_ids(ctx)

        gone = [i for i in d["removed_ids"] if i in panels]
        if gone:
            stocked = [i for i in gone if panels[i].get("cutout_slot_id")]
            extra = f"・在庫 {len(stocked)} 枚の使用回数を戻す" if stocked else ""
            out.append(item("auto", f"{names(gone)} のコマは孤立扱いにする（絵は保管{extra}・元に戻すと復活）"))
        if d["new_ids"]:
            label = "後半 " if ctx.op in SPLIT_OPS else "新しい行 "
            out.append(item("auto", f"{label}{names(d['new_ids'])} のコマを追加する"
                                    "（絵は未選択。在庫で埋める／生成は Aロールタブから）"))

        text_stale, kept_front, speaker_stale = [], [], []
        for lid, fields in d["changed"].items():
            if not _has_picture(panels.get(lid)):
                continue
            if "text" in fields:
                (kept_front if lid in fronts else text_stale).append(lid)
            if "speaker_id" in fields:
                speaker_stale.append(lid)
        if kept_front:
            out.append(item("auto", f"前半 {names(kept_front)} の絵は今のまま"
                                    "（分割は絵の内容を変えないので『台本とズレ』印は付けない）"))
        if text_stale:
            out.append(item("later", f"{names(text_stale)} は『台本とズレ』印が付く（本文が変わった）→ 確認して確定"))
        if speaker_stale:
            out.append(item("later", f"{names(speaker_stale)} は絵が別人になる → 『台本とズレ』印を付け、"
                                     "描くキャラを新しい話者に合わせる。在庫から選び直す"))
        emotion_only = [l for l, f in d["changed"].items() if f == ["emotion"]]
        if emotion_only and not (text_stale or speaker_stale):
            out.append(item("keep", "絵は変わらない（声の演技だけ。絵の表情は別）"))
        if [i for i in d["moved_ids"] if i in panels]:
            out.append(item("auto", "コマの並びを台本に合わせる"))
        if not out:
            out.append(item("keep", "絵への影響なし"))
        return out

    async def apply(self, ctx: OpContext) -> dict:
        res = await downstream.call(
            "scrapping", "POST",
            f"/projects/{ctx.project_id}/episodes/{ctx.episode}/aroll/lines/sync-structure",
            json={"split_front_line_ids": sorted(_split_front_ids(ctx))}, timeout=60.0)
        if res.status_code >= 400:
            raise RuntimeError(downstream.error_detail(res))
        return res.json()

    async def confirm(self, ctx: ConfirmContext) -> dict:
        """確定した行の絵の下ごしらえ（すべて無料・機械処理だけ。**LLM も画像生成も呼ばない**＝I6・
        `Docs/AROLL_EMOTION_LOCAL_PLAN.md` §5 E1）。

        ① 行構造をコマ一覧へ合わせ、プロンプトの無いコマを埋める（窓口を通らず変わった台本でもコマが
           あるように・冪等）。サブ行は親のコマを引き継ぎ、独立した新しい行はルールの slot（E2・E3）。
           英語の演出プロンプトは「残りを生成」の時に無い行だけ作る（E4）＝ここでは作らない
        ② 背景を自動で当てる（確定した行だけ・`only_missing`＝手で選んだ背景は壊さない）
        ③ 話者を替えた行は在庫から選び直す（行を明示した `cutout-plan/apply`）
        各段の失敗は `warnings` に載せて次へ進む。在庫の候補は新しい行に自動では当てない
        （『在庫で埋める』は Aロールタブの一括操作＝W4b。ワンパターンの発生源にしない）。
        """
        base = f"/projects/{ctx.project_id}/episodes/{ctx.episode}/aroll"
        if _aroll_panels(_ctx_stub(ctx)) is None:
            return {"skipped": "Aロール未着手（aroll.json が無い）ため何もしない", "warnings": []}
        out: dict = {"warnings": []}

        async def step(key: str, label: str, path: str, body: dict, timeout: float = 60.0):
            try:
                res = await downstream.call("scrapping", "POST", f"{base}{path}", json=body, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                out["warnings"].append(f"{label}に失敗: {type(e).__name__}: {e}")
                return None
            if res.status_code >= 400:
                out["warnings"].append(f"{label}に失敗: {downstream.error_detail(res)}")
                return None
            out[key] = res.json()
            return out[key]

        await step("sync", "行構造の同期", "/lines/sync-structure", {"fill_line_ids": list(ctx.line_ids)})
        await step("backgrounds", "背景の割当", "/backgrounds/auto_assign",
                   {"only_missing": True, "line_ids": ctx.line_ids})
        panels = _aroll_panels(_ctx_stub(ctx)) or {}
        swapped = [lid for lid in ctx.line_ids if (panels.get(lid) or {}).get("speaker_changed")]
        if swapped:
            await step("stock", "在庫の選び直し", "/cutout-plan/apply", {"line_ids": swapped})
        return out


def _ctx_stub(ctx: ConfirmContext) -> OpContext:
    """`_aroll_panels` は OpContext から ep_dir だけを見る。確定用の入れ物でも読めるようにする。"""
    return OpContext(op="confirm", project_id=ctx.project_id, episode=ctx.episode, line_id=None, params={},
                     before=[], after=[], diff={"new_ids": [], "removed_ids": [], "changed": {}, "moved_ids": []},
                     ep_dir=ctx.ep_dir)


# ─── 派生物3: 仕上がり（psassist・ホスト常駐の合成） ─────────────────

class FinalDerivative(Derivative):
    """合成（PSD・書き出しPNG）は `line_id` 単位のファイルで、消えも移動もしない。
    後処理は無い（再合成は各タブの一括操作＝W4b）。影響の文面だけを出す。"""
    key, lane, label = "final", "final", "仕上がり"

    def describe(self, ctx: OpContext) -> list[dict]:
        psd = _psd_ids(ctx)
        d, out = ctx.diff, []

        text_ids = [l for l, f in d["changed"].items() if l in psd and "text" in f]
        speaker_ids = [l for l, f in d["changed"].items() if l in psd and "speaker_id" in f]
        if text_ids:
            head = "前半 " if ctx.op in SPLIT_OPS else ""
            why = "吹き出しが短くなる" if ctx.op in SPLIT_OPS else "吹き出しの文字が変わる"
            out.append(item("later", f"{head}{names(text_ids)} は{why} → 再合成が要る"))
        if speaker_ids:
            out.append(item("later", f"{names(speaker_ids)} は吹き出しの形・向きの既定が変わる → 再合成が要る"))
        if d["new_ids"]:
            label = "後半 " if ctx.op in SPLIT_OPS else "新しい行 "
            out.append(item("later", f"{label}{names(d['new_ids'])} は未合成"))
        if [i for i in d["moved_ids"] if i in psd]:
            out.append(item("later", "背景の続き・吹き出しの向き（左右）が変わりうる → 仕上がりを確認"))
        gone = [i for i in d["removed_ids"] if i in psd]
        if gone:
            out.append(item("keep", f"{names(gone)} の合成物（PSD・書き出しPNG）は消さずに保管"))
        if not out:
            out.append(item("keep", "仕上がりへの影響なし" if psd else "合成は未着手のため影響なし"))
        return out

    async def apply(self, ctx: OpContext) -> dict:
        return {}


register(TtsDerivative())
register(ArollDerivative())
register(FinalDerivative())
