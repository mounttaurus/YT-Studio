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
import subprocess
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

# 組版ロジック本体（build_plan.py と同じ入れ方）。plan_builder は build_plan ジョブで使う
sys.path.insert(0, os.path.join(PSASSIST_ROOT, "psassist-agent"))

# Phase 5 で組版の全工程を載せた。director はこの文字列だけを知る（§2-6）。
# ★Photoshop を占有するものと、しないものを分けて持つ。UI が警告を出し分けるため。
CAPABILITIES = ["build_plan", "cutout", "build_panel", "qa_check", "export_png"]
NEEDS_PHOTOSHOP = {"cutout", "build_panel", "export_png"}
JOB_KINDS = set(CAPABILITIES)
DEFAULT_INTERVAL = 1.5

# P2b（AROLL_TAB_REDESIGN_PLAN.md §6-d）: build_panel を何行ずつに割って
# `bridge.py --lines` を繰り返し呼ぶか。チャンクの切れ目でだけ中断を確認する
# （Photoshop はチャンク内では単一プロセスの整合を保つが、途中で kill すると壊れる）。
DEFAULT_CHUNK_SIZE = 20

_EP_RE = re.compile(r"^ep(\d+)$")
# bridge.py / batch_cutout.py の進捗行 "  [  3/196] …" を拾う（P2a）。
_PROGRESS_RE = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: str, data: dict) -> None:
    """読み手（director-agent）が書きかけを掴まないよう、tmp→replace で書く。

    ⚠️ **Windows の `os.replace` は一時的に `PermissionError` を返すことがある**
    （読み手が開いた瞬間・ウイルス対策が .tmp を掴んだ瞬間など）。実際に
    ハートビートの書き込みで `WinError 5` が出て worker ごと落ちた（2026-08-30）。
    数回リトライし、それでも駄目なら例外を上げる（呼び出し側が握るかを決める）。
    """
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    last: Exception | None = None
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:      # 掴まれている。少し待って再試行
            last = e
            time.sleep(0.2 * (attempt + 1))
    raise last  # type: ignore[misc]


# ── ユーザー資産の起動時チェック ────────────────────────────────────────

def check_user_assets() -> list[str]:
    """組版に要るユーザー資産（非公開・チャンネル固有）の実在を起動時に確認する。

    ⚠️ **どちらも「無くても動く」設計だが、動き方が違う**:
      - `bubbles.psd` が無いと `build_panel` ジョブだけが実行時エラーで失敗する
        （`cutout`/`qa_check`等は無関係に動く）。原因は既存のガード
        （`run_build_panel_job`）が正しく説明するが、**ジョブを投げるまで気づけない**。
        新しい起動経路を試すたびに同じ穴で失敗を再発していた
        （`Docs/AROLL_PSASSIST_REFACTOR_PLAN.md` S4参照）。
      - `speaker_defaults.json` が無いと**エラーにならず**全行が
        `spec.FALLBACK_DEFAULT`（`rect_a`・左・`UNKNOWN_SPEAKER`警告）になる。
        エラーが出ない分、気づくのがもっと遅れる（2026-09-02実測: 本番31行全滅で発覚）。
    どちらも起動直後に一度だけ知らせるだけで、常駐自体は止めない
    （`cutout`/`qa_check`等の他ジョブは資産が無くても動くため）。
    """
    bubbles = (os.environ.get("PSA_BUBBLES_PSD") or "").strip() or \
        os.path.join(PSASSIST_ROOT, "assets", "bubbles.psd")
    speaker_defaults = (os.environ.get("PSA_SPEAKER_DEFAULTS") or "").strip() or \
        os.path.join(PSASSIST_ROOT, "assets", "speaker_defaults.json")

    warnings: list[str] = []
    if not os.path.exists(bubbles):
        warnings.append(
            "[!] bubbles.psd が見つかりません: %s\n"
            "    build_panel ジョブだけが失敗します（cutout/qa_check等は無関係）。\n"
            "    資産を持つチェックアウトから --shared で起動するか、\n"
            "    ルート .env の PSA_BUBBLES_PSD で場所を指定してください。" % bubbles
        )
    if not os.path.exists(speaker_defaults):
        warnings.append(
            "[!] speaker_defaults.json が見つかりません: %s\n"
            "    エラーにはなりませんが、全行が既定（rect_a・左・UNKNOWN_SPEAKER警告）\n"
            "    になります（話者ごとの吹き出し使い分けが効きません）。\n"
            "    資産を持つチェックアウトからコピーするか、ルート .env の\n"
            "    PSA_SPEAKER_DEFAULTS で場所を指定してください。" % speaker_defaults
        )
    return warnings


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


