"""stdio MCPサーバー（Stage1）。

tools.py の純関数を FastMCP ツールとして公開するだけの薄い包み。
外部ホスト（Claude Code / Goose）がこのサーバーをサブプロセスとして起動し、
頭脳とエージェントループはホスト側が担う（Docs/MCP_AGENT_RESEARCH.md §5 / Stage1）。

新コンテナ・新ポートは追加しない（stdio＝ホスト常駐）。director(:8005) へは localhost で到達。
"""
import functools
import json

from mcp.server.fastmcp import FastMCP, Image

import tools

mcp = FastMCP("yt-studio")


def _with_images(fn):
    """戻り値の "_images"（[{"name", "png": bytes}]）を画像として返す。
    tools.py を FastMCP に依存させないための橋（画像を返すツールだけがこの形を使う）。
    本文（JSON）→ 画像の順に返す。画像が無ければ元の戻り値のまま。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        out = await fn(*args, **kwargs)
        images = out.pop("_images", None) if isinstance(out, dict) else None
        if not images:
            return out
        parts = [json.dumps(out, ensure_ascii=False)]
        parts += [Image(data=i["png"], format=i.get("format", "png")) for i in images]
        return parts

    return wrapper


# tools.py のレジストリを走査して登録（1正本から自動配線＝二重定義しない）。
for _entry in tools.TOOLS:
    mcp.tool()(_with_images(_entry["fn"]))


if __name__ == "__main__":
    mcp.run()  # 既定 stdio トランスポート
