"""ホスト常駐ワーカー — 全プロジェクト・全エピソードの psassist/ を1プロセスで見張る.

`Docs/AROLL_TAB_REDESIGN_PLAN.md` Phase 0（ホスト工程ブリッジ）の実装。
**既存の `qa_check.py --watch` を置き換える**（機能を内包する。2つ常駐させない）。

    python psassist/scripts/host_worker.py

引数なし。`HOST_SHARED_DIR`（ルート `.env`）配下の
`projects/*/episodes/ep*/psassist/` を毎周期グロブして全部見張る。
1台のマシンで動く前提なので、複数の `psassist/` を1プロセスに収める。

ループ（既定1.5秒間隔）:
  1. `shared/_psassist/worker.json` にハートビートを書く（プロジェクト非依存の1本）
  2. 全エピソードの `jobs/queue/` を横断し、**作成順に1件だけ**実行して `jobs/state/` へ書き戻す
  3. 各エピソードの `psd_final/` の PSD 保存を監視（直したものだけ即座に再検査）

★ステップ2で `export_png` ジョブを処理したエピソードは、同じ周期のステップ3を見送る。
  Photoshop は単一プロセスで、バッチ実行中に別の COM 操作を挟むと壊れる
  （`psassist/README.md` 罠）。他エピソードの監視は止めない。

★`export_png` は `--resume` を使わず、ジョブの `lines` を必ず明示して呼ぶ。
  `export_png.py --resume` は「PNGが存在すればスキップ」で mtime を見ておらず、
  直した行の再書き出しに効かないため。
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PSASSIST_ROOT = os.path.dirname(SCRIPTS_DIR)

sys.path.insert(0, SCRIPTS_DIR)
from _rootenv import load_root_env  # noqa: E402
import qa_check  # noqa: E402

load_root_env()

sys.path.insert(0, os.path.join(PSASSIST_ROOT, "host-bridge"))
import export_png as export_png_mod  # noqa: E402

CAPABILITIES = ["export_png"]  # Phase 0 はこれだけ。残りは Phase 5
JOB_KINDS = set(CAPABILITIES)
DEFAULT_INTERVAL = 1.5

_EP_RE = re.compile(r"^ep(\d+)$")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: str, data: dict) -> None:
    """読み手（director-agent）が書きかけを掴まないよう、tmp→replace で書く。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ── エピソードの発見 ────────────────────────────────────────────────────

def resolve_shared_dir(cli_value: str | None) -> tuple[str, str]:
    """見張る shared を決める。返り値は (絶対パス, 由来の説明)。

    ⚠️ **`HOST_SHARED_DIR` に固定しない。** psassist はホストのスクリプトなので、
    「開発リポのチェックアウトから、本番の shared に対して」動かす運用が実在する
    （既存の `PSA_EPISODE_DIR` が既にその形。実害: 開発リポの
    `HOST_SHARED_DIR=…\\YT-Studio\\shared` を見張ってしまい、本番 director が読む
    `…\\LUKAandAOI\\shared\\_psassist\\worker.json` が永遠に生まれず
    「ボタンが出ない」になった。2026-08-30）。

    優先順位（上が勝つ）:
      1. `--shared`         その場限り。`.env` を書き換えずに向き先を変えられる
      2. `PSA_SHARED_DIR`   psassist だけ別の shared を見張る時の恒久設定
      3. `HOST_SHARED_DIR`  既定（リポと shared が揃っている通常の環境）
    """
    for value, src in ((cli_value, "--shared"),
                       (os.environ.get("PSA_SHARED_DIR"), "PSA_SHARED_DIR"),
                       (os.environ.get("HOST_SHARED_DIR"), "HOST_SHARED_DIR")):
        v = (value or "").strip().strip('"')
        if v:
            return os.path.abspath(v), src
    return "", ""


def find_episodes(shared_dir: str) -> list[dict]:
    """`SHARED_DIR/projects/*/episodes/ep*/psassist/` を毎周期グロブする。"""
    pattern = os.path.join(shared_dir, "projects", "*", "episodes", "ep*", "psassist")
    out = []
    for psa in glob.glob(pattern):
        if not os.path.isdir(psa):
            continue
        ep_dir = os.path.dirname(psa)
        m = _EP_RE.match(os.path.basename(ep_dir))
        if not m:
            continue
        proj_dir = os.path.dirname(os.path.dirname(ep_dir))
        out.append({
            "ep_dir": ep_dir,
            "project_id": os.path.basename(proj_dir),
            "episode": int(m.group(1)),
        })
    return out


