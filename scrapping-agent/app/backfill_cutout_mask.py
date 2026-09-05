"""在庫エントリ(panel_library/library.json)に mask を後付けし、死蔵在庫を復帰する（1回きり）.

対象は2種類（Docs/AROLL_PSASSIST_REFACTOR_PLAN.md §0-1・§0-3）:
  A) cutout はあるが mask が無い（このS2導入より前に登録された分）:
     既存の透過PNGを測るだけ（re-generate不要・一瞬）
  B) image はあるが cutout が無い（2026-08-27の背景除去導入より前の世代。死蔵）:
     cutout_engine.cut_out(method="ai") で新たに切り抜き・指紋・maskを作り、
     cutouts/{slot_id}.png へ保存する。**画像は既にある生成物を再利用するだけ
     ＝API課金ゼロ**（約2.2秒/枚・Rembg）。slot_id は変えない（既存参照を壊さない）。

冪等: 既に mask を持つエントリ（かつ cutout もある）はスキップする。既定はドライラン
（何も書き込まない）。--apply で実際に書き込む。1キャラ処理し終えるごとに保存するので、
途中で落ちても直前までのキャラは保存済み＝再実行すれば続きから（スキップ判定）進む。

実行（コンテナ内・scrapping-agentのPython環境が要る＝numpy/scipy/rembg）:
  docker compose exec scrapping-agent python -m app.backfill_cutout_mask
  docker compose exec scrapping-agent python -m app.backfill_cutout_mask --char char-mr5dgcfe --apply
"""

from __future__ import annotations

import argparse
import io

from PIL import Image

from app.core import character_manager, cutout_engine, fingerprint
from app.core import panel_library_manager as plm


def _process_char(char_id: str, apply: bool) -> dict:
    idx = plm.load_index(char_id)
    entries = idx.get("entries", [])
    stats = {"total": len(entries), "mask_added": 0, "cutout_added": 0, "skipped": 0, "errors": []}
    lib_dir = plm.library_dir(char_id)
    changed = False

    for e in entries:
        slot_id = e.get("slot_id", "?")
        has_cutout = bool(e.get("cutout"))
        has_mask = bool(e.get("mask"))

        if has_cutout and has_mask:
            stats["skipped"] += 1
            continue

        try:
            if has_cutout:
                # A) 既にある透過PNGを測るだけ
                cut_path = lib_dir / e["cutout"]
                if not cut_path.exists():
                    stats["errors"].append(f"{slot_id}: cutout ファイルが無い ({e['cutout']})")
                    continue
                rgba = Image.open(cut_path).convert("RGBA")
                mask = cutout_engine.analyze_alpha(rgba)
                if apply:
                    e["mask"] = mask
                stats["mask_added"] += 1
                changed = True

            elif e.get("image"):
                # B) 死蔵: 背景付き画像から切り抜き・指紋・maskを新たに作る（課金なし）
                img_path = lib_dir / e["image"]
                if not img_path.exists():
                    stats["errors"].append(f"{slot_id}: image ファイルが無い ({e['image']})")
                    continue
                rgba, info = cutout_engine.cut_out(Image.open(img_path), method="ai")
                fp = fingerprint.for_entry(rgba)
                if not fp.get("dhash"):
                    stats["errors"].append(
                        f"{slot_id}: 背景除去でアルファが空（方式={info.get('effective')}）"
                    )
                    continue
                mask = cutout_engine.analyze_alpha(rgba)
                if apply:
                    (lib_dir / "cutouts").mkdir(parents=True, exist_ok=True)
                    buf = io.BytesIO()
                    rgba.save(buf, "PNG")
                    (lib_dir / "cutouts" / f"{slot_id}.png").write_bytes(buf.getvalue())
                    e["cutout"] = f"cutouts/{slot_id}.png"
                    e["fingerprint"] = fp
                    e["mask"] = mask
                    e["cutout_method"] = info.get("effective")
                stats["cutout_added"] += 1
                changed = True
            else:
                stats["skipped"] += 1
        except Exception as exc:  # 1件失敗しても続行
            stats["errors"].append(f"{slot_id}: {exc}")

    if apply and changed:
        plm.save_index(char_id, idx)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", help="対象キャラ1件だけ（省略で全キャラ）")
    ap.add_argument("--apply", action="store_true", help="実際に書き込む（既定はドライラン）")
    args = ap.parse_args()

    char_ids = [args.char] if args.char else [c["char_id"] for c in character_manager.list_characters()]
    print("対象キャラ: %s" % ", ".join(char_ids))
    print("モード: %s" % ("APPLY（書き込む）" if args.apply else "ドライラン（何も書かない）"))

    total = {"total": 0, "mask_added": 0, "cutout_added": 0, "skipped": 0}
    for char_id in char_ids:
        stats = _process_char(char_id, args.apply)
        print(
            "\n[%s] entries=%d mask追加=%d cutout新規=%d skip=%d"
            % (char_id, stats["total"], stats["mask_added"], stats["cutout_added"], stats["skipped"])
        )
        for err in stats["errors"]:
            print("  ! %s" % err)
        for k in ("total", "mask_added", "cutout_added", "skipped"):
            total[k] += stats[k]

    print(
        "\n=== 合計 === entries=%d mask追加=%d cutout新規=%d skip=%d"
        % (total["total"], total["mask_added"], total["cutout_added"], total["skipped"])
    )
    if not args.apply:
        print("\n（ドライランでした。書き込むには --apply を付けて再実行してください）")


if __name__ == "__main__":
    main()
