"""Aロールの「同じ絵の繰り返し」の検査・直しの窓口側（Docs/AROLL_DUPLICATE_CHECK_PLAN.md）。

判定と直しの本体は scrapping-agent（`aroll_manager.duplicate_report` ほか）。ここは director にしか
できない仕事だけを持つ: **「✋ 手直し済み」の行を判定して渡す**（PSD の中身で決まるので scrapping は
知らない・`psd_records`）。手直し済みの行は差し替えると手直しが消えるので、検査も直しも対象にしない。
"""
from __future__ import annotations

from app.core import downstream, line_ops, workbench_view

DEFAULT_WINDOW = 30


def edited_line_ids(view: dict) -> list[str]:
    """✋ 手直し済みの行（`workbench_view.build_view` の結果から）。"""
    return [l["id"] for l in view.get("lines") or [] if (l.get("final") or {}).get("edited")]


def _base(project_id: str, episode: int) -> str:
    return f"/projects/{project_id}/episodes/{episode}/aroll"


async def duplicate_report(project_id: str, episode: int, view: dict | None = None,
                           window: int = DEFAULT_WINDOW) -> dict:
    """検査（読み取りのみ・無料）。`view` を渡せば作り直さない（audit が既に持っている時）。"""
    if view is None:
        view = await workbench_view.build_view(project_id, episode)
    protect = edited_line_ids(view)
    try:
        res = await downstream.call("scrapping", "GET", f"{_base(project_id, episode)}/duplicates",
                                    params={"protect": protect, "window": window}, timeout=60.0)
    except Exception as e:
        raise line_ops.OpError(502, f"scrapping-agent に繋がりません（{type(e).__name__}）")
    if res.status_code >= 400:
        raise line_ops.OpError(res.status_code, downstream.error_detail(res))
    out = res.json()
    out["protected_line_ids"] = protect
    return out


async def _post(project_id: str, episode: int, path: str, body: dict, timeout: float = 120.0) -> dict:
    try:
        res = await downstream.call("scrapping", "POST", f"{_base(project_id, episode)}/{path}",
                                    json=body, timeout=timeout)
    except Exception as e:
        raise line_ops.OpError(502, f"scrapping-agent に繋がりません（{type(e).__name__}）")
    if res.status_code >= 400:
        raise line_ops.OpError(res.status_code, downstream.error_detail(res))
    return res.json()


async def fix_duplicates(project_id: str, episode: int, mode: str = "reselect", apply: bool = False,
                         line_ids: list[str] | None = None, window: int = DEFAULT_WINDOW,
                         view: dict | None = None) -> dict:
    """検査の指摘を直す。**既定は案だけ（apply=False）**。✋手直し済みの行は director がここで守る。

    mode: reselect（この話数で未使用の絵へ選び直す・無料）／unassign（替えが無い行の絵を外して
    未決定にする・無料。その後は応答の `changed` の行だけを「残りを生成」の見積もり・確認で生成する。
    ⚠️ 「在庫で埋める」は使わない＝在庫が尽きた行へ選択の段3が同じ絵を再使用で当て直す）。
    ここでは生成しない（課金は既存の見積もりと確認に任せる）。Undo は `undo_duplicate_fix`。
    """
    if view is None:
        view = await workbench_view.build_view(project_id, episode)
    body = {"mode": mode, "apply": bool(apply), "window": window, "protected_line_ids": edited_line_ids(view)}
    if line_ids is not None:
        body["line_ids"] = line_ids
    return await _post(project_id, episode, "duplicates/fix", body)


async def undo_duplicate_fix(project_id: str, episode: int, fix_id: str | None = None) -> dict:
    """直前（または fix_id）の「重複を直した」を戻す（絵の割当と確定）。その後に変わった行は戻さない。"""
    return await _post(project_id, episode, "duplicates/undo", {"fix_id": fix_id} if fix_id else {})
