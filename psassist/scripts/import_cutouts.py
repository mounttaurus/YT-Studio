"""切り抜き196枚を panel_library へ取り込み、キャラ画像の正本にする（§6 工程1〜3）.

ユーザー決定(2026-08-25):「正本が2系統あるのは混乱する。今回の196枚をまずは正本とし、
他は入りません」。既存71件（パステル無地背景のスタジオ画像）は**触らない・混ぜない**。
新しい entry は `kind: "cutout"` で区別する。

⚠️ **本番パイプラインを壊さないための2重の安全網**
  1. `review_status: "pending"` で入れる → `find_current` は未承認を除外するので選ばれない
  2. `kind: "cutout"` → `find_current` 側にも除外を入れる（透過PNGをパネルとして
     配ると背景の無いコマになる）
  切り抜きは「背景・吹き出しと合成する素材」であって、そのまま1コマにはならない。

⚠️ 既定はドライラン。**書き込むには `--apply`**。

取り込む情報は確度で3層に分ける（§3）。埋められないものは推測で埋めず `null`。
各フィールドの出所を `*_source: "llm" | "measured" | "user"` で必ず併記する。
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_keep_stdout = sys.stdout
from fingerprint import SIGNALS  # noqa: F401  （指紋の版を明示するため）

sys.stdout.reconfigure(encoding="utf-8")

from PIL import Image, PngImagePlugin  # noqa: E402

FP_VERSION = "dhash+shape_rel/1"  # §9-8 で採用した指紋。閾値0.073（§10-6）
KIND = "cutout"
# 層C: 今は埋めない。**推測で埋めるより空欄が良い**（[[slot-shot-label-unreliable]]）
LAYER_C = ["facing", "mouth", "eyes", "hands", "props"]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slot_id_for(slot: dict, seq: int) -> str:
    """既存 panel_library と同じ命名（`{emotion}_{shot}_{angle}_{nnn}`）。

    §5-1の決定により**ラベル由来のまま据え置く**（実測は `shot_measured` に別で持つ）。
    可変値は焼かない（`times_used`・`review_status` はJSON側だけ。§4）。
    """
    parts = [(slot.get(k) or "unknown") for k in ("emotion", "shot", "angle")]
    return "%s_%03d" % ("_".join(parts), seq)


def build_entry(lid: str, fp: dict, stats: dict, shot: dict, panel: dict,
                slot_id: str, appearance: str, project_id: str, episode: str,
                style: str, aspect: str) -> dict:
    slot = panel.get("slot") or {}
    e = {
        "slot_id": slot_id,
        "kind": KIND,
        "cutout": "cutouts/%s.png" % slot_id,
        "image": None,  # 切り抜きは単体でコマにならない。背景と合成して初めて絵になる
        # --- 層B: 既存メタから引き継ぐ（出所が明確・ただしLLM由来は信用度を併記）
        "emotion": slot.get("emotion"),
        "shot": slot.get("shot"),
        "angle": slot.get("angle"),
        "pose": slot.get("pose"),
        "slot_source": "llm",
        "appearance_version": appearance,
        "aspect": aspect,
        "style": style,
        "prompt": panel.get("prompt"),
        "provider": panel.get("provider"),
        # ⚠️ aroll.json は model を記録していない（196件すべて provider のみ）。
        #    推測で埋めない。生成側のスキーマ追加は別課題。
        "model": panel.get("model"),
        "source": {"project_id": project_id, "episode": episode, "line_id": lid},
        # --- 層A: 実測（信頼できる）
        "measured": {
            "shot": (shot or {}).get("measured"),
            "heads": (shot or {}).get("heads"),
            "bbox": fp.get("bbox"),
            "coverage": fp.get("coverage"),
            "head_bbox_x": (stats or {}).get("head_bbox_x"),
            "edges": (stats or {}).get("edges"),
            "light_dx": (stats or {}).get("light_dx"),
            "canvas": (stats or {}).get("canvas"),
        },
        "measured_source": "measured",
        # --- 選択システムの中核（§9-8）
        "fingerprint": {"version": FP_VERSION, "dhash": fp["dhash"], "shape_rel": fp["shape_rel"]},
        # --- 層D: 運用
        "review_status": "pending",
        "times_used": 0,
        "created_at": _now(),
        "note": "",
    }
    for k in LAYER_C:  # 空欄であることを明示する（キーごと無いと「未対応」と区別が付かない）
        e[k] = None
        e["%s_source" % k] = None
    return e


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode-dir", required=True, help="…/episodes/epNN")
    ap.add_argument("--characters", required=True, help="shared/characters")
    ap.add_argument("--apply", action="store_true", help="実際に書き込む（既定はドライラン）")
    args = ap.parse_args()

    ep = args.episode_dir.replace("\\", "/").rstrip("/")
    ps = ep + "/psassist"
    with open(ps + "/fingerprints.json", encoding="utf-8") as fh:
        fps = json.load(fh)["items"]
    with open(ps + "/mask_stats.json", encoding="utf-8") as fh:
        stats = json.load(fh)
    with open(ps + "/shot_measured.json", encoding="utf-8") as fh:
        shots = json.load(fh)
    with open(ep + "/a_roll/aroll.json", encoding="utf-8") as fh:
        ar = json.load(fh)
    panels = {p["line_id"]: p for p in ar["panels"]}
    project_id, episode = ar.get("project_id"), ar.get("episode")
    style, aspect = ar.get("style"), ar.get("aspect")

    # --- 対象を仕分ける
    per_char: dict[str, list[str]] = collections.defaultdict(list)
    skipped: list[tuple[str, str]] = []
    for lid in sorted(fps):
        chars = fps[lid].get("characters") or []
        if len(chars) == 1:
            per_char[chars[0]].append(lid)
        else:
            skipped.append((lid, "キャラ%d人（単一char_idに紐付けられない）" % len(chars)))

    print("切り抜き %d 枚 / 取り込み対象 %d 枚 / 対象外 %d 枚"
          % (len(fps), sum(len(v) for v in per_char.values()), len(skipped)))
    for lid, why in skipped:
        print("   対象外 %s: %s" % (lid, why))
    print("   ※ 2ショットの破綻は入力側の不整合が原因（memory/aroll-two-shot-input-inconsistency）")

    total_new = 0
    for char_id, lids in sorted(per_char.items()):
        cdir = "%s/%s" % (args.characters.replace("\\", "/").rstrip("/"), char_id)
        libf = cdir + "/panel_library/library.json"
        lib = {"schema_version": "1.0.0", "char_id": char_id, "entries": []}
        if os.path.exists(libf):
            with open(libf, encoding="utf-8") as fh:
                lib = json.load(fh)
        lib.setdefault("entries", [])
        existing = lib["entries"]
        old_kinds = collections.Counter(e.get("kind", "panel") for e in existing)
        taken = {e.get("slot_id") for e in existing}
        appearance = next((e.get("appearance_version") for e in existing if e.get("appearance_version")), None)

        made = []
        for lid in lids:
            slot = panels[lid].get("slot") or {}
            seq = 1
            while True:
                sid = slot_id_for(slot, seq)
                if sid not in taken:
                    break
                seq += 1
            taken.add(sid)
            made.append((lid, sid, build_entry(
                lid, fps[lid], stats.get(lid, {}), shots.get(lid, {}), panels[lid],
                sid, appearance, project_id, episode, style, aspect)))

        print("\n■ %s  既存 %s → 追加 %d 件（すべて review_status=pending）"
              % (char_id, dict(old_kinds), len(made)))
        for lid, sid, _ in made[:3]:
            print("   %s → %s" % (lid, sid))
        print("   ...")

        if not args.apply:
            continue

        outdir = cdir + "/panel_library/cutouts"
        os.makedirs(outdir, exist_ok=True)
        for lid, sid, entry in made:
            src = "%s/cutout/panel_%s.png" % (ps, lid)
            dst = "%s/%s.png" % (outdir, sid)
            shutil.copyfile(src, dst)
            # メタは iTXt に埋める（機械が読む用。Explorerには出ない。§2）
            im = Image.open(dst)
            info = PngImagePlugin.PngInfo()
            info.add_itxt("cutout_meta", json.dumps(entry, ensure_ascii=False), zip=True)
            im.save(dst, "PNG", pnginfo=info)
        lib["entries"] = existing + [e for _, _, e in made]
        lib["updated_at"] = _now()
        with open(libf, "w", encoding="utf-8") as fh:
            json.dump(lib, fh, ensure_ascii=False, indent=2)
        total_new += len(made)
        print("   → %s に %d 件書き込み / 画像 %s" % (libf, len(made), outdir))

    if not args.apply:
        print("\n※ ドライラン。書き込むには --apply を付ける")
    else:
        print("\n取り込み完了 %d 件（すべて pending。承認するまで本番の選択対象にならない）" % total_new)


if __name__ == "__main__":
    main()
