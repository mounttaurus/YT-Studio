"""在庫切り抜き（Photoshop 被写体選択）の共通処理.

`batch_cutout.py`（エピソードのAロール素材の切り抜き）と `host_worker.py` の
在庫スイープ（Docs/CUTOUT_PS_PRIMARY_PLAN.md P1）が共用する。JSXを2か所に
持たない。

★このファイルは win32com を import しない。`needs_ps_cutout` だけは
Photoshop 抜きで（host_worker.py の常駐ループから）呼べる必要があるため。
"""

from __future__ import annotations

import shutil

JSX_SELECT_SUBJECT = r"""
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


def cutout_one(ps, png_save_options, src: str, out: str, work: str) -> None:
    """src（背景付きの元画像）に触れず、work にコピーしてから開いて切り抜き、out へ保存する。

    呼び出し側が Photoshop.Application（`DisplayDialogs` 設定済み）と
    `Photoshop.PNGSaveOptions` を用意している前提。プロセスの起動・
    アクティブドキュメントの退避/復元は呼び出し側の責務にする
    （1回のPS起動で何枚も回す呼び出し元がいるため、ここでは持たない）。
    """
    shutil.copyfile(src, work)
    doc = ps.Open(work)
    try:
        ps.DoJavaScript(JSX_SELECT_SUBJECT)
        doc.SaveAs(out, png_save_options, True, 2)
    finally:
        doc.Close(2)  # DONOTSAVECHANGES


def needs_ps_cutout(entry: dict) -> bool:
    """在庫スイープの対象判定（Docs/CUTOUT_PS_PRIMARY_PLAN.md §3）。

    元画像(`image`)があり、psassist取り込み由来（`kind == "cutout"`）ではなく、
    まだPS版に置き換わっていない（`cutout_method != "ps_select_subject"`）もの。

    ⚠️ **コンテナ側 `panel_library_manager.needs_ps_cutout`（P2で追加）と同じ定義を保つこと。**
    ホストはコンテナのコードを import できないためここに複製する。
    両方に同じテスト表を置いて一致を保証する（§3）。
    """
    return (
        bool(entry.get("image"))
        and entry.get("kind") != "cutout"
        and entry.get("cutout_method") != "ps_select_subject"
    )
