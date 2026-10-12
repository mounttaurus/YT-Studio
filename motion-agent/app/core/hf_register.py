"""描いたインサートを Bロールの箱へ入れる（Docs/MOTION_UNIFIED_PLAN.md §11-1・I1の完遂／Docs/BROLL_PLAN.md §8-2）。

流れ: コンポジションを書く → 検査 → 静止画 → 描画（明滅の実測に合格）→ **ここ**: scrapping-agent の
`POST …/broll/register` を呼ぶ（箱に入れるのは scrapping-agent だけ＝M4・BR3）。

渡すもの: 描いた out.mp4（shared 相対）・`source.kind:"motion"`・`license:"own"`・行（fit.line_id か引数）・
コンポジションの写し（plan_dir＝HTML・ブロックの写し・その場の部品・motion.json）・使った素材の asset_id（derived_from）・
作り直しなら前に入れた asset_id（replaces）・poster_sec・slug。
入ったら motion.json に `last_registered`（asset_id・描いた時の秒・ジョブ）を残す＝作り直しの `replaces` と尺ずれの判定に使う。

箱に入れないもの（M6）: 明滅の実測に落ちた描画／音を宣言しているのに音が無い描画／描いた後にコンポジションが直された描画
（`source_sha` が今と違う＝見ている絵と箱の絵がずれる）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from app.core import config, hf, hf_checks, jobs


class RegisterError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422, detail=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail      # scrapping-agent が返した detail（{code, message}）をそのまま


def _rel(p: Path) -> str:
    return p.resolve().relative_to(config.SHARED_DIR.resolve()).as_posix()


def _post_register(url: str, body: dict) -> dict:
    try:
        r = httpx.post(url, json=body, timeout=config.BROLL_REGISTER_TIMEOUT_SEC)
    except httpx.RequestError as e:
        raise RegisterError("scrapping_unreachable", f"scrapping-agent に繋がらない（{url}）: {e}", 502) from e
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:300]
        msg = detail.get("message") if isinstance(detail, dict) else detail
        raise RegisterError("scrapping_rejected", f"箱への登録が拒否された（{r.status_code}）: {msg}", 502, detail=detail)
    return r.json()


def _pick_job(target: str, job_id: str | None) -> dict:
    job = jobs.get(job_id) if job_id else jobs.latest_done_render(target)
    if job is None:
        raise RegisterError("no_render", f"{target} の描画（done）が無い。motion_hf_render で描いてから", 404)
    if (job.get("composition") != target or job.get("kind") != "render" or job.get("engine") != "hyperframes"
            or job.get("status") != "done"):
        raise RegisterError("bad_job", f"このジョブは {target} の終わった本描画ではない", 422)
    return job


def register(project_id: str, episode: int, comp_id: str, job_id: str | None = None, line_id: str | None = None,
             assign: bool = True, post=None) -> dict:
    """描画済みのインサートを箱へ入れる。
    返り値: {asset_id, duplicate, assigned, replaces, job_id, target, line_id}。
    assign=False なら箱に入れるだけで、行には当てない（行が決まっていない・後で当てる時）。"""
    target = f"projects/{project_id}/{episode}/{comp_id}"
    d = hf.existing(target)
    motion = hf.read_motion(d) or {}
    job = _pick_job(target, job_id)
    result = job.get("result") or {}
    if (result.get("flash") or {}).get("ok") is not True:
        raise RegisterError("flash_failed", "明滅の実測に合格していない描画は箱に入れない（M6）。直して描き直す", 422)
    audio = result.get("audio")
    if audio is not None and audio.get("ok") is False:
        raise RegisterError("audio_missing", "音を宣言しているのに mp4 に音が入っていない。描き直す", 422)
    out = config.SHARED_DIR / (result.get("file") or "")
    if not result.get("file") or not out.is_file():
        raise RegisterError("no_file", f"描いた mp4 が無い: {result.get('file')}", 404)
    want = result.get("source_sha")
    if want is not None:
        hf.prepare(d)   # motion.json の変更（効果音・見た目）を index.html へ反映した状態で比べる＝描く時と同じ導出
        if hf.source_sha(d) != want:
            raise RegisterError("stale_render", "この描画の後にコンポジションが直されている（箱の絵と今の絵がずれる）。"
                                                "motion_hf_render で描き直してから入れる", 409)

    fit = motion.get("fit") or {}
    line = (line_id or fit.get("line_id")) if assign else None
    mats = motion.get("materials") if isinstance(motion.get("materials"), dict) else {}
    derived = sorted({m["asset_id"] for m in mats.values() if isinstance(m, dict) and m.get("asset_id")})
    replaces = (motion.get("last_registered") or {}).get("asset_id")
    duration = hf_checks.root_info((d / "index.html").read_text(encoding="utf-8", errors="replace"))["duration"]
    body: dict = {
        "path": _rel(out), "license": "own", "source": {"kind": "motion"}, "added_by": "motion",
        "slug": motion.get("slug") or comp_id,
        "motion": {"engine": "hyperframes", "plan_dir": _rel(d), "fit": fit or None, "composition": comp_id,
                   "duration": duration},
        "note": (motion.get("notes") or "")[:200],
    }
    if motion.get("poster_sec") is not None:
        body["poster_sec"] = motion["poster_sec"]
    if line:
        body["line_id"] = line
    if derived:
        body["derived_from"] = derived
    if replaces:
        body["replaces"] = replaces
    url = f"{config.SCRAPPING_AGENT_URL.rstrip('/')}/projects/{project_id}/episodes/{episode}/broll/register"
    send = post or _post_register
    dropped = None
    try:
        res = send(url, body)
    except RegisterError as e:
        d_ = e.detail if isinstance(e.detail, dict) else {}
        if replaces and d_.get("code") == "unknown_asset" and replaces in str(d_.get("message", "")):
            # 前に入れた素材が箱から外れている（_trash）。古い方の割り当ての差し替えは諦めて、新しく入れる
            body.pop("replaces")
            dropped, replaces = replaces, None
            res = send(url, body)
        else:
            raise
    asset_id = (res.get("asset") or {}).get("asset_id")
    if asset_id:
        _remember(d, {"asset_id": asset_id, "rendered_sec": duration, "job_id": job["job_id"], "at": jobs._now()})
    out = {"asset_id": asset_id, "duplicate": bool(res.get("duplicate")), "assigned": res.get("assigned"),
           "replaces": replaces if (replaces and replaces != asset_id) else None, "job_id": job["job_id"],
           "target": target, "line_id": line}
    if dropped:
        out["replaces_dropped"] = dropped   # 古い素材が箱に無かったので、差し替えはしていない
    return out


def _remember(d: Path, last: dict) -> None:
    """motion.json に last_registered を足す（他の項目は触らない・一時ファイル経由）。"""
    p = d / "motion.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    data["last_registered"] = last
    tmp = p.with_name("motion.json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
