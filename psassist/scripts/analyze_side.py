"""バブルの左右を予測する信号を、正解129枚で採点する.

batch_cutout.py が出した mask_stats.json（キャラ位置）と、既存PSDから抽出した
正解（calibration.json）を突き合わせ、どの規則が一番当たるかを測る。

「予測できない」と分かればそれも成果。既定は最頻値のままにして、MCP で直す
のを一瞬にする設計へ倒す（[[psd-layout-has-no-rule]]）。
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

SPEAKER_DEFAULT = {"アオイ": "right", "ルカ": "left"}


def rules(m: dict, speaker: str) -> dict[str, str]:
    """各仮説が出す予測（left / right）。"""
    w = m["canvas"][0]
    cx = m["center_x"]
    head = m.get("head_center_x")
    x0, _, x1, _ = m["bbox"]
    out = {
        # 1. 空き領域が広い側に置く（最初の仮説。4枚では2/4だった）
        "free_space": "left" if m["free_left"] > m["free_right"] else "right",
        # 2. キャラの重心と反対側
        "opposite_body": "left" if cx > 0.5 else "right",
        # 3. キャラのbbox中心と反対側
        "opposite_bbox": "left" if (x0 + x1) / 2 / w > 0.5 else "right",
        # 4. 話者ごとに固定（現行の既定。実測64%）
        "speaker": SPEAKER_DEFAULT.get(speaker, "left"),
    }
    # 5. 頭（顔）と反対側＝バブルが顔を覆わない
    out["opposite_head"] = ("left" if head > 0.5 else "right") if head is not None else out["speaker"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", required=True)
    ap.add_argument("--calibration", required=True)
    args = ap.parse_args()

    with open(args.stats, encoding="utf-8") as fh:
        stats = json.load(fh)
    with open(args.calibration, encoding="utf-8") as fh:
        cal = json.load(fh)["rows"]

    hits: dict[str, int] = collections.Counter()
    per_speaker: dict[str, dict[str, int]] = collections.defaultdict(collections.Counter)
    n = 0
    missing = 0
    for r in cal:
        m = stats.get(r["line_id"])
        if not m or m.get("error") or m.get("empty"):
            missing += 1
            continue
        n += 1
        truth = r["bubble_side"]
        sp = r["speaker"]
        per_speaker[sp]["n"] += 1
        for name, pred in rules(m, sp).items():
            if pred == truth:
                hits[name] += 1
                per_speaker[sp][name] += 1

    if not n:
        print("照合できるデータがありません（バッチ未完了？）")
        return

    print("照合 %d 枚（マスク欠損 %d）\n" % (n, missing))
    print("■ 規則ごとの的中率")
    for name, h in hits.most_common():
        bar = "#" * round(40 * h / n)
        print("   %-15s %3d/%3d  %3.0f%%  %s" % (name, h, n, 100 * h / n, bar))

    print("\n■ 話者別")
    for sp, c in per_speaker.items():
        m_ = c["n"]
        best = max((k for k in hits), key=lambda k: c[k])
        print("   %-5s N=%3d  最良=%s (%.0f%%)  speaker既定=%.0f%%"
              % (sp, m_, best, 100 * c[best] / m_, 100 * c["speaker"] / m_))

    best_name, best_hit = hits.most_common(1)[0]
    cur = hits["speaker"]
    print("\n■ 判定")
    if best_hit > cur + n * 0.08:
        print("   → '%s' が現行既定(speaker, %.0f%%)を %.0f ポイント上回る。既定に採用する価値あり"
              % (best_name, 100 * cur / n, 100 * (best_hit - cur) / n))
    else:
        print("   → 現行既定(speaker, %.0f%%)を有意に上回る規則は無い。" % (100 * cur / n))
        print("      左右は予測せず最頻値のままとし、MCP で直すのを一瞬にする設計を維持する。")


if __name__ == "__main__":
    main()
