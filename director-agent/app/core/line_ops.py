"""行の操作の窓口（Docs/LINE_WORKBENCH_PLAN.md §3・W1）。

台本を変える操作（9種）は**この窓口だけ**を通す（I1）。UI・MCP は後処理を書かない。

実行順（1操作）:
  1. 操作前のスナップショットを操作履歴（`episodes/epNN/line_ops/`）へ
  2. scripting-agent の既存の行API（`/script/line/...`）で台本を変更  ← **確定点**（I3）
  3. 登録された派生物（`derivatives.py`）の後処理。すべて無料・消さない（I5・I6）。
     失敗しても台本の変更は取り消さず、`warnings` に載せて director の監査ログへ
  4. 変わった行を返す

`?dry_run=true` は同じ検証を scripting-agent に通して（何も保存させず）、操作後の台本を
プレビューとして受け取り、操作前との差分から「影響」を導く。何も書かない。

Undo（D11）は、直前の操作のスナップショットの台本を書き戻し、派生物の後処理を再実行する
（外した音声・コマは消していない＝I5 なので、台本に戻った行へそのまま戻る）。
今の director の Undo（`import` で丸ごと書き戻し）はこれに置き換わる。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core import confirmations, derivatives, downstream, project_manager
from app.core.derivatives import OpContext, item
from app.core.line_fingerprint import diff_lines

OPS = ("edit", "emotion", "speaker", "split", "merge", "add-subline", "insert", "move", "delete",
       "split-apply", "split-apply-all")
# 行を前後に分ける操作（手動の分割・行ごとの自動区切り・話数全体の自動区切り）。前半は同じ行ID・後半が新しいサブ行
SPLIT_OPS = ("split", "split-apply", "split-apply-all")
# 「LLMの案を採用」（W2・§4-1）。行を直す9操作とは別物（ドラフト→正本）だが、窓口の同じ流れ
# （スナップショット→scripting→派生物の後処理→履歴とUndo）に乗せる＝採用もUndoで戻せる
ADOPT = "adopt"
ALL_OPS = OPS + (ADOPT,)
MAX_HISTORY = 30          # 話数ごとに残す操作の数（古いものから消す）
_OP_LABELS = {"edit": "本文の編集", "emotion": "声の感情の変更", "speaker": "話者の変更", "split": "行を分ける",
              "merge": "次の行と結合", "add-subline": "サブ行の追加", "insert": "行の挿入",
              "move": "行の移動", "delete": "行の削除", "undo": "元に戻す", "adopt": "LLMの案を採用",
              "split-apply": "長い行を自動で区切る", "split-apply-all": "長い行をすべて自動で区切る"}


class OpError(Exception):
    """窓口のエラー。`status` は HTTP ステータス、`message` は人が読める理由。"""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ─── 台本の読み出し（読み取り専用・生のファイル） ────────────────────────

@dataclass
class Docs:
    draft: dict | None
    script: dict | None

    @property
    def primary(self) -> dict | None:
        """scripting-agent が行操作の基準にする台本（ドラフト優先・無ければ確定版）。"""
        return self.draft if self.draft is not None else self.script

    def lines(self) -> list[dict]:
        return list((self.primary or {}).get("lines", []))

    def script_lines(self) -> list[dict]:
        """正本（script.json）の行。TTS・Aロール・確定が読む台本＝採用の前後を比べる基準。"""
        return list((self.script or {}).get("lines", []))

    def sha(self) -> dict:
        return {"draft": _sha(self.draft), "script": _sha(self.script)}

    def same_as(self, other: "Docs") -> bool:
        return self.draft == other.draft and self.script == other.script


def _sha(doc: dict | None) -> str | None:
    if doc is None:
        return None
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def read_docs(ep_dir: Path) -> Docs:
    return Docs(draft=_load(ep_dir / "script_draft.json"), script=_load(ep_dir / "script.json"))


# ─── 操作 → scripting-agent の行API ───────────────────────────────────

@dataclass
class Call:
    method: str
    path: str
    json: dict | None
    basis: str = "primary"   # 前後を比べる台本: primary＝ドラフト優先（行操作）／script＝正本（採用）
    query: dict | None = None   # scripting-agent へ足すクエリ（例 自動区切りの上限字数 limit）


def _find(lines: list[dict], line_id: str | None, what: str = "line_id") -> dict:
    if not line_id:
        raise OpError(400, f"{what} が必要です")
    line = next((l for l in lines if l.get("id") == line_id), None)
    if line is None:
        raise OpError(404, f"{what}={line_id} の行が台本に見つかりません")
    return line


def _order(line: dict, lines: list[dict]) -> int:
    return int(line.get("order") or (lines.index(line) + 1))


def _pick(body: dict, keys: tuple) -> dict:
    return {k: body[k] for k in keys if body.get(k) is not None}


def _limit_query(body: dict) -> dict | None:
    """自動区切りの上限字数（省略で scripting-agent の既定＝55字）。"""
    if body.get("limit") is None:
        return None
    if not isinstance(body["limit"], int) or body["limit"] < 2:
        raise OpError(400, "limit（上限字数）は2以上の整数で指定してください")
    return {"limit": body["limit"]}


def _resolve_orders(body: dict, lines: list[dict]) -> dict:
    """行の指定は `line_id`（正）のほか、行番号 `order`（挿入は `after_order`）でも受ける。
    MCP の旧来の呼び方（`split_line(order=…)`）を窓口経由に切り替えても、呼び出し側が先に台本を引かなくて済む。
    `line_id` があればそちらを優先する。"""
    body = dict(body)
    for key, src in (("line_id", "order"), ("after_line_id", "after_order")):
        n = body.get(src)
        if body.get(key) is None and n is not None:
            if not isinstance(n, int) or isinstance(n, bool):
                raise OpError(400, f"{src} は行番号（整数）で指定してください")
            if key == "after_line_id" and n == 0:
                body[key] = ""                                   # 先頭に入れる
            else:
                line = next((l for l in lines if _order(l, lines) == n), None)
                if line is None:
                    raise OpError(404, f"{src}={n} の行が台本に見つかりません")
                body[key] = line["id"]
    return body


def build_call(project_id: str, op: str, body: dict, lines: list[dict], episode: int = 1) -> Call:
    """操作を scripting-agent の既存の行APIの呼び出しへ翻訳する。入力の検査もここ。"""
    body = _resolve_orders(body, lines)
    base = f"/projects/{project_id}/script/line"
    if op == ADOPT:
        line_ids = body.get("line_ids")
        if line_ids is not None and not isinstance(line_ids, list):
            raise OpError(400, "line_ids は行IDの配列（省略で差のある行すべて）")
        return Call("POST", f"/projects/{project_id}/episodes/{episode}/script/adopt",
                    {"line_ids": line_ids, "replace_all": bool(body.get("replace_all", False))}, basis="script")
    if op == "insert":
        anchor = body.get("after_line_id")
        if anchor is None:
            anchor = body.get("line_id")
        if anchor is None:
            raise OpError(400, "after_line_id が必要です（先頭に入れる時は空文字 \"\" を指定）")
        after_order = 0 if anchor == "" else _order(_find(lines, anchor, "after_line_id"), lines)
        return Call("POST", base, {"after_order": after_order,
                                   **_pick(body, ("text", "speaker_id", "emotion", "speed", "pause_after_sec"))})

    if op == "split-apply-all":
        return Call("POST", f"/projects/{project_id}/episodes/{episode}/split-apply-all", None,
                    query=_limit_query(body))
    line = _find(lines, body.get("line_id"))
    order = _order(line, lines)
    if op == "split-apply":
        return Call("POST", f"{base}/{order}/split-apply", None, query=_limit_query(body))
    if op == "edit":
        for bad in ("emotion", "speaker_id", "speaker_name"):
            if body.get(bad) is not None:
                raise OpError(400, f"{bad} は edit では変えられません（op={'emotion' if bad == 'emotion' else 'speaker'} を使う）")
        payload = _pick(body, ("text", "speed", "pause_after_sec", "notes"))
        if not payload:
            raise OpError(400, "変える項目（text / speed / pause_after_sec / notes）を指定してください")
        return Call("PATCH", f"{base}/{order}", payload)
    if op == "emotion":
        if not body.get("emotion"):
            raise OpError(400, "emotion が必要です")
        return Call("PATCH", f"{base}/{order}", {"emotion": body["emotion"]})
    if op == "speaker":
        if not body.get("speaker_id"):
            raise OpError(400, "speaker_id が必要です")
        return Call("PATCH", f"{base}/{order}", _pick(body, ("speaker_id", "speaker_name")))
    if op == "split":
        if not isinstance(body.get("position"), int):
            raise OpError(400, "position（文字位置・整数）が必要です")
        return Call("POST", f"{base}/{order}/split", {"position": body["position"]})
    if op == "merge":
        return Call("POST", f"{base}/{order}/merge-next", None)
    if op == "add-subline":
        return Call("POST", f"{base}/{order}/add-subline", _pick(body, ("text", "emotion")))
    if op == "move":
        if body.get("direction") not in ("up", "down"):
            raise OpError(400, "direction は up か down を指定してください")
        return Call("PATCH", f"{base}/{order}/move", {"direction": body["direction"]})
    if op == "delete":
        return Call("DELETE", f"{base}/{order}", None)
    raise OpError(404, f"未知の操作です: {op}（{' / '.join(ALL_OPS)}）")


# ─── 操作履歴（`episodes/epNN/line_ops/`・director だけが書く） ───────────────

def _ops_dir(ep_dir: Path) -> Path:
    return ep_dir / "line_ops"


def _new_op_id() -> str:
    # 時刻を先頭に置く＝文字列ソートが作成順（マイクロ秒＋乱数で同時刻の衝突を避ける）
    return f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}-{secrets.token_hex(2)}"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _begin(ep_dir: Path, op: str, params: dict, before: Docs) -> str:
    """操作前のスナップショットを先に書く（`pending`）。台本の変更が確定した後で `commit` する。"""
    op_id = _new_op_id()
    d = _ops_dir(ep_dir)
    _write_json(d / f"{op_id}.before.json", {"draft": before.draft, "script": before.script})
    _write_json(d / f"{op_id}.json", {
        "op_id": op_id, "op": op, "params": params, "line_id": params.get("line_id"),
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "pending", "undone": False})
    return op_id


def _update_meta(ep_dir: Path, op_id: str, **fields) -> None:
    f = _ops_dir(ep_dir) / f"{op_id}.json"
    meta = json.loads(f.read_text(encoding="utf-8"))
    meta.update(fields)
    _write_json(f, meta)


def _commit(ep_dir: Path, op_id: str, after: Docs, diff: dict) -> None:
    """台本の変更が確定した時点で履歴を確定する（後処理の前。後処理の途中で落ちても Undo できる）。"""
    _update_meta(ep_dir, op_id, status="committed", new_line_ids=diff["new_ids"],
                 removed_line_ids=diff["removed_ids"], warnings=[])
    _write_head(ep_dir, after, op_id)
    _prune(ep_dir)


def _discard(ep_dir: Path, op_id: str) -> None:
    d = _ops_dir(ep_dir)
    for suffix in (".json", ".before.json"):
        (d / f"{op_id}{suffix}").unlink(missing_ok=True)


def _write_head(ep_dir: Path, docs: Docs, op_id: str | None) -> None:
    """窓口が最後に確定させた台本の指紋。Undo の時、これと今の台本が違えば
    「窓口を通さず変えられた」＝書き戻すと他の変更を潰す、と分かる。"""
    _write_json(_ops_dir(ep_dir) / "_head.json", {"sha": docs.sha(), "op_id": op_id})


def _metas(ep_dir: Path) -> list[dict]:
    d = _ops_dir(ep_dir)
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_") or f.name.endswith(".before.json"):
            continue
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _prune(ep_dir: Path) -> None:
    committed = [m for m in _metas(ep_dir) if m.get("status") == "committed"]
    for m in committed[:-MAX_HISTORY]:
        _discard(ep_dir, m["op_id"])


async def proposal(project_id: str, episode: int) -> dict:
    """LLMの案（ドラフト）と正本の行ごとの差分。採用する前に見せる。何も変えない。
    差分の計算と全文再生成の判定は scripting-agent（`adoption.py`）が本籍。"""
    _ep_dir(project_id, episode)
    try:
        res = await downstream.call("scripting", "GET", f"/projects/{project_id}/episodes/{episode}/script/proposal")
    except httpx.RequestError as e:
        raise OpError(502, f"scripting-agent に接続できません: {e}")
    if res.status_code >= 400:
        raise OpError(res.status_code, downstream.error_detail(res))
    return res.json()


def history(project_id: str, episode: int, limit: int = 20) -> dict:
    ep_dir = _ep_dir(project_id, episode)
    ops = [m for m in _metas(ep_dir) if m.get("status") == "committed"]
    summaries = [{
        "op_id": m["op_id"], "op": m["op"], "label": _OP_LABELS.get(m["op"], m["op"]),
        "line_id": m.get("line_id"), "created_at": m.get("created_at"), "undone": bool(m.get("undone")),
        "new_line_ids": m.get("new_line_ids", []), "removed_line_ids": m.get("removed_line_ids", []),
    } for m in reversed(ops)]
    return {"undoable": sum(1 for m in ops if not m.get("undone")), "ops": summaries[:limit]}


# ─── 実行 ────────────────────────────────────────────────────────────

_locks: dict[tuple, asyncio.Lock] = {}


def _lock(project_id: str, episode: int) -> asyncio.Lock:
    return _locks.setdefault((project_id, episode), asyncio.Lock())


def _ep_dir(project_id: str, episode: int) -> Path:
    ep_dir = project_manager.episode_dir(project_id, episode)
    if ep_dir is None:
        raise OpError(404, f"プロジェクトまたは第{episode}話が見つかりません")
    return ep_dir


def _script_items(op: str, ctx: OpContext) -> list[dict]:
    """台本の区画（窓口自身が済ませる部分）の文面。"""
    d, lid = ctx.diff, ctx.line_id
    new = d["new_ids"]
    if op == "edit":
        return [item("auto", f"{lid} の項目を更新する（line_id は変わらない）")]
    if op == "emotion":
        old = next((l.get("emotion") or "neutral" for l in ctx.before if l.get("id") == lid), "neutral")
        return [item("auto", f"{lid} の声の感情を「{old}」→「{ctx.params.get('emotion')}」にする")]
    if op == "speaker":
        n = sum(1 for f in d["changed"].values() if "speaker_id" in f)
        tail = f"（同じグループの全{n}行に伝わる）" if n > 1 else ""
        return [item("auto", f"{lid} の話者を「{ctx.params.get('speaker_id')}」にする{tail}")]
    if op == "split":
        back = new[0] if new else "新しいサブ行"
        return [item("auto", f"前半は {lid} のまま、後半は {back} として直後に挿入する")]
    if op in ("split-apply", "split-apply-all"):
        fronts = [l for l, f in d["changed"].items() if "text" in f]
        scope = f"{lid} を" if op == "split-apply" else f"上限を超えた {len(fronts)}行を"
        return [item("auto", f"{scope}句点・読点で自動で区切る（前半は同じ行ID・後半 {len(new)}行が新しいサブ行として直後に入る）")]
    if op == "merge":
        gone = d["removed_ids"][0] if d["removed_ids"] else "次の行"
        return [item("auto", f"{lid} に {gone} の本文を結合する（{gone} は台本から外れる）")]
    if op == "add-subline":
        return [item("auto", f"{lid} の直後に同じ話者のサブ行 {new[0] if new else ''} を追加する")]
    if op == "insert":
        return [item("auto", f"新しい行 {new[0] if new else ''} を挿入する")]
    if op == "move":
        return [item("auto", f"{lid} を{'上' if ctx.params.get('direction') == 'up' else '下'}へ1つ動かす")]
    if op == "delete":
        return [item("auto", f"{lid} を台本から外す")]
    if op == "undo":
        return [item("auto", "台本の変更を1つ戻す")]
    if op == ADOPT:
        n = len(d["changed"]) + len(new) + len(d["removed_ids"])
        return [item("auto", f"LLMの案を正本へ採用する（変更{len(d['changed'])}行・新規{len(new)}行・削除{len(d['removed_ids'])}行"
                             f"＝計{n}行。採用した行は『未確定』になる）")]
    return []


def _context(project_id, episode, op, body, before_lines, after_lines, ep_dir, hints=None, dry_run=False) -> OpContext:
    return OpContext(
        op=op, project_id=project_id, episode=episode, line_id=body.get("line_id"), params=body,
        before=before_lines, after=after_lines, diff=diff_lines(before_lines, after_lines),
        ep_dir=ep_dir, hints=hints or {}, dry_run=dry_run)


def _touched(diff: dict) -> list[str]:
    seen, out = set(), []
    for lid in [*diff["new_ids"], *diff["changed"], *diff["moved_ids"]]:
        if lid not in seen:
            seen.add(lid)
            out.append(lid)
    return out


def _response(op: str, ctx: OpContext, *, dry_run: bool, changed: bool, warnings: list[str],
              impact: dict | None = None, extra: dict | None = None,
              state_lines: list[dict] | None = None) -> dict:
    return {
        "op": op, "ok": True, "dry_run": dry_run, "changed": changed,
        "line_id": ctx.line_id, "new_line_ids": ctx.diff["new_ids"],
        "removed_line_ids": ctx.diff["removed_ids"],
        "impact": impact or {},
        # touched_line_ids＝この操作で新規・変更・移動した行。未確定の行（W2）は確定の運用が
        # 始まっている話数だけ出る（運用外の話数は confirmation_enabled=False）。dry_run は
        # 操作後のプレビューの行で、実行後は正本 script.json で数える
        "state": {"touched_line_ids": _touched(ctx.diff),
                  **(confirmations.state(ctx.ep_dir, state_lines) if ctx.ep_dir else {})},
        "warnings": warnings,
        **(extra or {}),
    }


async def _scripting(method: str, call: Call, project_id: str, episode: int, dry_run: bool) -> httpx.Response:
    params = {"episode": episode, **(call.query or {}), **({"dry_run": "true"} if dry_run else {})}
    try:
        return await downstream.call("scripting", method, call.path, params=params, json=call.json)
    except httpx.RequestError as e:
        raise OpError(502, f"scripting-agent に接続できません: {e}")


async def run_line_op(project_id: str, episode: int, op: str, body: dict | None,
                      dry_run: bool = False) -> dict:
    """行の操作を1つ実行する（`dry_run=True` は影響だけを返して何も変えない）。"""
    if op not in ALL_OPS:
        raise OpError(404, f"未知の操作です: {op}（{' / '.join(ALL_OPS)}）")
    body = dict(body or {})
    ep_dir = _ep_dir(project_id, episode)

    async with _lock(project_id, episode):
        before = read_docs(ep_dir)
        if before.primary is None:
            raise OpError(404, f"第{episode}話の台本が見つかりません")
        body = _resolve_orders(body, before.lines())         # order 指定も line_id に直す（履歴・応答・派生物は line_id で持つ）
        call = build_call(project_id, op, body, before.lines(), episode)
        basis = (lambda d: d.script_lines()) if call.basis == "script" else (lambda d: d.lines())
        before_lines = basis(before)

        if dry_run:
            res = await _scripting(call.method, call, project_id, episode, dry_run=True)
            if 400 <= res.status_code < 500:
                # できない操作は 200 で理由を返す（行モーダルがグレーアウトして理由を出せる）
                empty = OpContext(op=op, project_id=project_id, episode=episode, line_id=body.get("line_id"),
                                  params=body, before=[], after=[], diff=diff_lines([], []), ep_dir=ep_dir,
                                  dry_run=True)
                return {**_response(op, empty, dry_run=True, changed=False, warnings=[]),
                        "ok": False, "reason": downstream.error_detail(res)}
            if res.status_code >= 400:
                raise OpError(502, f"scripting-agent: {downstream.error_detail(res)}")
            preview = (res.json().get("preview") or {}).get("lines")
            if preview is None:
                raise OpError(502, "scripting-agent が dry_run のプレビューを返しませんでした（バージョン不一致）")
            ctx = _context(project_id, episode, op, body, before_lines, preview, ep_dir, dry_run=True)
            changed = preview != before_lines
            impact = {"script": _script_items(op, ctx) if changed else [item("keep", "変更なし")],
                      **(derivatives.describe_all(ctx) if changed else {})}
            extra = ({"adoption": res.json().get("adoption"),
                      # 採用で確定の運用が始まる話数か（始まる話数は、採用した行だけが未確定になる）
                      "starts_confirmations": not confirmations.enabled(ep_dir)} if op == ADOPT else None)
            return _response(op, ctx, dry_run=True, changed=changed, warnings=[], impact=impact,
                             state_lines=preview, extra=extra)

        # 最初の採用（正本がまだ無い）は戻す先が無い＝履歴に積まない（Undo は script.json を消せない）
        undoable = not (op == ADOPT and before.script is None)
        op_id = _begin(ep_dir, op, body, before) if undoable else None
        try:
            res = await _scripting(call.method, call, project_id, episode, dry_run=False)
        except OpError:
            if op_id:
                _discard(ep_dir, op_id)
            raise
        if res.status_code >= 400:
            if op_id:
                _discard(ep_dir, op_id)
            raise OpError(res.status_code, downstream.error_detail(res))

        after = read_docs(ep_dir)
        if before.same_as(after):
            if op_id:
                _discard(ep_dir, op_id)   # 何も変わらなかった操作は履歴に積まない（Undo が空振りしないように）
            ctx = _context(project_id, episode, op, body, before_lines, basis(after), ep_dir)
            return _response(op, ctx, dry_run=False, changed=False, warnings=[],
                             impact={"script": [item("keep", "変更なし")]})

        ctx = _context(project_id, episode, op, body, before_lines, basis(after), ep_dir)
        warnings: list[str] = []
        if op == ADOPT and not confirmations.enabled(ep_dir):
            # 採用で確定の運用が始まる（ユーザー判断 2026-09-29）。採用の**前**の台本を基準にするので、
            # 既存の台本があれば全行確定済みから始まり、採用した行だけが未確定になる。最初の採用は空から
            try:
                confirmations.start(ep_dir, before.script_lines() or None, baseline=before.script is not None)
            except Exception as e:  # noqa: BLE001 — 運用の開始に失敗しても採用は確定済み
                warnings.append(f"確定の運用を始められませんでした（採用は確定済み）: {type(e).__name__}: {e}")
        # 台本の変更はここで確定している。以降の失敗は warnings に載せるだけ（I3）
        if op_id:
            _commit(ep_dir, op_id, after, ctx.diff)
        else:
            _write_head(ep_dir, after, None)
        applied, apply_warnings = await derivatives.apply_all(ctx)
        warnings += apply_warnings
        if warnings and op_id:
            _update_meta(ep_dir, op_id, warnings=warnings)
        _audit(project_id, episode, op, ctx, warnings, op_id)
        extra = {"op_id": op_id, "applied": applied}
        if op == ADOPT:
            extra["adoption"] = res.json().get("adoption")
            extra["undoable"] = undoable
        return _response(op, ctx, dry_run=False, changed=True, warnings=warnings, extra=extra)


async def undo_line_op(project_id: str, episode: int, force: bool = False) -> dict:
    """直前の窓口の操作を1つ戻す。台本（ドラフトと確定版を別々に）を書き戻し、派生物の後処理を
    再実行する＝外した音声・コマは台本に戻った行へそのまま戻る（I5）。行の確定・作り直した音声は
    Undo の対象外（台本の変更だけを戻す）。"""
    ep_dir = _ep_dir(project_id, episode)
    async with _lock(project_id, episode):
        metas = [m for m in _metas(ep_dir) if m.get("status") == "committed" and not m.get("undone")]
        if not metas:
            raise OpError(404, "戻せる操作がありません")
        meta = metas[-1]
        snap_file = _ops_dir(ep_dir) / f"{meta['op_id']}.before.json"
        if not snap_file.exists():
            raise OpError(410, f"操作 {meta['op_id']} のスナップショットが見つかりません")
        snap = json.loads(snap_file.read_text(encoding="utf-8"))

        now = read_docs(ep_dir)
        head = _load(_ops_dir(ep_dir) / "_head.json")
        if not force and head and head.get("sha") != now.sha():
            raise OpError(409, "台本が窓口の最後の操作の後に別の経路で変更されています。"
                               "戻すとその変更も消えます（force=true で強制的に戻せます）")

        payload = {k: v for k, v in snap.items() if v is not None}
        try:
            res = await downstream.call(
                "scripting", "POST", f"/projects/{project_id}/episodes/{episode}/script/restore", json=payload)
        except httpx.RequestError as e:
            raise OpError(502, f"scripting-agent に接続できません: {e}")
        if res.status_code >= 400:
            raise OpError(res.status_code, downstream.error_detail(res))

        after = read_docs(ep_dir)
        hints = {"split_front_line_ids": [meta["line_id"]] if meta["op"] in ("split", "split-apply") and meta.get("line_id") else []}
        body = {"line_id": meta.get("line_id")}
        undo_basis = (lambda d: d.script_lines()) if meta["op"] == ADOPT else (lambda d: d.lines())
        ctx = _context(project_id, episode, "undo", body, undo_basis(now), undo_basis(after), ep_dir, hints=hints)
        applied, warnings = await derivatives.apply_all(ctx)

        _update_meta(ep_dir, meta["op_id"], undone=True, undone_at=datetime.now(timezone.utc).isoformat())
        _write_head(ep_dir, after, meta["op_id"])
        _audit(project_id, episode, "undo", ctx, warnings, meta["op_id"])
        return _response("undo", ctx, dry_run=False, changed=not now.same_as(after), warnings=warnings, extra={
            "undone_op": meta["op"], "undone_op_id": meta["op_id"], "applied": applied,
            "restored_line_ids": ctx.diff["new_ids"], "removed_line_ids": ctx.diff["removed_ids"],
            "impact": {"script": [item("auto", f"台本の変更「{_OP_LABELS.get(meta['op'], meta['op'])}」を1つ戻した"
                                               "（音声とコマも台本に合わせて戻る）")]},
        })


def _audit(project_id: str, episode: int, op: str, ctx: OpContext, warnings: list[str], op_id: str) -> None:
    """窓口の操作を監査ログへ。後処理の失敗は台本の確定と無関係に、ここへ残す（I3）。"""
    try:
        project_manager.append_director_log(project_id, {
            "action": "line-op", "op": op, "episode": episode, "line_id": ctx.line_id, "op_id": op_id,
            "new_line_ids": ctx.diff["new_ids"], "removed_line_ids": ctx.diff["removed_ids"],
            "warnings": warnings,
        })
    except Exception:  # noqa: BLE001 — 監査ログの失敗で操作の応答を落とさない
        pass