def _cancel_marker_path(ep_dir: str, job_id: str) -> str:
    return os.path.join(ep_dir, "psassist", "jobs", "cancel", job_id)


def is_cancel_requested(ep_dir: str, job_id: str) -> bool:
    """director が `POST …/psassist/jobs/{job_id}/cancel` で置くマーカーの有無。"""
    return os.path.exists(_cancel_marker_path(ep_dir, job_id))


def clear_cancel_marker(ep_dir: str, job_id: str) -> None:
    try:
        os.remove(_cancel_marker_path(ep_dir, job_id))
    except OSError:
        pass


def _run_script(argv: list[str], env_extra: dict, log: list[str], label: str,
                 on_line=None) -> int:
    """psassist の CLI スクリプトを子プロセスで回す。

    ⚠️ **Photoshop を触るスクリプトは import して呼ばない。** モジュール先頭で
    `PSA_EPISODE_DIR` を読む作りで、しかも win32com の状態を持つ。1プロセスで
    複数エピソードを回す worker から import すると env の付け替えが効かない。
    子プロセスなら env をそのジョブ用に閉じ込められる。

    `on_line`（P2a）: 標準出力を1行読むごとに呼ぶ。進捗行（`[ n/N]`）の抽出と
    state.json への反映は呼び出し側（process_job）の責務にする
    （ここは子プロセスを回すことだけに専念する）。
    """
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(argv, cwd=PSASSIST_ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1)
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            print("    | %s" % line)
            log.append(line)
            if on_line:
                on_line(line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("%s が失敗しました（exit %d）" % (label, proc.returncode))
    return proc.returncode


def run_build_plan_job(ep_dir: str, job: dict, log: list[str]) -> dict:
    """工程1: panel_plan.json を作る（Photoshop 不要）。

    ★`Paths.from_env()` を使わない。worker は複数エピソードを1プロセスで回すので、
      env 依存にするとどの話数のプランを作っているか分からなくなる。明示的に組む。
    """
    from app.core import plan_builder  # noqa: PLC0415  （子プロセスにしないので遅延import）

    # ep_dir = <shared>/projects/<pid>/episodes/epNN → 4段上が <shared>
    shared = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(ep_dir))))
    paths = plan_builder.Paths(
        episode_dir=ep_dir,
        backgrounds_dir=(os.environ.get("PSA_BACKGROUNDS_DIR") or "").strip()
                        or os.path.join(shared, "backgrounds"),
        bubbles_psd=(os.environ.get("PSA_BUBBLES_PSD") or "").strip()
                    or plan_builder.DEFAULT_BUBBLES_PSD,
        out_dir=os.path.join(ep_dir, "psassist"),
    )
    plan = plan_builder.build(paths)
    path = plan_builder.write(plan, paths.out_dir)
    panels = plan.get("panels", [])
    need = [p for p in panels if p.get("needs_attention")]
    log.append("パネル %d 件 → %s" % (len(panels), path))
    if need:
        log.append("人の判断が要る行: %d 件" % len(need))
    return {"panels": len(panels), "needs_attention": len(need), "plan": path}


def run_qa_check_job(ep_dir: str, job: dict, log: list[str]) -> dict:
    """工程5: 合成結果の検査（Photoshop 不要・PSDの合成プレビューを読む）。

    lines を指定すればその行だけ。省略で全件。
    """
    ctx = qa_check.build_ctx(ep_dir)
    if ctx is None:
        raise RuntimeError("検査の準備ができません（psd_final が見つかりません）")
    lines = job.get("lines") or []
    if lines:
        files = ["panel_%s.psd" % lid for lid in lines]
        files = [f for f in files if os.path.exists(os.path.join(ctx["psd_dir"], f))]
    else:
        # ⚠️ `run_pass(ctx, files)` は files を**必ず反復する**。None を渡せない
        #    （全件のつもりで None にして `'NoneType' object is not iterable` で落ちた）。
        #    全件検査は自分でファイル一覧を作る。
        files = sorted(f for f in os.listdir(ctx["psd_dir"])
                       if f.startswith("panel_") and f.endswith(".psd"))
    if not files:
        raise RuntimeError("対象のPSDが1枚もありません")
    rep = qa_check.run_pass(ctx, files, verbose=False)
    summary = (rep or {}).get("summary", {})
    # ⚠️ `run_pass` は**マージ後のレポート全体**を返す。`panels` の数は検査した枚数では
    #    ないので、そのまま「検査N件」と書くと1行だけ指定した時に196と出て嘘になる。
    checked = len(files)
    log.append("検査 %d 枚（レポート全体 %d 行）: %s"
               % (checked, len((rep or {}).get("panels", [])), summary))
    return {"checked": checked, "total": len((rep or {}).get("panels", [])),
            "summary": summary}


