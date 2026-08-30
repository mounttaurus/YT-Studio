"""合成済みPSDを 1920×1080 の PNG として一括書き出しする（ホスト常駐・Photoshop 必須）.

★ロジックは持たない。JSX に値を注入して実行し、結果を返すだけ（bridge.py と同方針）。

⚠️ **Photoshop はユーザーが作業中のことがある。** 単一プロセスなのでバッチ中に
   別の COM 操作を挟めない。流す前に必ず本人に確認すること。
   実行中に開いていたドキュメントには触れず、終了時にアクティブを復元する。

    python host-bridge/export_png.py --episode <episode_dir> --all
    python host-bridge/export_png.py --episode <...> --lines line_121,line_168
    python host-bridge/export_png.py --episode <...> --all --resume
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

if __name__ == "__main__":
    # host_worker.py がモジュールとしてimportする時は再ラップしない（qa_check.py と同じ理由）。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSX_PATH = os.path.join(ROOT, "jsx", "export_png.jsx")

OUT_W, OUT_H = 1920, 1080


def run(jobs: list[dict]) -> list[dict]:
    import win32com.client

    with open(JSX_PATH, encoding="utf-8") as fh:
        jsx_body = fh.read()

    ps = win32com.client.Dispatch("Photoshop.Application")
    ps.DisplayDialogs = 3
    prev = ps.ActiveDocument if ps.Documents.Count else None
    if prev is not None:
        print("開いているドキュメント %d 件は触れません（終了時に復元）" % ps.Documents.Count)

    out = []
    t0 = time.time()
    for i, job in enumerate(jobs, 1):
        payload = "var JOB = %s;\n" % json.dumps(job, ensure_ascii=True)
        try:
            raw = ps.DoJavaScript(payload + jsx_body)
            res = json.loads(str(raw))
        except Exception as e:
            res = {"line_id": job["line_id"], "ok": False, "error": str(e)[:300]}
        out.append(res)
        mark = "OK " if res.get("ok") else "NG "
        note = res.get("error") or ("%s → %s" % (res.get("src_size"), res.get("out_size")))
        print("  [%3d/%3d] %s %-10s %s" % (i, len(jobs), mark, res.get("line_id"), note))

    if prev is not None:
        try:
            ps.ActiveDocument = prev
        except Exception:
            pass
    ok = sum(1 for r in out if r.get("ok"))
    print("\n成功 %d / %d   %.1f分" % (ok, len(out), (time.time() - t0) / 60))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", required=True)
    ap.add_argument("--psd-dir", default="psd_final")
    ap.add_argument("--lines", help="line_121,line_168 のようにカンマ区切り")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true", help="書き出し済みは飛ばす")
    ap.add_argument("--width", type=int, default=OUT_W)
    ap.add_argument("--height", type=int, default=OUT_H)
    args = ap.parse_args()

    psa = os.path.join(os.path.abspath(args.episode), "psassist")
    psd_dir = os.path.join(psa, args.psd_dir)
    out_dir = os.path.join(psa, "export")
    if not os.path.isdir(psd_dir):
        sys.exit("PSD が見つかりません: %s" % psd_dir)
    os.makedirs(out_dir, exist_ok=True)

    files = sorted(f for f in os.listdir(psd_dir) if f.startswith("panel_") and f.endswith(".psd"))
    if args.lines:
        want = {s.strip() for s in args.lines.split(",")}
        files = [f for f in files if f[len("panel_") : -len(".psd")] in want]
    elif not args.all:
        ap.error("--lines か --all のどちらかを指定してください")
    if args.limit:
        files = files[: args.limit]

    jobs = []
    for fn in files:
        line_id = fn[len("panel_") : -len(".psd")]
        jobs.append({
            "line_id": line_id,
            "in_psd": os.path.join(psd_dir, fn),
            "out_png": os.path.join(out_dir, "panel_%s.png" % line_id),
            "width": args.width,
            "height": args.height,
        })
    if args.resume:
        before = len(jobs)
        jobs = [j for j in jobs if not os.path.exists(j["out_png"])]
        print("書き出し済みを除外: %d → %d 枚" % (before, len(jobs)))

    print("対象 %d 枚 → %s（%d×%d）\n" % (len(jobs), out_dir, args.width, args.height))
    if not jobs:
        return
    results = run(jobs)

    log = os.path.join(out_dir, "export_log.json")
    with open(log, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    print("ログ:", log)
    print("\n次: python scripts/qa_check.py --episode \"%s\"  で納品物も含めて再検査する"
          % os.path.abspath(args.episode))


if __name__ == "__main__":
    main()