def ep_label(info: dict) -> str:
    return "%s/ep%02d" % (info["project_id"], info["episode"])


# ── PSD 保存監視（qa_check.watch() のロジックをプロジェクト横断に展開） ───

def psd_watch_pass(ctx: dict, state: dict) -> int:
    """1エピソード分の監視を1周期だけ進める。直したPSDの数を返す。"""
    psd_dir = ctx["psd_dir"]
    now = qa_check.snapshot_psd_dir(psd_dir)
    seen, pending = state["seen"], state["pending"]
    for fn, sig in now.items():
        if seen.get(fn) != sig:
            pending[fn] = sig  # 変わった。落ち着くまで待つ
    settled = [fn for fn in pending if pending.get(fn) == now.get(fn) and seen.get(fn) != now.get(fn)]
    for fn in settled:
        try:
            qa_check.run_pass(ctx, [fn], verbose=False)
        except Exception as e:
            print("  [watch] 検査エラー %s: %s" % (fn, e))
        seen[fn] = now[fn]
        pending.pop(fn, None)
    for fn in list(pending):
        if fn in now:
            pending[fn] = now[fn]
    return len(settled)


# ── ジョブキュー ────────────────────────────────────────────────────────

def pick_oldest_job(episodes: list[dict]) -> tuple[str, str] | None:
    """全エピソードの `jobs/queue/` を横断し、job_id が最も古い1件を返す。

    `job_id` は `YYYYMMDD-HHMMSS-xxxx` で始まるため、文字列比較がそのまま作成順になる。
    """
    best = None  # (ep_dir, path, job_id)
    for info in episodes:
        qdir = os.path.join(info["ep_dir"], "psassist", "jobs", "queue")
        if not os.path.isdir(qdir):
            continue
        for fn in os.listdir(qdir):
            if not fn.endswith(".json"):
                continue
            job_id = fn[:-5]
            if best is None or job_id < best[2]:
                best = (info["ep_dir"], os.path.join(qdir, fn), job_id)
    return (best[0], best[1]) if best else None


def run_export_png_job(ep_dir: str, job: dict, log: list[str]) -> dict:
    lines = job.get("lines") or []
    if not lines:
        raise ValueError("lines が空です（対象ゼロ）")
    psa = os.path.join(ep_dir, "psassist")
    psd_dir = os.path.join(psa, "psd_final")
    out_dir = os.path.join(psa, "export")
    os.makedirs(out_dir, exist_ok=True)

    export_jobs, missing = [], []
    for lid in lines:
        src = os.path.join(psd_dir, "panel_%s.psd" % lid)
        if not os.path.exists(src):
            missing.append(lid)
            continue
        export_jobs.append({
            "line_id": lid, "in_psd": src,
            "out_png": os.path.join(out_dir, "panel_%s.png" % lid),
            "width": export_png_mod.OUT_W, "height": export_png_mod.OUT_H,
        })
    if missing:
        log.append("PSDが無いので対象外: %s" % ", ".join(missing))

    results = export_png_mod.run(export_jobs) if export_jobs else []
    for r in results:
        mark = "OK" if r.get("ok") else "NG"
        log.append("%s %s%s" % (mark, r.get("line_id"), "" if r.get("ok") else (" " + str(r.get("error") or "")[:200])))
    ok = sum(1 for r in results if r.get("ok"))
    failed_lines = [r.get("line_id") for r in results if not r.get("ok")]

    # ★書き出し直後にその行だけ再検査する。export_png は PSD を変更しないので
    #   PSD保存監視（psd_watch_pass）では気付けない。ここで即座に更新しないと
    #   EXPORT_STALE / EXPORT_MISSING が古いまま残る。
    if export_jobs:
        ctx = qa_check.build_ctx(ep_dir)
        if ctx is not None:
            qa_check.run_pass(ctx, ["panel_%s.psd" % j["line_id"] for j in export_jobs], verbose=False)
            log.append("再検査 %d枚 完了" % len(export_jobs))

    return {"exported": ok, "failed": len(failed_lines) + len(missing),
            "missing": missing, "failed_lines": failed_lines}


