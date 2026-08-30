"""合成済みPSDを検査して qa_report.json と表示用画像を書き出す.

★方針: **検査は必ず出力PSDを読んで行う。** プラン（panel_plan.json）の計算値では
  判定しない。`doc.paste()` が座標を壊していた時、プラン上は「重なり0」なのに
  実物は 307px ずれて吹き出しに被っていた。QA の価値は実物を見ることにある。

★重なりは2次元で測る。「左端 vs 右端」の1次元比較では、吹き出しの**下**にはみ出た
  肩を重なりと誤判定する。バブルのアルファとキャラのアルファの AND を取る。

出力（すべて episode/psassist/ の下）:

    qa_report.json      検査結果（director-agent の🖼️Aロールタブが読む）
    qa/thumb/{lid}.jpg  一覧用サムネ（320px）
    qa/view/{lid}.jpg   詳細ビュー用（1376px）

⚠️ `qa/` は**表示用**であって納品物ではない。納品物は `export/panel_*.png`
   （1920×1080・ベクター再描画のため Photoshop が要る）。`qa/` は PSD が持つ
   Photoshop 自身の合成プレビューをそのまま取り出すので **Photoshop 不要**＝
   ユーザーが Photoshop で作業中でも検査と表示ができる。
   納品物の有無・寸法は EXPORT_* として別に検査する。

    python scripts/qa_check.py --episode <episode_dir>
    python scripts/qa_check.py --episode <...> --lines line_121,line_168
"""

from __future__ import annotations

import argparse
import collections
import datetime
import io
import json
import os
import sys
import time

if __name__ == "__main__":
    # host_worker.py がモジュールとしてimportする時は再ラップしない。
    # 既にラップ済みのsys.stdoutを再ラップすると、外れたTextIOWrapperがGCで
    # closeされ、後続のimport（export_png.py等）で "I/O operation on closed file" になる。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)

import numpy as np
from PIL import Image
from psd_tools import PSDImage

SCHEMA_VERSION = "1.0.0"

# ── しきい値 ────────────────────────────────────────────────────────────
# バブル本体にキャラが乗っている割合。0.02 以下は輪郭のアンチエイリアス程度。
OVERLAP_CLEAN = 0.02
OVERLAP_BLOCK = 0.10
# 文字がバブルの外に出ている割合。1% でも読めなくなるので低め。
TEXT_OUT_TOL = 0.005
# バブルが顔に掛かっている割合（顔の面積比）。
FACE_TOL = 0.05
# 光源の向きが逆と見なす最小の強さ（弱い光は判定しない）。
LIGHT_MIN = 0.10

# 頭身 → ショット（scripts/measure_shot.py と同一の対応表）
SHOT_BY_HEADS = [
    (1.6, "face_closeup"),
    (2.8, "bust"),
    (4.0, "waist_up"),
    (5.6, "knee"),
    (99.0, "wide"),
]
# 背景アーカイブの framing 語彙との対応（knee は背景側に語彙が無い）
FRAMING_OF_SHOT = {
    "face_closeup": "face_closeup",
    "bust": "bust",
    "waist_up": "waist_up",
    "knee": "waist_up",
    "wide": "wide",
}

THUMB_W = 320
VIEW_W = 1376

SEV_ORDER = {"clean": 0, "advisory": 1, "blocking": 2}


# ── PSD 読み取り ────────────────────────────────────────────────────────

def layer_alpha(layer, canvas: tuple[int, int]) -> np.ndarray | None:
    """レイヤーのアルファをキャンバス座標の 2D float32 (0..1) にして返す。"""
    w, h = canvas
    bbox = layer.bbox
    if bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    try:
        a = layer.numpy()
    except Exception:
        return None
    if a is None or a.size == 0:
        return None
    al = a[:, :, 3] if a.shape[2] >= 4 else np.ones(a.shape[:2], np.float32)

    out = np.zeros((h, w), np.float32)
    x0, y0, x1, y1 = bbox
    # キャンバスからはみ出す分を落とす（生成物は画面外へ伸びていることがある）
    sx0, sy0 = max(0, -x0), max(0, -y0)
    dx0, dy0 = max(0, x0), max(0, y0)
    dx1, dy1 = min(w, x1), min(h, y1)
    if dx1 <= dx0 or dy1 <= dy0:
        return None
    out[dy0:dy1, dx0:dx1] = al[sy0 : sy0 + (dy1 - dy0), sx0 : sx0 + (dx1 - dx0)]
    return out


