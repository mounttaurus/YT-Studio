"""必要な変種数 K を、実際の台本の並びから測る（宿題2）.

**Kは「スロットあたり何枚」では決まらない。** 判定64件の実測で、スロットの各軸
（emotion / shot / angle）が一致しているかは「使い回しに見えるか」をほとんど説明せず
（Spearman 0.01 / 0.33 / 0.04）、指紋距離だけが説明した（0.72）。
→ `CHARACTER_CUTOUT_PLAN.md` §9-8・§10。

代わりに **並びの制約から逆算する**（§7-2「ワンパターン化は画像でなく並びの性質」）:

    「直近W行のどれとも指紋距離が TH 以上」を満たせる最小の在庫が K

役割分担:
  emotion  = **適格性**の制約（悲しい行に笑顔を当てない）。Kを決める軸ではない
  指紋距離 = **変化**の制約。ここがKを決める

出力は2つ。
  1. 現状診断: 実際の196行（全行を新規生成した回）に違反がいくつ残っているか
  2. K掃引: グループごとに在庫をK枚に絞ったら違反がどう増えるか
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

TH = 0.144  # 「これ未満なら使い回しに見える」実測の初期閾値（§9-8）
WINDOWS = [2, 3, 5, 8]


def load(fp_path: str) -> tuple[list[str], dict, np.ndarray]:
    with open(fp_path, encoding="utf-8") as fh:
        items = json.load(fh)["items"]
    ids = sorted(items)  # line_001.. の昇順＝台本の並び
    n = len(ids)

    bits = np.array([np.unpackbits(np.frombuffer(bytes.fromhex(items[i]["dhash"]), np.uint8)) for i in ids])
    shp = np.array(
        [np.frombuffer(bytes.fromhex(items[i]["shape_rel"]), np.uint8).astype(np.float32) for i in ids]
    )
    # dhash = ハミング率, shape_rel = 平均絶対差。採用した指紋は両者の平均（§9-8）
    dh = (bits[:, None, :] != bits[None, :, :]).mean(2)
    sh = np.abs(shp[:, None, :] - shp[None, :, :]).mean(2) / 255.0
    return ids, items, (dh + sh) / 2


def group_of(item: dict) -> str:
    chars = item.get("characters") or []
    if len(chars) != 1:
        return "?"
    emo = (item.get("slot") or {}).get("emotion") or "unknown"
    return "%s|%s" % (chars[0], emo)


def violations(seq: list[int], dist: np.ndarray, w: int, th: float) -> list[int]:
    """直近w行の中に距離 th 未満の相手がいる行を返す。"""
    bad = []
    for i, a in enumerate(seq):
        if a < 0:
            continue
        for j in range(max(0, i - w), i):
            b = seq[j]
            if b >= 0 and dist[a, b] < th:
                bad.append(i)
                break
    return bad


def simulate(groups: list[str], pools: dict[str, list[int]], dist: np.ndarray, w: int, th: float) -> int:
    """在庫 pools から貪欲に割り当てて違反数を返す（直近w行から最も離れた1枚を選ぶ）。"""
    seq: list[int] = []
    used: collections.Counter[int] = collections.Counter()
    bad = 0
    for i, g in enumerate(groups):
        pool = pools.get(g) or []
        if not pool:
            seq.append(-1)
            continue
        recent = [x for x in seq[max(0, i - w) :] if x >= 0]
        best, best_key = pool[0], None
        for c in pool:
            near = min((dist[c, r] for r in recent), default=1.0)
            key = (near, -used[c])  # 直近から遠い順 → 使用回数が少ない順
            if best_key is None or key > best_key:
                best, best_key = c, key
        if best_key is not None and best_key[0] < th:
            bad += 1
        used[best] += 1
        seq.append(best)
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fingerprints", required=True)
    ap.add_argument("--threshold", type=float, default=TH)
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--seed", type=int, default=20260825)
    ap.add_argument("--sheets", default=None, help="現状診断で引っかかった隣接コマの検証シートを書く先")
    ap.add_argument("--panels", default=None,
                    help="シートに使う完成コマPNG（export/）。省略時は切り抜き。**文脈込みで見るには完成コマ**")
    ap.add_argument("--window", type=int, default=3, help="シートを作る窓幅")
    args = ap.parse_args()

    ids, items, dist = load(args.fingerprints)
    idx = {lid: i for i, lid in enumerate(ids)}
    groups = [group_of(items[lid]) for lid in ids]
    avail: dict[str, list[int]] = collections.defaultdict(list)
    for lid, g in zip(ids, groups):
        avail[g].append(idx[lid])

    print("台本 %d 行 / グループ(キャラ×感情) %d 種 / 閾値 %.3f\n" % (len(ids), len(avail), args.threshold))

    # --- 1. 現状診断: 全行を新規生成した実際の回
    print("■ 現状診断（全196行を新規生成した実際の回・1行1枚すべて別画像）")
    for w in WINDOWS:
        bad = violations(list(range(len(ids))), dist, w, args.threshold)
        print("   直近%d行を見る: 違反 %2d 行 (%.0f%%)  %s"
              % (w, len(bad), 100 * len(bad) / len(ids), " ".join(ids[b] for b in bad[:6])))
    print("   → 全部新規で作っても、これだけは「近い」。**新規生成は多様性を保証しない**")

    # --- 2. グループの規模
    print("\n■ グループの規模（実出現回数＝必要投資の目安。[[aroll-variant-weight-by-emotion]]）")
    for g, v in sorted(avail.items(), key=lambda x: -len(x[1]))[:8]:
        print("   %-28s %3d 行" % (g, len(v)))
    small = sum(1 for v in avail.values() if len(v) <= 3)
    print("   ... 3行以下のグループが %d 種（ここに均等投資すると無駄）" % small)

    # --- 3. K掃引
    print("\n■ K掃引: 各グループの在庫をK枚に絞ったときの違反（%d回試行の平均）" % args.trials)
    print("   %-6s %s" % ("K", "  ".join("W=%d" % w for w in WINDOWS)))
    rnd = random.Random(args.seed)
    for k in (1, 2, 3, 4, 6, 8, 12, 999):
        cells = []
        for w in WINDOWS:
            tot = 0.0
            for _ in range(args.trials):
                pools = {g: (v if len(v) <= k else rnd.sample(v, k)) for g, v in avail.items()}
                tot += simulate(groups, pools, dist, w, args.threshold)
            cells.append("%5.1f" % (tot / args.trials))
        label = "全在庫" if k == 999 else str(k)
        print("   %-6s %s" % (label, "  ".join(cells)))
    print("   （違反＝その行で「直近W行のどれかと使い回しに見える」画像しか無かった回数）")

    if args.sheets:
        write_sheets(ids, dist, args, idx)


def write_sheets(ids: list[str], dist: np.ndarray, args, idx: dict) -> None:
    """現状診断で引っかかった隣接コマを、実際に見て確かめるためのシートにする.

    ⚠️ 閾値0.144は**切り抜き単体を並べて**較正した値。実際の動画では背景・吹き出し・
       文字が付き、間に別のコマも挟まる。**文脈込みで妥当かは別途確かめる**必要がある。
    """
    from build_pair_sheets import make_sheet

    src = args.panels or None
    pairs = []
    seen = set()
    for i in range(len(ids)):
        for j in range(max(0, i - args.window), i):
            if dist[i, j] < args.threshold and (j, i) not in seen:
                seen.add((j, i))
                pairs.append((ids[j], ids[i], float(dist[i, j])))

    os.makedirs(args.sheets, exist_ok=True)
    made = 0
    for n, (a, b, d) in enumerate(pairs, 1):
        pa = os.path.join(src, "panel_%s.png" % a)
        pb = os.path.join(src, "panel_%s.png" % b)
        if not (os.path.exists(pa) and os.path.exists(pb)):
            continue
        make_sheet(
            n, pa, pb, os.path.join(args.sheets, "near_%03d.png" % n),
            caption="この2コマは実際の回で近くに並んでいた　→　使い回しに見えた？　はい / いいえ",
        )
        made += 1

    with open(os.path.join(args.sheets, "判定.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        fh.write("番号,行A,行B,距離,実際に使い回しに見えたか,メモ\n")
        for n, (a, b, d) in enumerate(pairs, 1):
            fh.write("%03d,%s,%s,%.3f,,\n" % (n, a, b, d))

    with open(os.path.join(args.sheets, "はじめに.txt"), "w", encoding="utf-8") as fh:
        fh.write(
            "完成した回の中で、**近くに並んでいて指紋が近い**コマの組です（直近%d行以内・%d組）。\n"
            "閾値0.144は切り抜き単体を並べて決めた値なので、背景・吹き出しが付いた実物でも\n"
            "同じ判断になるかを確かめます。\n\n"
            "判定.csv の「実際に使い回しに見えたか」欄に はい / いいえ を書いてください。\n"
            "「はい」が多ければ閾値は妥当。「いいえ」ばかりなら閾値が厳しすぎるので緩めます。\n" % (args.window, made)
        )
    print("\n■ 文脈込みの検証シート %d 組 → %s" % (made, args.sheets))


if __name__ == "__main__":
    main()
