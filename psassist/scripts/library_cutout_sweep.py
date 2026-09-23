"""在庫スイープ用: 1枚だけPSで切り抜く（`host_worker.py` から子プロセスで呼ばれる）.

Docs/CUTOUT_PS_PRIMARY_PLAN.md P1。`--src`（背景付きの元画像）を `--out` へ
透過PNGで書き出す。バッチ処理はしない（1枚専用）。`host_worker.py` が
1周期1枚に絞って呼ぶことで、Photoshopの占有を短く保つ。

安全策は `batch_cutout.py` と同じ:
  - 元画像は**開かない**。scratch へコピーしたものだけを開いて処理する
  - 既に開いているドキュメントには触れず、終了時に元のアクティブへ戻す
  - 検証用ドキュメントは必ず DONOTSAVECHANGES で閉じる
"""

from __future__ import annotations

import argparse
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _rootenv import load_root_env  # noqa: E402
import ps_cutout_lib  # noqa: E402

load_root_env()

WORK = os.path.join(os.environ.get("TEMP", "."), "psa_library_sweep_work.png")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="背景付きの元画像（panel_library/images/{slot_id}.png）")
    ap.add_argument("--out", required=True, help="書き出し先（一時パス。呼び出し側が本パスへrenameする）")
    args = ap.parse_args()

    import win32com.client  # noqa: PLC0415  ホスト専用なので遅延import

    ps = win32com.client.Dispatch("Photoshop.Application")
    ps.DisplayDialogs = 3
    prev = ps.ActiveDocument if ps.Documents.Count else None
    opts = win32com.client.Dispatch("Photoshop.PNGSaveOptions")
    try:
        ps_cutout_lib.cutout_one(ps, opts, args.src, args.out, WORK)
    finally:
        if prev is not None:
            try:
                ps.ActiveDocument = prev
            except Exception as e:
                print("アクティブ復元失敗:", e)
        if os.path.exists(WORK):
            os.remove(WORK)
    print("ok: %s" % args.out)


if __name__ == "__main__":
    main()
