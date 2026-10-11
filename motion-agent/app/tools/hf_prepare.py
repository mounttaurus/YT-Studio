"""HyperFrames のコンポジションを描く前の準備（vendor の GSAP・見本の素材）。scripts/hf.sh から呼ばれる。

    python -m app.tools.hf_prepare <コンテナ内のフォルダ>
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.core import hf


def main() -> None:
    d = Path(sys.argv[1])
    if not (d / "index.html").is_file():
        sys.exit(f"index.html が無い: {d}")
    for p in hf.prepare(d):
        print(p)


if __name__ == "__main__":
    main()
