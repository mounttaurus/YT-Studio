"""panel_plan.json を生成して要約を出す（Photoshop 不要）.

    python scripts/build_plan.py [--out DIR]
"""

from __future__ import annotations

import argparse
import collections
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "psassist-agent"))

from _rootenv import load_root_env  # noqa: E402
from app.core import plan_builder, spec  # noqa: E402

# ホスト実行なので設定はルート .env から読む（コンテナは compose の env_file 経由）
load_root_env()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    paths = plan_builder.Paths.from_env()
    if args.out:
        paths.out_dir = args.out

    plan = plan_builder.build(paths)
    panels = plan["panels"]
    path = plan_builder.write(plan, paths.out_dir)

    print("パネル数 %d → %s\n" % (len(panels), path))

    print("■ 話者 / バブル / 位置")
    for sp, c in collections.Counter(p["speaker"] for p in panels).most_common():
        d = spec.SPEAKER_DEFAULTS.get(sp)
        print(
            "   %-5s %3d件  %s (%s) / %s"
            % (sp, c, d.bubble_key if d else "?", spec.BUBBLE_BY_KEY[d.bubble_key].layer if d else "?", d.side if d else "?")
        )

    print("\n■ フォントサイズの分布（実測フィット前の見積り）")
    for size, c in sorted(collections.Counter(p["text"]["size"] for p in panels).items(), reverse=True):
        bar = "#" * max(1, round(c / 3))
        print("   %4.0f px  %3d件  %s" % (size, c, bar))

    print("\n■ 行数の分布")
    ln = collections.Counter(len(p["text"]["lines"]) for p in panels)
    print("   " + "  ".join("%d行:%d" % (k, v) for k, v in sorted(ln.items())))

    warn = collections.Counter(w for p in panels for w in p["warnings"])
    print("\n■ 警告")
    if not warn:
        print("   なし")
    for w, c in warn.most_common():
        print("   %-28s %3d件" % (w, c))

    att = [p for p in panels if p["status"] == "needs_attention"]
    print("\n■ 人の判断が要る %d 件 / %d" % (len(att), len(panels)))
    for p in att[:12]:
        s = p["text"]["split_suggestion"]
        extra = ""
        if s:
            extra = "  分割案: [%d字]／[%d字]" % (len(s[0]), len(s[1]))
        print("   %-10s %-22s %3d字%s" % (p["line_id"], ",".join(p["warnings"]), len(p["text"]["raw"]), extra))
    if len(att) > 12:
        print("   ...他 %d 件" % (len(att) - 12))

    print("\n■ 反転が必要なパネル: %d 件" % sum(1 for p in panels if p["bubble"]["flip_h"] or p["bubble"]["flip_v"]))


if __name__ == "__main__":
    main()
