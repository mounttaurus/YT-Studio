"""ホスト常駐ブリッジ — panel_plan.json を Photoshop に流し込む.

コンテナ（Linux）から Windows の COM には到達できないため、この層だけは
ホストに置く必要がある。**ロジックは持たない**：プランを JSX に注入して
実行し、結果を返すだけ。

    python host-bridge/bridge.py --plan <panel_plan.json> --lines line_006,line_040
    python host-bridge/bridge.py --plan <...> --all --limit 5
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSX_PATH = os.path.join(ROOT, "jsx", "build_panel.jsx")
DEFAULT_BUBBLES = os.path.join(ROOT, "assets", "bubbles.psd")


def resolve(panel: dict, plan: dict, *, use_cutout: bool, out_dir: str, bubbles: str) -> dict:
    """プラン（論理値）に、このマシン固有の絶対パスと実行時パラメータを足す。"""
    ep = plan["source"]["episode_dir"]
    src_dir = os.path.join(ep, "psassist", "cutout") if use_cutout else os.path.join(ep, "a_roll")
    job = json.loads(json.dumps(panel))  # deep copy
    # プランがキャラ所有ライブラリの切り抜きを指している場合は既に絶対パス（背景と同じ形）。
    # その時は話数フォルダに繋ぎ直さない。
    img = panel["character"]["image"]
    job["character"]["image_abs"] = img if os.path.isabs(img) else os.path.join(src_dir, img)
    job["bubbles_psd"] = bubbles
    job["out_psd"] = os.path.join(out_dir, "panel_%s.psd" % panel["line_id"])
    job["fit"] = {
        "min": plan["defaults"]["font_min"],
        "step": 2.0,
        "max_iter": 12,
    }
    return job


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
        # ensure_ascii=True で純ASCIIにする＝JS リテラルとして安全に注入できる
        payload = "var PLAN = %s;\n" % json.dumps(job, ensure_ascii=True)
        try:
            raw = ps.DoJavaScript(payload + jsx_body)
            res = json.loads(str(raw))
        except Exception as e:
            res = {"line_id": job["line_id"], "ok": False, "error": str(e)[:300]}
        out.append(res)
        mark = "OK " if res.get("ok") else "NG "
        print(
            "  [%3d/%3d] %s %-10s %s"
            % (i, len(jobs), mark, res.get("line_id"), res.get("error") or res.get("out", ""))
        )

    if prev is not None:
        try:
            ps.ActiveDocument = prev
        except Exception:
            pass
    print("\n成功 %d / %d   %.1f分" % (sum(1 for r in out if r.get("ok")), len(out), (time.time() - t0) / 60))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--lines", help="line_006,line_040 のようにカンマ区切り")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=None, help="PSD の出力先（既定 <episode>/psassist/psd）")
    ap.add_argument("--bubbles", default=DEFAULT_BUBBLES)
    ap.add_argument("--original", action="store_true", help="抜き済みでなく元PNGを使う")
    ap.add_argument("--resume", action="store_true", help="出力済みのPSDは飛ばす")
    args = ap.parse_args()

    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    out_dir = args.out or os.path.join(plan["source"]["episode_dir"], "psassist", "psd")
    os.makedirs(out_dir, exist_ok=True)

    panels = plan["panels"]
    if args.lines:
        want = {s.strip() for s in args.lines.split(",")}
        panels = [p for p in panels if p["line_id"] in want]
    elif not args.all:
        ap.error("--lines か --all のどちらかを指定してください")
    if args.limit:
        panels = panels[: args.limit]

    jobs = [
        resolve(p, plan, use_cutout=not args.original, out_dir=out_dir, bubbles=args.bubbles)
        for p in panels
    ]
    if args.resume:
        before = len(jobs)
        jobs = [j for j in jobs if not os.path.exists(j["out_psd"])]
        print("出力済みを除外: %d → %d 枚" % (before, len(jobs)))
    print("対象 %d 枚 → %s\n" % (len(jobs), out_dir))
    results = run(jobs)

    log = os.path.join(out_dir, "build_log.json")
    with open(log, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    print("ログ:", log)


if __name__ == "__main__":
    main()
