"""行の確定（Docs/LINE_WORKBENCH_PLAN.md §4-2・W2）。

「確定」は手で変えた行を行ごとに「これでいい」と決める操作（D8-②）。保存するのは
**確定した時点の行の指紋だけ**で、確定済み／未確定は毎回比較して出す導出値（I4）:

    今の行の指紋 ＝ 確定時の指紋 → 確定済み ／ 違う・記録なし → 未確定

だから import・Undo・MCP・旧タブ・LLM再生成のどの経路で台本が変わっても、書き込み口ごとに
「未確定に戻す」処理を置かなくても自動で未確定になる。外れた行の記録は残す（Undo で行が戻れば
確定済みへ戻れる）。指紋関数は `line_fingerprint` の1つ（将来の翻訳の「訳が古いか」も同じ）。

**保存先は `episodes/epNN/confirmations.json`**（`script.json` の metadata には置かない。台本の
丸ごと書き戻し＝import/Undo で消える・巻き戻る 2026-09-28 の事故と同じ穴になるため）。

**確定の運用は話数ごとに始まる**（ユーザー判断 2026-09-29）: `confirmations.json` が無い話数は
「運用外」で、確定ゲート（組版のスキップ等）は一切かからず従来どおり動く。運用は
①`LLMの案を採用`（`line_ops` の adopt）か ②`start(baseline=True)`（今の台本を全行確定済みとして
始める）のどちらかで始まる。本番の既存話数は、ユーザーが始めるまで何も変わらない。

台本の正本は `script.json`（ドラフトではない）。本文が空の行は「まだ作る対象ではない」ので
未確定に数えず、確定もできない（`skipped_empty`）。

純粋な読み書き（HTTPなし）。書き手は director だけ。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.line_fingerprint import FINGERPRINT_FIELDS, line_fields, line_fingerprint

FILE = "confirmations.json"
SCHEMA_VERSION = "1.0.0"

CONFIRMED, UNCONFIRMED, EMPTY = "confirmed", "unconfirmed", "empty"


def _path(ep_dir: Path) -> Path:
    return ep_dir / FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read(ep_dir: Path) -> dict | None:
    """確認の記録。無ければ None（＝この話数は確定の運用外）。壊れていたら例外
    （黙って運用外に戻すと、確定ゲートが勝手に外れる）。"""
    f = _path(ep_dir)
    if not f.exists():
        return None
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{FILE} が壊れています（{e}）。手で直すか、消して運用を始め直してください") from e
    doc.setdefault("lines", {})
    return doc


def enabled(ep_dir: Path | None) -> bool:
    return ep_dir is not None and _path(ep_dir).exists()


def script_lines(ep_dir: Path) -> list[dict] | None:
    """確定の基準になる台本の行（正本 script.json）。無ければ None。"""
    f = ep_dir / "script.json"
    if not f.exists():
        return None
    return list(json.loads(f.read_text(encoding="utf-8")).get("lines", []))


def _write(ep_dir: Path, doc: dict) -> None:
    f = _path(ep_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def _record(line: dict, now: str) -> dict:
    # fields は人が読む・「何が変わったか」を出すための写し（比較は fingerprint だけで行う）
    return {"fingerprint": line_fingerprint(line), "fields": line_fields(line), "confirmed_at": now}


def start(ep_dir: Path, lines: list[dict] | None, baseline: bool) -> dict:
    """確定の運用を始める。既に始まっていれば何もしない（`started: False`）。

    baseline=True … 今の台本を**全行確定済み**として記録して始める（既存話数の導入・
    採用の前の台本を基準にする時）。False … 空で始める（全行が未確定。新しい台本の最初の採用）。
    """
    if enabled(ep_dir):
        return {"started": False, "baseline": bool((read(ep_dir) or {}).get("baseline"))}
    now = _now()
    doc = {"schema_version": SCHEMA_VERSION, "started_at": now, "baseline": bool(baseline),
           "fingerprint_fields": list(FINGERPRINT_FIELDS), "lines": {}}
    if baseline:
        for l in lines or []:
            if (l.get("text") or "").strip():
                doc["lines"][l["id"]] = _record(l, now)
    _write(ep_dir, doc)
    return {"started": True, "baseline": bool(baseline), "recorded": len(doc["lines"])}


def line_states(lines: list[dict], doc: dict) -> dict[str, str]:
    """{line_id: confirmed | unconfirmed | empty}。今の指紋と記録の指紋の比較だけで決まる。"""
    rec = (doc or {}).get("lines", {})
    out = {}
    for l in lines:
        lid = l.get("id")
        if not (l.get("text") or "").strip():
            out[lid] = EMPTY
        elif lid in rec and rec[lid].get("fingerprint") == line_fingerprint(l):
            out[lid] = CONFIRMED
        else:
            out[lid] = UNCONFIRMED
    return out


def unconfirmed_ids(lines: list[dict], doc: dict) -> list[str]:
    return [lid for lid, s in line_states(lines, doc).items() if s == UNCONFIRMED]


def confirmed_ids(lines: list[dict], doc: dict) -> list[str]:
    return [lid for lid, s in line_states(lines, doc).items() if s == CONFIRMED]


def confirm(ep_dir: Path, lines: list[dict], line_ids: list[str] | None) -> dict:
    """行を確定する（指紋を記録する）。**確定点**＝後処理（音声・絵の下ごしらえ）の前に書く。

    line_ids=None は「今の未確定すべて」。既に確定済みの行は記録を触らない（`already`）。
    台本に無い行は `unknown`、本文が空の行は `skipped_empty`。
    """
    doc = read(ep_dir)
    if doc is None:
        raise KeyError("confirmations.json がありません（確定の運用が始まっていません）")
    by_id = {l.get("id"): l for l in lines}
    states = line_states(lines, doc)
    wanted = [lid for lid, s in states.items() if s == UNCONFIRMED] if line_ids is None else list(line_ids)

    now = _now()
    confirmed, already, unknown, empty = [], [], [], []
    for lid in dict.fromkeys(wanted):
        if lid not in by_id:
            unknown.append(lid)
        elif states[lid] == EMPTY:
            empty.append(lid)
        elif states[lid] == CONFIRMED:
            already.append(lid)
        else:
            confirmed.append(lid)
    if unknown:
        # 存在しない行が混じっていたら1行も確定しない（一部だけ確定して呼び出し側を混乱させない）
        return {"confirmed": [], "already": already, "unknown": unknown, "skipped_empty": empty}
    for lid in confirmed:
        doc["lines"][lid] = _record(by_id[lid], now)
    if confirmed:
        _write(ep_dir, doc)
    return {"confirmed": confirmed, "already": already, "unknown": unknown, "skipped_empty": empty}


def summary(ep_dir: Path | None) -> dict:
    """話数の確定の要約（episodes 一覧・状態帯用）。運用外は `{"enabled": False}`。"""
    if not enabled(ep_dir):
        return {"enabled": False}
    lines = script_lines(ep_dir)
    doc = read(ep_dir)
    if lines is None:
        return {"enabled": True, "has_script": False, "total": 0, "confirmed": 0, "unconfirmed": 0}
    st = line_states(lines, doc)
    n_unconf = sum(1 for s in st.values() if s == UNCONFIRMED)
    return {"enabled": True, "has_script": True,
            "total": sum(1 for s in st.values() if s != EMPTY),
            "confirmed": sum(1 for s in st.values() if s == CONFIRMED),
            "unconfirmed": n_unconf}


def state(ep_dir: Path | None, lines: list[dict] | None = None) -> dict:
    """窓口の応答に載せる確定の状態。運用外は `{"confirmation_enabled": False}`。
    `lines` を渡すとその行で判定する（dry_run は操作後の行を渡す）。省略は今の正本 script.json。"""
    if not enabled(ep_dir):
        return {"confirmation_enabled": False}
    if lines is None:
        lines = script_lines(ep_dir) or []
    return {"confirmation_enabled": True, "unconfirmed_line_ids": unconfirmed_ids(lines, read(ep_dir))}
