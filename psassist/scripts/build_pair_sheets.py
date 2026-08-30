"""指紋の検証用に、切り抜き2枚を並べた「比較シート」PNGと判定CSVを作る.

宿題1の受け皿。ユーザーは Explorer でシートを順に見て、CSVに 1/2/3 を書くだけ。
判定が集まったら `score_pairs.py` で各信号と突き合わせ、採否を決める。

⚠️ **シートに距離を印字しない。** 数字が見えると判定がそれに引っ張られ、
   検証にならない（正解は `_pairs_meta.json` 側に隠す）。
⚠️ **並び順をランダムにする。** 距離順に並べると「だんだん似てくる」と分かってしまう。

ペアの選び方:
  - 同一キャラのペアだけを本題にする（別キャラは自明に「別物」）
  - 各信号の距離分布のパーセンタイルから拾い、**全域をまたぐ**ようにする
  - 最も近い数ペア（真の重複候補）は必ず入れる
  - 別キャラのペアを数枚だけ紛れ込ませる＝**当たり前を外す信号を落とすための番犬**
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_keep_stdout = sys.stdout  # fingerprint 経由で measure_shot が stdout を差し替えるため
from fingerprint import SIGNALS, distances

sys.stdout.reconfigure(encoding="utf-8")

# 分布から拾う位置。近い側を厚く取る（判定の分かれ目は近い側にある）
PCTS = [0, 1, 3, 6, 10, 20, 35, 55, 80]
STRATIFY = ["phash", "color", "head", "combo", "shape_abs", "dhash"]
N_NEAREST = 8  # combo で最も近いペア（真の重複候補）
N_CROSS = 4  # 別キャラの番犬

CELL_W, CELL_H = 660, 430
PAD, HEAD_H = 16, 58
BG = (128, 128, 128)


def _font(size: int):
    # 日本語が出せるフォントを優先する（arial だと注意書きが豆腐になる）
    for p in (
        r"C:\Windows\Fonts\meiryob.ttc",
        r"C:\Windows\Fonts\meiryo.ttc",
        r"C:\Windows\Fonts\YuGothB.ttc",
        r"C:\Windows\Fonts\msgothic.ttc",
        r"C:\Windows\Fonts\arialbd.ttf",
    ):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _cell(path: str) -> Image.Image:
    im = Image.open(path).convert("RGBA")
    im.thumbnail((CELL_W, CELL_H), Image.LANCZOS)
    cell = Image.new("RGBA", (CELL_W, CELL_H), BG + (255,))
    cell.alpha_composite(im, ((CELL_W - im.width) // 2, (CELL_H - im.height) // 2))
    return cell


CAPTION = "1 = 続けて出たら使い回しに見える　 2 = 気にならない　 3 = 明らかに別物"


def make_sheet(no: int, pa: str, pb: str, out: str, caption: str = CAPTION) -> None:
    w = CELL_W * 2 + PAD * 3
    h = CELL_H + HEAD_H + PAD * 2
    sheet = Image.new("RGBA", (w, h), (32, 32, 32, 255))
    d = ImageDraw.Draw(sheet)
    d.text((PAD, 12), "No.%03d" % no, font=_font(28), fill=(255, 255, 255))
    d.text((PAD + 175, 20), caption, font=_font(18), fill=(175, 175, 175))
    sheet.alpha_composite(_cell(pa), (PAD, HEAD_H + PAD))
    sheet.alpha_composite(_cell(pb), (PAD * 2 + CELL_W, HEAD_H + PAD))
    sheet.convert("RGB").save(out, "PNG")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fingerprints", required=True)
    ap.add_argument("--cutout", required=True)
    ap.add_argument("--out", required=True, help="出力フォルダ")
    ap.add_argument("--seed", type=int, default=20260825)
    args = ap.parse_args()

    with open(args.fingerprints, encoding="utf-8") as fh:
        items = json.load(fh)["items"]
    ids = sorted(items)

    same, cross = [], []
    for a, b in itertools.combinations(ids, 2):
        ca = set(items[a].get("characters") or [])
        cb = set(items[b].get("characters") or [])
        if not ca or not cb:
            continue
        rec = (a, b, distances(items[a], items[b]))
        if ca == cb:
            same.append(rec)
        elif not (ca & cb):
            cross.append(rec)
    print("同キャラ %d ペア / 別キャラ %d ペア から選ぶ" % (len(same), len(cross)))

    picked: dict[tuple[str, str], dict] = {}

    def take(rec, why: str) -> None:
        key = (rec[0], rec[1])
        if key in picked:
            picked[key]["why"].append(why)
        else:
            picked[key] = {"a": rec[0], "b": rec[1], "d": rec[2], "why": [why]}

    for sig in STRATIFY:
        ordered = sorted(same, key=lambda r: r[2][sig])
        for p in PCTS:
            take(ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))], "%s@p%d" % (sig, p))
    for rec in sorted(same, key=lambda r: r[2]["combo"])[:N_NEAREST]:
        take(rec, "nearest")
    rnd = random.Random(args.seed)
    for rec in rnd.sample(cross, min(N_CROSS, len(cross))):
        take(rec, "cross_char")

    rows = list(picked.values())
    rnd.shuffle(rows)  # 距離順の気配を消す
    os.makedirs(args.out, exist_ok=True)

    meta = []
    for i, r in enumerate(rows, 1):
        make_sheet(
            i,
            os.path.join(args.cutout, "panel_%s.png" % r["a"]),
            os.path.join(args.cutout, "panel_%s.png" % r["b"]),
            os.path.join(args.out, "pair_%03d.png" % i),
        )
        meta.append(
            {
                "no": i,
                "a": r["a"],
                "b": r["b"],
                "why": r["why"],
                "same_char": set(items[r["a"]]["characters"]) == set(items[r["b"]]["characters"]),
                "same_slot_key": items[r["a"]].get("slot_key") == items[r["b"]].get("slot_key"),
                "d": {k: round(v, 4) for k, v in r["d"].items()},
            }
        )

    with open(os.path.join(args.out, "_pairs_meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"signals": SIGNALS, "pairs": meta}, fh, ensure_ascii=False, indent=1)

    # Excel が文字化けしないよう BOM 付き UTF-8 で書く
    csv_path = os.path.join(args.out, "判定.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("番号,判定,メモ\n")
        for m in meta:
            fh.write("%03d,,\n" % m["no"])

    with open(os.path.join(args.out, "はじめに.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "視覚指紋の検証シートです。pair_001.png から順に見て、判定.csv の「判定」欄に\n"
            "1 / 2 / 3 のいずれかを書いてください（%d枚）。\n\n"
            "  1 = この2枚が数行以内に続けて出たら「使い回し」に見える\n"
            "  2 = 続けて出ても特に気にならない\n"
            "  3 = 明らかに別物\n\n"
            "迷ったら直感で。メモ欄は任意です（「顔は同じだが構図が違う」等）。\n"
            "距離の数値はわざと出していません。数字を見ると判定がそちらに引っ張られ、\n"
            "検証になりません。\n" % len(meta)
        )

    n_cross = sum(1 for m in meta if not m["same_char"])
    print("シート %d 枚（うち別キャラの番犬 %d）→ %s" % (len(meta), n_cross, args.out))
    print("判定CSV: %s" % csv_path)


if __name__ == "__main__":
    main()
