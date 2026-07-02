"""stdio MCPサーバー（Stage1）。

tools.py の純関数を FastMCP ツールとして公開するだけの薄い包み。
外部ホスト（Claude Code / Goose）がこのサーバーをサブプロセスとして起動し、
頭脳とエージェントループはホスト側が担う（Docs/MCP_AGENT_RESEARCH.md §5 / Stage1）。

新コンテナ・新ポートは追加しない（stdio＝ホスト常駐）。director(:8005) へは localhost で到達。
"""
from mcp.server.fastmcp import FastMCP

import tools

mcp = FastMCP("yt-studio")

# tools.py のレジストリを走査して登録（1正本から自動配線＝二重定義しない）。
for _entry in tools.TOOLS:
    mcp.tool()(_entry["fn"])


if __name__ == "__main__":
    mcp.run()  # 既定 stdio トランスポート