def run_cutout_job(ep_dir: str, job: dict, log: list[str], on_line=None) -> dict:
    """工程1(素材): 背景抜き。⚠️ Photoshop を占有する。"""
    _run_script([sys.executable, os.path.join("scripts", "batch_cutout.py")],
                {"PSA_EPISODE_DIR": ep_dir}, log, "batch_cutout", on_line=on_line)
    cut = os.path.join(ep_dir, "psassist", "cutout")
    n = len([f for f in os.listdir(cut)]) if os.path.isdir(cut) else 0
    return {"cutouts": n}


def run_build_panel_job(ep_dir: str, job: dict, log: list[str], on_line=None) -> dict:
    """工程2-4: バブル配置・セリフ流し込み・背景合成。⚠️ Photoshop を占有する。

    P2b（AROLL_TAB_REDESIGN_PLAN.md §6-d）: `--all` を一括で投げず、
    `--lines` を `DEFAULT_CHUNK_SIZE` 件ずつに割って `bridge.py` を繰り返し呼ぶ。
    チャンクの切れ目でだけ中断マーカーを見る（Photoshop単一プロセスの整合を
    保ったまま止められる場所がそこしかない）。**子プロセスを kill しない**
    ── 今のチャンクは最後まで終えてから止まる（Aロールの `arollStop` と同じ約束）。
    """
    plan_path = os.path.join(ep_dir, "psassist", "panel_plan.json")
    if not os.path.exists(plan_path):
        raise RuntimeError("panel_plan.json がありません（先に build_plan を実行してください）")
    with open(plan_path, encoding="utf-8") as fh:
        plan = json.load(fh)
    # ⚠️ **bubbles.psd は配布物に含まれない**（ユーザー自作の資産で、見た目がそのまま
    #    出力になるため意図的に非公開）。worker を公開リポの clone から起動すると
    #    ここが無く、JSX が `Expected a reference to an existing File/Folder` という
    #    原因の分からないエラーで落ちる。**先に見て、読める言葉で止める。**
    #    対処: 資産を持つチェックアウトから `--shared <稼働側>/shared` で起動する。
    bubbles = (os.environ.get("PSA_BUBBLES_PSD") or "").strip() or \
        os.path.join(PSASSIST_ROOT, "assets", "bubbles.psd")
    if not os.path.exists(bubbles):
        raise RuntimeError(
            "吹き出しテンプレート bubbles.psd が見つかりません: %s\n"
            "これは配布物に含まれないユーザー資産です。"
            "資産を持つチェックアウトから `--shared` で起動するか、"
            "ルート .env の PSA_BUBBLES_PSD で場所を指定してください。" % bubbles)
    # ⚠️ P1（AROLL_TAB_REDESIGN_PLAN.md §6-d）: bridge.py の既定出力先は `psd`、
    #    qa_check.build_ctx の既定読み込み先は `psd_final`。既存エピソードは
    #    手作業で両方に196枚を置いていたため露呈しなかったが、新規エピソードでは
    #    ③→④の間で必ず止まる。ここで明示して揃える。
    psd_final_dir = os.path.join(ep_dir, "psassist", "psd_final")

    lines = job.get("lines") or [p["line_id"] for p in plan.get("panels", [])]
    total = len(lines)
    job_args = job.get("args") or {}
    chunk_size = int(job_args.get("chunk_size") or DEFAULT_CHUNK_SIZE)
    resume = bool(job_args.get("resume"))
    job_id = job.get("job_id") or ""

    done_count = 0
    cancelled = False
    for i in range(0, total, chunk_size):
        chunk = lines[i:i + chunk_size]
        argv = [sys.executable, os.path.join("host-bridge", "bridge.py"), "--plan", plan_path,
                "--bubbles", bubbles, "--out", psd_final_dir, "--lines", ",".join(chunk)]
        if resume:
            argv.append("--resume")
        _run_script(argv, {"PSA_EPISODE_DIR": ep_dir}, log, "bridge", on_line=on_line)
        done_count += len(chunk)
        if job_id and is_cancel_requested(ep_dir, job_id):
            clear_cancel_marker(ep_dir, job_id)
            cancelled = True
            log.append("中断マーカーを検出しました（%d/%d 完了）。次のチャンクへは進みません。"
                       % (done_count, total))
            break

    n = len([f for f in os.listdir(psd_final_dir) if f.endswith(".psd")]) if os.path.isdir(psd_final_dir) else 0
    result = {"psd": n, "lines": done_count, "total": total}
    if cancelled:
        result["cancelled"] = True
    return result


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
    job["job_id"] = job_id  # run_build_panel_job が中断マーカーを引くのに使う
    kind = job.get("kind")
    label = "%s/ep%02d/%s" % (job.get("project_id", "?"), job.get("episode", 0) or 0, job_id)
    state_path = os.path.join(ep_dir, "psassist", "jobs", "state", "%s.json" % job_id)
    started_at = now_iso()

    write_json_atomic(state_path, {
        "job_id": job_id, "kind": kind, "status": "running", "log": [], "started_at": started_at,
    })
    # ⚠️ ここで queue/ を消す（director→host の一方通行・単一プロセスなので再処理の心配はない）。
    try:
        os.remove(job_path)
    except OSError:
        pass
    # ⚠️ **ここで中断マーカーを掃除しない。** job_id はタイムスタンプ+uuid4で
    #   ジョブごとに一意なので、この時点で既に置かれているマーカーは「実行開始と
    #   ほぼ同時に中断が押された」ものであり、消していい古いゴミではない。
    #   掃除すると「起動直後にキャンセルを押すと効かない」レースを生む。

    print("[job] %s (%s) 開始" % (label, kind))
    log: list[str] = []
    result: dict = {}

    # P2a: 子プロセスの出力から進捗（`[ n/N]`）を拾い、state.json へ逐次反映する。
    # ⚠️ 毎行 write すると I/O が重いので、1秒に1回程度へ絞る
    #   （`write_json_atomic` は既にリトライ付きだが、頻度そのものを抑える）。
    progress: dict = {}
    last_write = [0.0]

    def on_line(line: str) -> None:
        m = _PROGRESS_RE.search(line)
        if m:
            progress["done"] = int(m.group(1))
            progress["total"] = int(m.group(2))
            progress["current"] = line
        now = time.time()
        if now - last_write[0] < 1.0:
            return
        last_write[0] = now
        try:
            write_json_atomic(state_path, {
                "job_id": job_id, "kind": kind, "status": "running", "log": log,
                "started_at": started_at, "progress": dict(progress) if progress else None,
            })
        except OSError as e:
            print("  [progress] 書き込み失敗（次の更新で再試行）: %s" % e)

    try:
        if kind not in JOB_KINDS:
            raise ValueError("未対応のkind: %s" % kind)
        if kind == "export_png":
            result = run_export_png_job(ep_dir, job, log)
        elif kind == "build_plan":
            result = run_build_plan_job(ep_dir, job, log)
        elif kind == "qa_check":
            result = run_qa_check_job(ep_dir, job, log)
        elif kind == "cutout":
            result = run_cutout_job(ep_dir, job, log, on_line=on_line)
        elif kind == "build_panel":
            result = run_build_panel_job(ep_dir, job, log, on_line=on_line)
        status = "cancelled" if result.get("cancelled") else "done"
    except Exception as e:
        log.append("エラー: %s" % e)
        status = "failed"

    write_json_atomic(state_path, {
        "job_id": job_id, "kind": kind, "status": status, "log": log,
        "started_at": started_at, "finished_at": now_iso(), "result": result,
        "progress": dict(progress) if progress else None,
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

    asset_warnings = check_user_assets()
    if asset_warnings:
        print()
        for w in asset_warnings:
            print(w)

    print("\nCtrl+C で終了\n")

    try:
        while True:
            episodes = find_episodes(shared_dir)
            # ⚠️ **心拍が1回書けないくらいで常駐を殺さない。** 次の周期で書き直せば
            #    済むものに、動いているワーカーを落とす価値は無い（1周期落ちても
            #    director 側は 60秒の猶予を見て alive を判定する）。
            try:
                write_json_atomic(worker_file, {
                    "pid": pid, "started_at": started_at, "heartbeat_at": now_iso(),
                    "watching": len(episodes), "capabilities": CAPABILITIES,
                })
            except OSError as e:
                print("  [heartbeat] 書き込み失敗（次の周期で再試行）: %s" % e)

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