def process_job(ep_dir: str, job_path: str) -> None:
    try:
        with open(job_path, encoding="utf-8") as fh:
            job = json.load(fh)
    except Exception as e:
        print("  [job] 読み込み失敗 %s: %s" % (job_path, e))
        try:
            os.remove(job_path)
        except OSError:
            pass
        return

    job_id = job.get("job_id") or os.path.splitext(os.path.basename(job_path))[0]
    kind = job.get("kind")
    label = "%s/ep%02d/%s" % (job.get("project_id", "?"), job.get("episode", 0) or 0, job_id)
    state_path = os.path.join(ep_dir, "psassist", "jobs", "state", "%s.json" % job_id)
    started_at = now_iso()

    write_json_atomic(state_path, {
        "job_id": job_id, "status": "running", "log": [], "started_at": started_at,
    })
    # ⚠️ ここで queue/ を消す（director→host の一方通行・単一プロセスなので再処理の心配はない）。
    try:
        os.remove(job_path)
    except OSError:
        pass

    print("[job] %s (%s) 開始" % (label, kind))
    log: list[str] = []
    result: dict = {}
    try:
        if kind not in JOB_KINDS:
            raise ValueError("未対応のkind: %s" % kind)
        if kind == "export_png":
            result = run_export_png_job(ep_dir, job, log)
        status = "done"
    except Exception as e:
        log.append("エラー: %s" % e)
        status = "failed"

    write_json_atomic(state_path, {
        "job_id": job_id, "status": status, "log": log,
        "started_at": started_at, "finished_at": now_iso(), "result": result,
    })
    print("[job] %s → %s  %s" % (label, status, result))


# ── メインループ ────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="ループ間隔（秒）")
    ap.add_argument("--shared", default=None,
                    help="見張る shared の絶対パス。省略時は PSA_SHARED_DIR → HOST_SHARED_DIR の順")
    args = ap.parse_args()

    shared_dir, src = resolve_shared_dir(args.shared)
    if not shared_dir:
        sys.exit(
            "見張り先の shared が決まりません。次のいずれかで指定してください:\n"
            "  --shared <絶対パス>            （その場限りの指定。.env を書き換えない）\n"
            "  ルート .env の PSA_SHARED_DIR   （psassist だけ別の shared を見る時）\n"
            "  ルート .env の HOST_SHARED_DIR （既定）"
        )
    if not os.path.isdir(shared_dir):
        sys.exit("見張り先が存在しません（%s）: %s" % (src, shared_dir))

    worker_file = os.path.join(shared_dir, "_psassist", "worker.json")
    pid = os.getpid()
    started_at = now_iso()

    watch_states: dict[str, dict] = {}   # ep_dir -> {"seen":..., "pending":...}

    print("psassist host_worker 起動  pid=%d" % pid)
    print("見張り先: %s%sprojects%s*%sepisodes%sep*%spsassist%s  （%s）"
          % (shared_dir, os.sep, os.sep, os.sep, os.sep, os.sep, os.sep, src))
    print("ハートビート: %s" % worker_file)
    # ★director が読むのはこのファイル。見張り先を間違えると「ボタンが出ない」になるだけで
    #   エラーにならないため、起動直後に見つけた数を必ず出す（0 なら向き先が違う）。
    found = find_episodes(shared_dir)
    print("見つかったエピソード: %d件%s"
          % (len(found), "" if found else "  ⚠️ 0件です。--shared で向き先を確認してください"))
    for info in found:
        print("  - %s" % ep_label(info))
    print("Ctrl+C で終了\n")

    try:
        while True:
            episodes = find_episodes(shared_dir)
            write_json_atomic(worker_file, {
                "pid": pid, "started_at": started_at, "heartbeat_at": now_iso(),
                "watching": len(episodes), "capabilities": CAPABILITIES,
            })

            # ── ジョブを1件だけ処理（作成順） ──
            picked = pick_oldest_job(episodes)
            job_ep_dir = picked[0] if picked else None
            if picked:
                process_job(*picked)

            # ── PSD保存監視。exportジョブを処理した直後の同エピソードは今回だけ見送る ──
            for info in episodes:
                ep_dir = info["ep_dir"]
                if ep_dir == job_ep_dir:
                    continue
                ctx = qa_check.build_ctx(ep_dir)
                if ctx is None:
                    continue
                state = watch_states.get(ep_dir)
                if state is None:
                    # 初見のエピソードは現状を基準点にする（既存分を検査済み扱いにはしない
                    # ── build_ctx/run_pass は別途 director backend 経由や手動実行で行う）
                    state = {"seen": qa_check.snapshot_psd_dir(ctx["psd_dir"]), "pending": {}}
                    watch_states[ep_dir] = state
                n = psd_watch_pass(ctx, state)
                if n:
                    print("[watch] %s  %d枚を再検査" % (ep_label(info), n))

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n終了します。")


if __name__ == "__main__":
    main()
