"""Select Subject でキャラを抜き、透過PNG＋マスク統計を出力する（本番工程1）.

安全策:
  - 元画像は**開かない**。scratch へコピーしたものだけを開いて処理する
  - 既に開いているドキュメントには触れず、終了時に元のアクティブへ戻す
  - 検証用ドキュメントは必ず DONOTSAVECHANGES で閉じる
  - 出力済みはスキップ（中断・再開が安全）

出力:
  {out}/cutout/panel_{line_id}.png   透過PNG
  {out}/mask_stats.json              1枚ごとに追記（キャラ位置・空き領域）
"""

from __future__ import annotations

import glob
import io
import json
import os
import shutil
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)
import numpy as np
import win32com.client
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _rootenv import load_root_env  # noqa: E402

load_root_env()

# ★ホスト固有の絶対パスを既定値にしない。他のマシンで黙って別の場所を見に行くのを防ぐ。
EP = (os.environ.get("PSA_EPISODE_DIR") or "").strip()
if not EP:
    sys.exit(
        "PSA_EPISODE_DIR が未設定です。ルート .env に対象エピソードの絶対パスを"
        "設定してください（例: <HOST_SHARED_DIR>/projects/<project_id>/episodes/ep01）"
    )
SRC_DIR = os.path.join(EP, "a_roll")
OUT_DIR = os.path.join(EP, "psassist")
CUT_DIR = os.path.join(OUT_DIR, "cutout")
STATS = os.path.join(OUT_DIR, "mask_stats.json")
WORK = os.path.join(os.environ.get("TEMP", "."), "psa_work.png")

JSX = r"""
(function () {
  var d = app.activeDocument;
  var desc = new ActionDescriptor();
  desc.putBoolean(stringIDToTypeID("sampleAllLayers"), false);
  executeAction(stringIDToTypeID("autoCutout"), desc, DialogModes.NO);
  if (d.activeLayer.isBackgroundLayer) { d.activeLayer.isBackgroundLayer = false; }
  var md = new ActionDescriptor();
  md.putClass(stringIDToTypeID("new"), stringIDToTypeID("channel"));
  var ref = new ActionReference();
  ref.putEnumerated(stringIDToTypeID("channel"), stringIDToTypeID("channel"),
                    stringIDToTypeID("mask"));
  md.putReference(stringIDToTypeID("at"), ref);
  md.putEnumerated(stringIDToTypeID("using"), stringIDToTypeID("userMaskEnabled"),
                   stringIDToTypeID("revealSelection"));
  executeAction(stringIDToTypeID("make"), md, DialogModes.NO);
  return "ok";
})();
"""


def analyze(png: str) -> dict:
    """透過PNGからキャラ位置と空き領域を測る（左右判定の材料）。"""
    a = np.asarray(Image.open(png).convert("RGBA"))[:, :, 3]
    h, w = a.shape
    on = a > 128
    if not on.any():
        return {"empty": True}
    ys, xs = np.where(on)
    col = on.mean(0)
    solid = col >= 0.02
    left = int(np.argmax(solid))
    right = w - 1 - int(np.argmax(solid[::-1]))
    # 頭部＝マスク上端から30%の帯。バブルが顔を覆わない側を選ぶための材料
    top_h = max(1, int((ys.max() - ys.min()) * 0.30))
    head = on[ys.min() : ys.min() + top_h]
    hx = np.where(head.any(0))[0]
    return {
        "empty": False,
        "canvas": [w, h],
        "bbox": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
        "coverage": round(float(on.mean()), 4),
        "free_left": left,
        "free_right": w - 1 - right,
        "center_x": round(float(xs.mean()) / w, 4),
        "head_center_x": round(float(hx.mean()) / w, 4) if len(hx) else None,
        "head_bbox_x": [int(hx.min()), int(hx.max())] if len(hx) else None,
    }


def main() -> None:
    os.makedirs(CUT_DIR, exist_ok=True)
    stats = {}
    if os.path.exists(STATS):
        with open(STATS, encoding="utf-8") as fh:
            stats = json.load(fh)

    srcs = sorted(glob.glob(os.path.join(SRC_DIR, "panel_line_*.png")))
    print("対象 %d 枚 / 済み %d 枚" % (len(srcs), len(stats)))

    ps = win32com.client.Dispatch("Photoshop.Application")
    ps.DisplayDialogs = 3
    prev = ps.ActiveDocument if ps.Documents.Count else None
    if prev is not None:
        print("開いているドキュメント: %d（触れません／終了時に復元）" % ps.Documents.Count)
    opts = win32com.client.Dispatch("Photoshop.PNGSaveOptions")

    t_start = time.time()
    done = err = skip = 0
    for i, src in enumerate(srcs, 1):
        line_id = os.path.basename(src)[len("panel_") : -len(".png")]
        out = os.path.join(CUT_DIR, "panel_%s.png" % line_id)
        if os.path.exists(out) and line_id in stats:
            skip += 1
            continue
        t0 = time.time()
        try:
            shutil.copyfile(src, WORK)
            doc = ps.Open(WORK)
            try:
                ps.DoJavaScript(JSX)
                doc.SaveAs(out, opts, True, 2)
            finally:
                doc.Close(2)
            stats[line_id] = analyze(out)
            stats[line_id]["sec"] = round(time.time() - t0, 1)
            done += 1
        except Exception as e:  # 1枚失敗しても続行
            stats[line_id] = {"error": str(e)[:200]}
            err += 1

        with open(STATS, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=1)

        if i % 10 == 0 or i == len(srcs):
            el = time.time() - t_start
            rate = el / max(1, done)
            print(
                "  [%3d/%3d] 完了%d 失敗%d スキップ%d  経過%.0f分  残り約%.0f分"
                % (i, len(srcs), done, err, skip, el / 60, rate * (len(srcs) - i) / 60)
            )

    if prev is not None:
        try:
            ps.ActiveDocument = prev
            print("アクティブを復元:", ps.ActiveDocument.Name)
        except Exception as e:
            print("復元失敗:", e)
    if os.path.exists(WORK):
        os.remove(WORK)
    print("\n完了: 成功%d / 失敗%d / スキップ%d  総時間 %.1f分" % (done, err, skip, (time.time() - t_start) / 60))


if __name__ == "__main__":
    main()