def layer_rgb(layer, canvas: tuple[int, int]) -> np.ndarray | None:
    """レイヤーのRGBをキャンバス座標に置いた float32 (0..255) で返す。"""
    w, h = canvas
    bbox = layer.bbox
    if bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    try:
        a = layer.numpy()
    except Exception:
        return None
    if a is None or a.size == 0 or a.shape[2] < 3:
        return None
    out = np.zeros((h, w, 3), np.float32)
    x0, y0, x1, y1 = bbox
    sx0, sy0 = max(0, -x0), max(0, -y0)
    dx0, dy0 = max(0, x0), max(0, y0)
    dx1, dy1 = min(w, x1), min(h, y1)
    if dx1 <= dx0 or dy1 <= dy0:
        return None
    out[dy0:dy1, dx0:dx1] = a[sy0 : sy0 + (dy1 - dy0), sx0 : sx0 + (dx1 - dx0), :3] * 255.0
    return out


def classify_layers(psd: PSDImage) -> dict:
    """JSX が組んだレイヤー構造を役割ごとに拾う（build_panel.jsx と対の知識）。"""
    found = {"background": [], "visible_bg": None, "adjust": None,
             "character": None, "bubble": None, "text": None, "empty": []}
    for l in psd.descendants():
        kind, name = l.kind, (l.name or "")
        if kind == "smartobject":
            found["background"].append(l)
            if l.visible and found["visible_bg"] is None:
                found["visible_bg"] = l
        elif kind in ("huesaturation", "brightnesscontrast", "curves", "levels"):
            found["adjust"] = l
        elif name.startswith("bubble_") or kind == "shape":
            if found["bubble"] is None:
                found["bubble"] = l
        elif kind == "type":
            if found["text"] is None:
                found["text"] = l
        elif kind == "pixel":
            bbox = l.bbox
            empty = bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]
            if empty or name.startswith("Layer 1"):
                found["empty"].append(l)
            elif found["character"] is None:
                found["character"] = l
    return found


# ── 個別の測定 ──────────────────────────────────────────────────────────

