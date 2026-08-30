"""ユーザーのペア判定と、各視覚指紋の距離を突き合わせて採否を決める.

宿題1の判定フェーズ。`analyze_side.py`（左右の規則を正解129枚で採点した）と同じ形。
**「予測できない」と分かればそれも成果**なので、基準を先に置いてから測る。

採用の条件（先に宣言する。結果を見てから動かさない）:
  a. 別キャラの番犬ペアが全て「3=明らかに別物」＝判定データ自体が健全である
  b. AUC ≥ 0.80  （「1=使い回し」と「3=別物」を距離で並べ替えられるか）
  c. Spearman ≥ 0.50  （3段階と単調に対応しているか）
  d. 1検出の均衡正解率 ≥ 0.75  （実際に閾値を引いて使えるか）

⚠️ 64ペアで重みを最適化しない。過学習して本番で外れる。等重みの合成と、
   上位2信号の平均までを候補に留める。
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

AUC_MIN, RHO_MIN, BACC_MIN = 0.80, 0.50, 0.75


def _rankdata(a: np.ndarray) -> np.ndarray:
    """同値は平均順位（scipy が無いので自前）。"""
    a = np.asarray(a, dtype=float)
    sorter = np.argsort(a, kind="mergesort")
    inv = np.empty(len(a), dtype=int)
    inv[sorter] = np.arange(len(a))
    arr = a[sorter]
    obs = np.r_[True, arr[1:] != arr[:-1]]
    dense = obs.cumsum()[inv]
    cnt = np.r_[np.nonzero(obs)[0], len(a)]
    return 0.5 * (cnt[dense] + cnt[dense - 1] + 1)


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    rx, ry = _rankdata(x), _rankdata(y)
    rx, ry = rx - rx.mean(), ry - ry.mean()
    den = np.sqrt((rx**2).sum() * (ry**2).sum())
    return float((rx * ry).sum() / den) if den else 0.0


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    """pos（=3 別物。距離が大きいはず）が neg（=1 使い回し）より上に来る率。"""
    if not len(pos) or not len(neg):
        return float("nan")
    r = _rankdata(np.r_[pos, neg])[: len(pos)]
    return float((r.sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def best_threshold(dist: np.ndarray, is_rep: np.ndarray) -> tuple[float, float, int, int]:
    """『距離 < th なら使い回し』の最良閾値と均衡正解率。"""
    cands = np.unique(dist)
    cands = np.r_[cands[0] - 1e-6, (cands[:-1] + cands[1:]) / 2, cands[-1] + 1e-6]
    best = (float("nan"), -1.0, 0, 0)
    for th in cands:
        pred = dist < th
        tp = int((pred & is_rep).sum())
        fn = int((~pred & is_rep).sum())
        fp = int((pred & ~is_rep).sum())
        tn = int((~pred & ~is_rep).sum())
        sens = tp / max(1, tp + fn)
        spec = tn / max(1, tn + fp)
        bacc = (sens + spec) / 2
        if bacc > best[1]:
            best = (float(th), float(bacc), tp, fp)
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", required=True, help="fp_review フォルダ")
    args = ap.parse_args()

    with open(os.path.join(args.review, "_pairs_meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    pairs = {m["no"]: m for m in meta["pairs"]}

    verdicts: dict[int, int] = {}
    with open(os.path.join(args.review, "判定.csv"), encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            v = (row.get("判定") or "").strip()
            if v in ("1", "2", "3"):
                verdicts[int(row["番号"])] = int(v)

    if len(verdicts) < 20:
        print("判定が %d 件しかありません。判定.csv の「判定」欄を埋めてください。" % len(verdicts))
        return

    # a. 番犬チェック
    watch = [(n, v) for n, v in verdicts.items() if not pairs[n]["same_char"]]
    bad = [n for n, v in watch if v != 3]
    print("判定 %d / %d 件" % (len(verdicts), len(pairs)))
    print("■ 番犬（別キャラ %d ペア）" % len(watch))
    if bad:
        print("   ⚠️ 別キャラなのに「別物」でない判定: %s" % bad)
        print("      判定基準がぶれている可能性。先にここを確認する。")
    else:
        print("   全て 3=明らかに別物。判定データは健全")

    nos = sorted(verdicts)
    y = np.array([verdicts[n] for n in nos], dtype=float)
    print("\n■ 判定の内訳  1=%d  2=%d  3=%d" % ((y == 1).sum(), (y == 2).sum(), (y == 3).sum()))

    cols = {s: np.array([pairs[n]["d"][s] for n in nos]) for s in meta["signals"]}
    # 上位2信号の平均も候補に入れる（重みは振らない＝過学習させない）。
    # 上位4本まで見る: 3本で切ると Spearman 0.01 差の信号が落ちて有力候補を見逃す
    # （実際 2026-08-25 の初回採点で shape_rel が4位0.57・shape_abs が3位0.58だった）。
    base = sorted(cols, key=lambda s: -abs(spearman(cols[s], y)))[:4]
    for a, b in itertools.combinations(base, 2):
        cols["%s+%s" % (a, b)] = (cols[a] + cols[b]) / 2

    is_rep = y == 1
    pos, neg = y == 3, y == 1
    rows = []
    for s, d in cols.items():
        rho = spearman(d, y)
        a = auc(d[pos], d[neg])
        th, bacc, tp, fp = best_threshold(d, is_rep)
        rows.append((s, rho, a, bacc, th, tp, fp))
    rows.sort(key=lambda r: -(r[2] if r[2] == r[2] else 0))

    print("\n■ 信号ごとの成績（1=使い回し ↔ 3=別物 を距離で当てられるか）")
    print("   %-16s %7s %7s %7s %8s  %s" % ("signal", "Spearman", "AUC", "均衡正解", "閾値", "判定"))
    for s, rho, a, bacc, th, tp, fp in rows:
        ok = (a >= AUC_MIN) and (rho >= RHO_MIN) and (bacc >= BACC_MIN)
        print(
            "   %-16s %7.2f %7.2f %7.2f %8.3f  %s"
            % (s, rho, a, bacc, th, "採用可" if ok else "")
        )

    print("\n■ 判定別の平均距離（上位3信号）")
    for s in [r[0] for r in rows[:3]]:
        d = cols[s]
        print(
            "   %-16s 1:%.3f  2:%.3f  3:%.3f"
            % (s, d[y == 1].mean() if (y == 1).any() else float("nan"),
               d[y == 2].mean() if (y == 2).any() else float("nan"),
               d[y == 3].mean() if (y == 3).any() else float("nan"))
        )

    best = rows[0]
    print("\n■ 結論")
    if (best[2] >= AUC_MIN) and (best[1] >= RHO_MIN) and (best[3] >= BACC_MIN):
        print("   → '%s' を視覚指紋に採用する（AUC %.2f / Spearman %.2f / 均衡正解 %.2f）。"
              % (best[0], best[2], best[1], best[3]))
        print("      『距離 < %.3f なら使い回しに見える』を初期閾値とし、宿題2〜4へ進む。" % best[4])
        # ⚠️ 1位を鵜呑みにしない。数十ペアでは上位のAUC差は誤差に埋もれる
        tie = [r[0] for r in rows if r[2] == r[2] and best[2] - r[2] <= 0.05]
        if len(tie) > 1:
            print("      ただし AUC が 0.05 以内に %d 本が並ぶ: %s" % (len(tie), " / ".join(tie)))
            print("      **この差は %d ペアでは決着しない。順位でなく機構で選ぶこと**"
                  "（何を見ている信号かで説明が付く方を採る）。" % len(verdicts))
    else:
        print("   → 基準(AUC≥%.2f, Spearman≥%.2f, 均衡正解≥%.2f)を満たす信号は無い。"
              % (AUC_MIN, RHO_MIN, BACC_MIN))
        print("      最良は '%s'(AUC %.2f)。**軽量ハッシュでは人間の感覚に届かない**と結論し、"
              % (best[0], best[2]))
        print("      埋め込みへ進むか、選択方式そのものを見直す（ストックから選ぶ設計の再考）。")
        print("      ⚠️ ここで基準を下げて無理に採用しない。土台が動くと宿題2〜4が全部やり直しになる。")


if __name__ == "__main__":
    main()
