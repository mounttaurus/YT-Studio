"""ユーザーの自己申告を `assets/character_overrides.json` に集約する（宿題4）.

`background_overrides.json` と同じ形（`_comment` + マップ）。ただし**背景には無い層が要る**。

背景の申告は「この画像はここでは使えない」＝**1枚ごとの制約**だけで済んだ。
キャラは「ワンパターンに見える」が**2枚の関係**なので、per-image では表せない。
実際 2026-08-25 の検証では、閾値を 0.144 → 0.073 に動かしたのは
**ペア判定80件**であって個別の画像評価ではなかった（CHARACTER_CUTOUT_PLAN.md §10-6）。

⚠️ **`context` を必ず記録する。** 同じ距離でも判定が変わる ──
   `isolated`（切り抜き2枚を並べて見た）と `in_episode`（完成コマ・背景と吹き出し込み）で
   結論が割れたのが今回の最大の発見。文脈を落とすと較正データが嘘になる。

⚠️ 判定は episode 配下のレビューフォルダに置くと**話数を片付けた時に消える**ので、
   `shared/characters/character_overrides.json` に集約する。

   置き場所を repo(`psassist/assets/`) でなく shared にする理由（2026-08-25 訂正）:
   **読むのは scrapping-agent（コンテナ）**で、コンテナには `./shared:/shared` しか
   マウントされていない＝repo の `psassist/assets/` は見えない。
   `background_overrides.json` が repo に居られるのは、読み手の psassist が
   ホスト常駐でリポジトリを直接見られるから。読み手が違えば置き場所も違う。
   なお shared には character.json / styles / voices など**手作業のデータが元々居る**。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

FP_VERSION = "dhash+shape_rel/1"

COMMENT = [
    "キャラ切り抜きの選択について、機械では測れない判断をユーザーの実見に基づいて記録する。",
    "本籍の設計: psassist/Docs/CHARACTER_CUTOUT_PLAN.md §7-5 / §9 / §10。",
    "",
    "■ thresholds — ペア判定から較正した閾値。選択システムはここを読む。",
    "  repetitive_below: この距離未満は「使い回しに見える」。在庫が無いと判断して新規生成する。",
    "  gray_zone: 単体で並べると過半が気になるが、背景・吹き出し・間に挟まる別コマが",
    "    付くと気にならなくなる帯。**許容側に倒す**（実測: 完成コマ16組すべて『いいえ』）。",
    "  max_uses: 同じ画像の生涯使用回数の上限（times_used の累計。1話内ではない）。",
    "",
    "■ overrides — 1枚ごとの制約。キーは \"char_id/slot_id\"。",
    "  ⚠️ slot_id はキャラ内でしか一意でない（命名にキャラが入らないため、別キャラで同名が",
    "    25件ある）。必ず char_id を前置すること。pair_judgments の a / b も同じ形式。",
    "  banned: true にすると一切使わない。",
    "  allowed_emotions: この画像を当ててよい感情。指定した以外の行では候補にしない。",
    "    （emotion は LLM ラベルで、実際の表情とずれることがある。その補正に使う）",
    "  max_uses: この画像だけ上限を変える（特徴的すぎて何度も出せない絵など）。",
    "  note: 自由記述。",
    "",
    "■ pair_judgments — 2枚の関係についての判定。閾値を較正する教師データ。",
    "  verdict: 1=続けて出たら使い回しに見える / 2=気にならない / 3=明らかに別物",
    "  context: isolated=切り抜き2枚を並べて見た / in_episode=完成コマを実際の並びで見た",
    "  ⚠️ context を落とすと較正できない。同じ距離でも判定が変わる。",
]


def _ep(v) -> str:
    """話数の表記ゆれを吸収する。aroll.json は整数 1、人は "ep01" と書く。"""
    s = str(v).strip().lower()
    if s.startswith("ep"):
        s = s[2:]
    return str(int(s)) if s.isdigit() else s


def load_library(chars_dir: str) -> dict[tuple[str, str, str], str]:
    """(project_id, episode, line_id) → "char_id/slot_id"。取り込み済みの entry から逆引きする。

    ⚠️ line_id は話数ごとに 001 から振り直されるので、project_id と episode まで見ないと
       別プロジェクトの同じ行番号と衝突する。
    ⚠️ **slot_id はキャラ内でしか一意でない**（アオイとルカで25件が同名。命名が
       `{emotion}_{shot}_{angle}_{nnn}` でキャラを含まないため）。全キャラを1ファイルに
       集約するこのファイルでは、必ず `char_id/slot_id` の形で持つこと。
    """
    out = {}
    for char_id in sorted(os.listdir(chars_dir)):
        f = os.path.join(chars_dir, char_id, "panel_library", "library.json")
        if not os.path.exists(f):
            continue
        with open(f, encoding="utf-8") as fh:
            for e in json.load(fh).get("entries", []):
                s = e.get("source") or {}
                if s.get("line_id"):
                    key = (s.get("project_id"), _ep(s.get("episode")), s["line_id"])
                    out[key] = "%s/%s" % (char_id, e["slot_id"])
    return out


def read_isolated(review: str, meta: dict) -> list[dict]:
    pairs = {m["no"]: m for m in meta["pairs"]}
    rows = []
    with open(os.path.join(review, "判定.csv"), encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            v = (r.get("判定") or "").strip()
            if v not in ("1", "2", "3"):
                continue
            m = pairs[int(r["番号"])]
            rows.append({"a": m["a"], "b": m["b"], "verdict": int(v),
                         "distance": round((m["d"]["dhash"] + m["d"]["shape_rel"]) / 2, 4),
                         "context": "isolated", "note": (r.get("メモ") or "").strip()})
    return rows


def read_in_episode(review: str) -> list[dict]:
    rows = []
    with open(os.path.join(review, "判定.csv"), encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            ans = (r.get("実際に使い回しに見えたか") or "").strip()
            if ans not in ("はい", "いいえ"):
                continue
            # 「はい」=使い回しに見えた=1 / 「いいえ」=気にならなかった=2
            rows.append({"a": r["行A"], "b": r["行B"], "verdict": 1 if ans == "はい" else 2,
                         "distance": float(r["距離"]), "context": "in_episode",
                         "note": (r.get("メモ") or "").strip()})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--characters", required=True)
    ap.add_argument("--project-id", required=True, help="判定が採られたプロジェクトID")
    ap.add_argument("--episode", required=True, help="判定が採られた話数（1 でも ep01 でも可）")
    ap.add_argument("--fp-review", default=None, help="切り抜き単体のペア判定フォルダ")
    ap.add_argument("--near-review", default=None, help="完成コマの隣接判定フォルダ")
    ap.add_argument("--repetitive-below", type=float, default=0.073)
    ap.add_argument("--gray-until", type=float, default=0.144)
    ap.add_argument("--max-uses", type=int, default=3)
    args = ap.parse_args()

    lib = load_library(args.characters)
    rows: list[dict] = []
    if args.fp_review:
        with open(os.path.join(args.fp_review, "_pairs_meta.json"), encoding="utf-8") as fh:
            rows += read_isolated(args.fp_review, json.load(fh))
    if args.near_review:
        rows += read_in_episode(args.near_review)

    unmapped = 0
    for r in rows:
        for k in ("a", "b"):
            sid = lib.get((args.project_id, _ep(args.episode), r[k]))
            if sid is None:
                unmapped += 1
            # 取り込めなかった行（2ショット等）は line_id のまま残す＝判定を捨てない
            r[k] = sid or ("line:%s/%s/%s" % (args.project_id, _ep(args.episode), r[k]))

    prev = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            prev = json.load(fh)

    data = {
        "_comment": COMMENT,
        "schema_version": "1.0.0",
        "thresholds": {
            "fingerprint": FP_VERSION,
            "repetitive_below": args.repetitive_below,
            "gray_zone": [args.repetitive_below, args.gray_until],
            "max_uses": args.max_uses,
            "calibrated_from": {"judgments": len(rows), "episode": args.episode, "date": "2026-08-25"},
        },
        "overrides": prev.get("overrides", {}),
        "pair_judgments": rows,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

    by = {}
    for r in rows:
        by[(r["context"], r["verdict"])] = by.get((r["context"], r["verdict"]), 0) + 1
    print("判定 %d 件を書き出し（slot_id へ引けなかった参照 %d 件は line_id のまま保持）" % (len(rows), unmapped))
    for k in sorted(by):
        print("   %-12s verdict=%d : %d件" % (k[0], k[1], by[k]))
    print("   閾値 repetitive_below=%.3f / gray_zone=[%.3f, %.3f] / max_uses=%d"
          % (args.repetitive_below, args.repetitive_below, args.gray_until, args.max_uses))
    print("→ %s" % args.out)


if __name__ == "__main__":
    main()