def head_box(mask: np.ndarray) -> dict | None:
    """マスクから頭の範囲を測る（scripts/measure_shot.py と同じ手順）。

    ⚠️ 単純に上から幅の谷を探すと頭のてっぺんを拾う。頭のピークより**下**で
       最小幅になる行を首とする。
    """
    rows = np.where(mask.any(1))[0]
    if len(rows) < 40:
        return None
    top, bottom = int(rows[0]), int(rows[-1])
    widths = mask[top : bottom + 1].sum(1).astype(float)
    n = len(widths)
    if widths.max() <= 0 or n < 40:
        return None

    head_zone = max(8, int(n * 0.35))
    head_peak = int(np.argmax(widths[:head_zone]))
    if head_peak < 3:
        return None
    lo, hi = head_peak + 2, max(head_peak + 6, int(n * 0.65))
    if hi <= lo:
        return None
    neck = lo + int(np.argmin(widths[lo:hi]))
    if widths[neck] >= widths[head_peak] * 0.95:
        return None  # くびれが無い＝顔アップで首まで写っていない

    # ⚠️ 頭の左右端を「占有列の min〜max」で取ると、2ショットで**もう1人の頭まで
    #    含んでしまう**（実測: 165枚中5枚が画面幅の55%超になり、顔被り率が薄まった）。
    #    かといって「最も濃い塊」を1つ選ぶと、**バブルが被っていない側の頭**を選んで
    #    しまい今度は0%になる。連続した塊を**候補として全部返し**、選ぶのは
    #    「顔が隠れているか」を判定する側に任せる。
    band = mask[top : top + neck]
    occ = band.any(0)
    if not occ.any():
        return None
    runs, start = [], None
    for x in range(len(occ)):
        if occ[x] and start is None:
            start = x
        elif not occ[x] and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, len(occ)))
    # 細すぎる塊（触角・アホ毛・背景の切り残し）は頭ではない
    runs = [(a, b) for a, b in runs if b - a >= max(16, neck // 4)] or [
        (int(np.where(occ)[0][0]), int(np.where(occ)[0][-1]) + 1)
    ]
    widest = max(runs, key=lambda r: r[1] - r[0])
    return {
        "rect": [int(widest[0]), top, int(widest[1]), top + neck],
        "runs": [[int(a), int(b)] for a, b in runs],
        "row_band": [top, top + neck],
        "head_h": int(neck),
        "visible_h": bottom - top + 1,
    }


def classify_shot(heads: float) -> str:
    for limit, name in SHOT_BY_HEADS:
        if heads <= limit:
            return name
    return "wide"


def light_dx(rgb: np.ndarray, mask: np.ndarray) -> float | None:
    """キャラ画素だけで測る照明の向き（正=右が明るい）。augment_masks.py と同式。"""
    xs = np.where(mask.any(0))[0]
    if len(xs) < 20:
        return None
    lum = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    third = max(1, (xs.max() - xs.min()) // 3)
    lsl = slice(int(xs.min()), int(xs.min()) + third)
    rsl = slice(int(xs.max()) - third, int(xs.max()))
    lm, rm = mask[:, lsl], mask[:, rsl]
    if lm.sum() <= 50 or rm.sum() <= 50:
        return None
    left, right = lum[:, lsl][lm].mean(), lum[:, rsl][rm].mean()
    return round(float((right - left) / max(1.0, (left + right) / 2)), 3)


def bbox_of(mask: np.ndarray) -> list[int] | None:
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    return [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]


# ── 1枚ぶんの検査 ───────────────────────────────────────────────────────

def check_panel(psd_path: str, meta: dict, bgs: dict, export_png: str | None) -> dict:
    psd = PSDImage.open(psd_path)
    canvas = (psd.width, psd.height)
    L = classify_layers(psd)

    issues: list[dict] = []
    measured: dict = {"canvas": [canvas[0], canvas[1]]}

    def add(code, sev, label, value=None, region=None):
        it = {"code": code, "severity": sev, "label": label}
        if value is not None:
            it["value"] = value
        if region is not None:
            it["region"] = region
        issues.append(it)

    # ── 構造の欠落 ──
    if L["character"] is None:
        add("MISSING_CHARACTER", "blocking", "キャラのレイヤーがありません")
    if L["bubble"] is None:
        add("MISSING_BUBBLE", "blocking", "吹き出しのレイヤーがありません")
    if L["text"] is None:
        add("MISSING_TEXT", "blocking", "セリフのテキストレイヤーがありません")
    if L["visible_bg"] is None:
        add("NO_VISIBLE_BACKGROUND", "blocking", "表示中の背景がありません（背景が透明のまま）")
    if L["adjust"] is None:
        add("NO_ADJUSTMENT", "advisory", "色調整レイヤーがありません（後から色を合わせられません）")
    for e in L["empty"]:
        add("EMPTY_LAYER", "advisory", "空のレイヤーが残っています: %s" % e.name)

    bg_id = L["visible_bg"].name if L["visible_bg"] is not None else None
    measured["background"] = bg_id
    measured["bg_hidden"] = len(L["background"]) - (1 if bg_id else 0)

    char_a = layer_alpha(L["character"], canvas) if L["character"] is not None else None
    bub_a = layer_alpha(L["bubble"], canvas) if L["bubble"] is not None else None
    txt_a = layer_alpha(L["text"], canvas) if L["text"] is not None else None

    char_m = char_a > 0.5 if char_a is not None else None
    bub_m = bub_a > 0.5 if bub_a is not None else None
    # 文字はアンチエイリアスが載るので低めのしきい値で拾う
    txt_m = txt_a > 0.25 if txt_a is not None else None

    if bub_m is not None:
        measured["bubble_rect"] = bbox_of(bub_m)

    # ── ① 吹き出しとキャラの重なり（2次元AND） ──
    if char_m is not None and bub_m is not None and bub_m.sum():
        inter = char_m & bub_m
        ratio = float(inter.sum()) / float(bub_m.sum())
        measured["overlap"] = round(ratio, 4)
        if ratio > OVERLAP_BLOCK:
            add("BUBBLE_OVERLAP", "blocking",
                "吹き出しの%d%%にキャラが重なっています" % round(ratio * 100),
                round(ratio, 4), bbox_of(inter))
        elif ratio > OVERLAP_CLEAN:
            add("BUBBLE_OVERLAP", "advisory",
                "吹き出しに少しキャラが掛かっています（%d%%）" % round(ratio * 100),
                round(ratio, 4), bbox_of(inter))

    # ── ② 文字が吹き出しからはみ出す ──
    if txt_m is not None and txt_m.sum():
        if bub_m is not None:
            outside = txt_m & ~bub_m
            ratio = float(outside.sum()) / float(txt_m.sum())
            measured["text_outside"] = round(ratio, 4)
            if ratio > TEXT_OUT_TOL:
                add("TEXT_OVERFLOW", "blocking",
                    "セリフが吹き出しから%d%%はみ出しています" % max(1, round(ratio * 100)),
                    round(ratio, 4), bbox_of(outside))
        tb = bbox_of(txt_m)
        measured["text_rect"] = tb
        if tb and (tb[0] < 0 or tb[1] < 0 or tb[2] > canvas[0] or tb[3] > canvas[1]):
            add("TEXT_OFF_CANVAS", "blocking", "セリフが画面の外へ出ています", None, tb)

    # ── ③ 実測ショット vs 背景の画角 ──
    hb = head_box(char_m) if char_m is not None else None
    if hb:
        heads = hb["visible_h"] / max(1, hb["head_h"])
        shot = classify_shot(heads)
        measured["heads"] = round(heads, 2)
        measured["shot"] = shot
        measured["head_rect"] = hb["rect"]
        bg = bgs.get(bg_id or "")
        want = bg.get("framing") if bg else None
        if want and FRAMING_OF_SHOT.get(shot) != want:
            add("FRAMING_MISMATCH", "advisory",
                "キャラは%s なのに背景は%s 用です" % (shot, want), None)
            measured["bg_framing"] = want

        # ── ④ 吹き出しが顔に被る ──
        #
        # 知りたいのは「どの頭か」ではなく「**顔が隠れているか**」。2ショットでは
        # 頭の候補が複数あるので全部試し、**最も隠れている顔**で判定する
        # （片方の顔が丸ごと隠れているのに、もう片方で薄めて見逃すのを防ぐ）。
        if bub_m is not None:
            y0, y1 = hb["row_band"]
            best_r, best_face = 0.0, None
            for x0, x1 in hb.get("runs") or [hb["rect"][0::2]]:
                face = np.zeros_like(bub_m)
                face[y0:y1, x0:x1] = char_m[y0:y1, x0:x1]
                if not face.sum():
                    continue
                r = float((face & bub_m).sum()) / float(face.sum())
                if r > best_r or best_face is None:
                    best_r, best_face = r, face
                    measured["head_rect"] = [x0, y0, x1, y1]
            if best_face is not None:
                measured["face_overlap"] = round(best_r, 4)
                if best_r > FACE_TOL:
                    add("BUBBLE_ON_FACE", "advisory",
                        "吹き出しが顔に%d%%掛かっています" % round(best_r * 100),
                        round(best_r, 4), bbox_of(best_face & bub_m))

    # ── ⑤ 光源が逆 ──
    if char_m is not None:
        rgb = layer_rgb(L["character"], canvas)
        cdx = light_dx(rgb, char_m) if rgb is not None else None
        measured["light_dx"] = cdx
        bg = bgs.get(bg_id or "")
        bdx = bg.get("light_dx") if bg else None
        if cdx is not None and bdx is not None:
            measured["bg_light_dx"] = bdx
            if cdx * bdx < 0 and abs(cdx) > LIGHT_MIN and abs(bdx) > LIGHT_MIN:
                add("LIGHT_CONFLICT", "advisory",
                    "光の向きが背景と逆です（キャラ%+.2f / 背景%+.2f）" % (cdx, bdx))

    # ── ⑥ 納品物（1920×1080 PNG）の有無・寸法・鮮度 ──
    if export_png is not None:
        if not os.path.exists(export_png):
            add("EXPORT_MISSING", "advisory", "1920×1080の書き出しがまだありません")
        else:
            try:
                with Image.open(export_png) as im:
                    measured["export_size"] = list(im.size)
                    if im.size != (1920, 1080):
                        add("EXPORT_SIZE", "blocking",
                            "書き出しが%d×%dです（1920×1080であるべき）" % im.size)
            except Exception as e:
                add("EXPORT_UNREADABLE", "blocking", "書き出しPNGが読めません: %s" % e)
            # ★PSDを直したのに書き出し直していないと、**検査は緑なのに納品物は古い**
            #   という一番危ない状態になる。mtime で検知する。
            try:
                if os.path.getmtime(psd_path) > os.path.getmtime(export_png) + 1:
                    add("EXPORT_STALE", "advisory",
                        "PSDを直したあと書き出し直していません（納品PNGが古い）")
            except OSError:
                pass

    sev = "clean"
    for it in issues:
        if SEV_ORDER[it["severity"]] > SEV_ORDER[sev]:
            sev = it["severity"]

    return {"psd": psd, "severity": sev, "issues": issues, "measured": measured}


# ── 表示用画像 ──────────────────────────────────────────────────────────

def write_images(psd: PSDImage, line_id: str, qa_dir: str) -> dict:
    """PSD が持つ Photoshop 自身の合成プレビューを取り出して縮小保存する。

    ★Photoshop を起動しない。ユーザーが作業中でも検査できることを優先する。
    """
    out = {}
    im = psd.topil()
    if im is None:
        return out
    if im.mode != "RGB":
        im = im.convert("RGB")
    for sub, width, quality in (("view", VIEW_W, 82), ("thumb", THUMB_W, 78)):
        d = os.path.join(qa_dir, sub)
        os.makedirs(d, exist_ok=True)
        w = min(width, im.width)
        r = im if w == im.width else im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
        p = os.path.join(d, "%s.jpg" % line_id)
        r.save(p, quality=quality, optimize=True)
        out[sub] = "psassist/qa/%s/%s.jpg" % (sub, line_id)
    return out


def existing_images(qa_dir: str, line_id: str) -> dict:
    """既に書き出してある表示用画像への参照（--no-images でも参照を失わないため）。"""
    out = {}
    for sub in ("view", "thumb"):
        if os.path.exists(os.path.join(qa_dir, sub, "%s.jpg" % line_id)):
            out[sub] = "psassist/qa/%s/%s.jpg" % (sub, line_id)
    return out


# ── メイン ──────────────────────────────────────────────────────────────

def build_ctx(episode: str, psd_dir_name: str = "psd_final",
               backgrounds: str | None = None, no_images: bool = False) -> dict | None:
    """1エピソード分の検査コンテキストを組み立てる。

    ★ `--watch` のプロジェクト横断化（`host_worker.py`）でも使う共通部品。
       `psd_dir` が無ければ None を返す（そのエピソードはまだ組版が無い＝スキップ対象）。
    """
    ep = os.path.abspath(episode)
    psa = os.path.join(ep, "psassist")
    psd_dir = os.path.join(psa, psd_dir_name)
    qa_dir = os.path.join(psa, "qa")
    export_dir = os.path.join(psa, "export")
    report_path = os.path.join(psa, "qa_report.json")

    if not os.path.isdir(psd_dir):
        return None

    plan_path = os.path.join(psa, "panel_plan.json")
    plan = {}
    if os.path.exists(plan_path):
        with open(plan_path, encoding="utf-8") as fh:
            plan = json.load(fh)
    meta = {p["line_id"]: p for p in plan.get("panels", [])}

    bg_json = backgrounds or os.path.join(
        plan.get("source", {}).get("backgrounds_dir", ""), "backgrounds.json")
    bgs = {}
    if bg_json and os.path.exists(bg_json):
        with open(bg_json, encoding="utf-8") as fh:
            bgs = {b["bg_id"]: b for b in json.load(fh).get("backgrounds", [])}

    have_export = os.path.isdir(export_dir)
    return {
        "psd_dir": psd_dir, "qa_dir": qa_dir, "export_dir": export_dir,
        "report_path": report_path, "ep": ep, "plan": plan, "meta": meta,
        "bgs": bgs, "have_export": have_export, "src": psd_dir_name,
        "no_images": no_images,
    }


def snapshot_psd_dir(psd_dir: str) -> dict[str, tuple[float, int]]:
    """`psd_dir` 内の panel_*.psd の (mtime, size) 一覧。`watch()` と `host_worker.py` の共通部品。"""
    out = {}
    for fn in os.listdir(psd_dir):
        if not (fn.startswith("panel_") and fn.endswith(".psd")):
            continue
        try:
            st = os.stat(os.path.join(psd_dir, fn))
        except OSError:
            continue
        out[fn] = (st.st_mtime, st.st_size)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", required=True, help="エピソードディレクトリ")
    ap.add_argument("--psd-dir", default="psd_final", help="psassist/ からの相対（既定 psd_final）")
    ap.add_argument("--backgrounds", default=None, help="backgrounds.json（既定はプランから）")
    ap.add_argument("--lines", help="line_121,line_168 のようにカンマ区切り（部分再検査）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-images", action="store_true", help="サムネ/ビューを書き出さない")
    ap.add_argument("--watch", action="store_true",
                    help="検査後もPSDの保存を見張り、直したものだけ即座に検査し直す")
    ap.add_argument("--interval", type=float, default=2.0, help="--watch の確認間隔（秒）")
    args = ap.parse_args()

    ctx = build_ctx(args.episode, args.psd_dir, args.backgrounds, args.no_images)
    if ctx is None:
        sys.exit("PSD が見つかりません: %s" % os.path.join(
            os.path.abspath(args.episode), "psassist", args.psd_dir))
    psd_dir = ctx["psd_dir"]

    all_files = sorted(f for f in os.listdir(psd_dir)
                       if f.startswith("panel_") and f.endswith(".psd"))
    files = all_files
    if args.lines:
        want = {s.strip() for s in args.lines.split(",")}
        files = [f for f in files if f[len("panel_") : -len(".psd")] in want]
    if args.limit:
        files = files[: args.limit]

    print("検査 %d 枚 ← %s" % (len(files), psd_dir))
    if not ctx["have_export"]:
        print("（export/ がまだ無いので納品物の検査は行いません）")

    run_pass(ctx, files, verbose=True)

    if args.watch:
        watch(ctx, args.interval)


def run_pass(ctx: dict, files: list[str], *, verbose: bool) -> dict:
    """指定ファイルを検査し、既存レポートに**line_id単位でマージ**して書き戻す。"""
    psd_dir, qa_dir = ctx["psd_dir"], ctx["qa_dir"]
    export_dir, report_path = ctx["export_dir"], ctx["report_path"]
    meta, bgs, have_export = ctx["meta"], ctx["bgs"], ctx["have_export"]
    plan = ctx["plan"]

    old = {}
    if os.path.exists(report_path):
        try:
            with open(report_path, encoding="utf-8") as fh:
                old = {p["line_id"]: p for p in json.load(fh).get("panels", [])}
        except Exception:
            old = {}

    panels = []
    t0 = time.time()
    for i, fn in enumerate(files, 1):
        line_id = fn[len("panel_") : -len(".psd")]
        m = meta.get(line_id, {})
        exp = os.path.join(export_dir, "panel_%s.png" % line_id) if have_export else None
        try:
            r = check_panel(os.path.join(psd_dir, fn), m, bgs, exp)
        except Exception as e:
            panels.append({
                "line_id": line_id, "order": m.get("order"),
                "severity": "blocking",
                "issues": [{"code": "PSD_UNREADABLE", "severity": "blocking",
                            "label": "PSDが読めません: %s" % str(e)[:120]}],
                "measured": {},
            })
            print("  [%3d/%3d] NG %-10s %s" % (i, len(files), line_id, str(e)[:80]))
            continue

        if ctx["no_images"]:
            # 書き出しを飛ばしても、既にある画像への参照は落とさない
            imgs = {k: v for k, v in existing_images(qa_dir, line_id).items()}
        else:
            imgs = write_images(r["psd"], line_id, qa_dir)
        panels.append({
            "line_id": line_id,
            "order": m.get("order"),
            "speaker": m.get("speaker"),
            "text": (m.get("text") or {}).get("raw"),
            "psd": "psassist/%s/%s" % (ctx["src"], fn),
            "export": "psassist/export/panel_%s.png" % line_id if (
                exp and os.path.exists(exp)) else None,
            "view": imgs.get("view"),
            "thumb": imgs.get("thumb"),
            "severity": r["severity"],
            "issues": r["issues"],
            "measured": r["measured"],
        })
        if verbose and (i % 25 == 0 or i == len(files)):
            print("  %3d/%3d  %.0f秒" % (i, len(files), time.time() - t0))

    # 部分再検査なら既存ぶんを残す
    merged = dict(old)
    for p in panels:
        merged[p["line_id"]] = p
    out = sorted(merged.values(), key=lambda p: (p.get("order") or 9999, p["line_id"]))

    counts = collections.Counter(p["severity"] for p in out)
    by_code = collections.Counter(
        it["code"] for p in out for it in p.get("issues", []))

    report = {
        "schema_version": SCHEMA_VERSION,
        "project_id": plan.get("project_id"),
        "episode": plan.get("episode"),
        # ホスト側の実パス。director-agent の UI が「再検査コマンド」を出すのに使う
        # （コンテナからはホストのパスが分からないため、書いた側が記録する）。
        "episode_dir": ctx["ep"],
        "checked_at": datetime.datetime.now(datetime.timezone.utc)
                              .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": ctx["src"],
        "summary": {
            "total": len(out),
            "blocking": counts["blocking"],
            "advisory": counts["advisory"],
            "clean": counts["clean"],
            "by_code": dict(by_code.most_common()),
        },
        "panels": out,
    }
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)

    if verbose:
        print("\n■ 検査結果  全%d枚" % len(out))
        print("   要対応 %d / 助言 %d / 問題なし %d"
              % (counts["blocking"], counts["advisory"], counts["clean"]))
        for code, n in by_code.most_common():
            print("   %-22s %3d件" % (code, n))
        print("\n→ %s" % report_path)
        if not ctx["no_images"]:
            print("→ %s%s{thumb,view}%s" % (qa_dir, os.sep, os.sep))
    return report


def watch(ctx: dict, interval: float) -> None:
    """PSDの保存を見張り、変わったものだけ即座に検査し直す.

    ★これが「直す→確認」のループを閉じる。ユーザーは Photoshop で保存するだけでよく、
      ブラウザでは 🔄再読込 を押すだけ。**Photoshop は一切操作しない**（占有しない）。

    ⚠️ Photoshop の保存は一瞬で終わらない。サイズが2回続けて同じになるまで待つ
       （書きかけのPSDを読むと PSD_UNREADABLE になる）。
    """
    psd_dir = ctx["psd_dir"]

    seen = snapshot_psd_dir(psd_dir)
    print("\n👀 監視中: %s" % psd_dir)
    print("   Photoshop で保存すると、その1枚だけ検査し直します（Ctrl+C で終了）")
    print("   ブラウザ側は director の🔍合成チェックで「🔄 再読込」を押してください\n")
    pending: dict[str, tuple[float, int]] = {}
    try:
        while True:
            time.sleep(interval)
            now = snapshot_psd_dir(psd_dir)
            for fn, sig in now.items():
                if seen.get(fn) != sig:
                    pending[fn] = sig  # 変わった。落ち着くまで待つ
            ready = [fn for fn, sig in pending.items() if now.get(fn) == sig]
            # 1周期またいで同じ＝書き込み完了とみなす
            settled = [fn for fn in ready if pending[fn] == now[fn] and seen.get(fn) != now[fn]]
            done = []
            for fn in settled:
                if pending[fn] != now.get(fn):
                    continue
                rep = run_pass(ctx, [fn], verbose=False)
                lid = fn[len("panel_") : -len(".psd")]
                p = next((x for x in rep["panels"] if x["line_id"] == lid), None)
                mark = {"clean": "✔ 問題なし", "advisory": "🟡 助言",
                        "blocking": "🔴 要対応"}.get(p["severity"] if p else "", "?")
                detail = "  ".join(i["label"] for i in (p or {}).get("issues", []))
                s = rep["summary"]
                print("[%s] %-10s %s  %s" % (time.strftime("%H:%M:%S"), lid, mark, detail))
                print("            残り 要対応%d / 助言%d / 問題なし%d"
                      % (s["blocking"], s["advisory"], s["clean"]))
                seen[fn] = now[fn]
                done.append(fn)
            for fn in done:
                pending.pop(fn, None)
            for fn in list(pending):
                if fn in now:
                    pending[fn] = now[fn]
    except KeyboardInterrupt:
        print("\n監視を終了しました。")


if __name__ == "__main__":
    main()
