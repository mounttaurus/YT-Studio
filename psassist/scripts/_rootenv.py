"""ホスト実行のスクリプトにリポルートの `.env` を読ませる.

YT-Studio の規約は **設定はルート `.env` 1枚**（個別サービスの .env は作らない）。
コンテナは compose の `env_file:` でそれを受け取るが、ホストで直接叩くスクリプトは
自分で読む必要がある。ここだけがその差を吸収する。

⚠️ 既に環境変数にある値は**上書きしない**（シェルで一時的に差し替えられるように）。
"""

from __future__ import annotations

import os

# psassist/scripts/_rootenv.py → psassist/ → リポルート
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_root_env(path: str | None = None) -> str | None:
    """リポルートの .env を os.environ へ流し込む。読めたパスを返す。"""
    f = path or os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(f):
        return None
    # utf-8-sig: エディタが付けた BOM で先頭キーが壊れるのを防ぐ
    with open(f, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            # compose は引用符を剥がすので同じ挙動に揃える
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)
    return f
