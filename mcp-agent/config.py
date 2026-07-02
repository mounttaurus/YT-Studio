"""MCP-Agent 設定（環境変数1枚＝ルート .env / 環境に依存しない既定値）。

stdio MCPサーバーはホスト上で動くため、director へは localhost で到達する。
docker-compose の environment ではなくホストプロセスの env を読む点に注意。
"""
import os

# director-agent（既存の汎用プロキシ＝各コンテナRESTの中継元）への到達先。
# 更新系を director 経由で叩くと director_log.json に監査が残る利点を継承する。
DIRECTOR_URL = os.getenv("DIRECTOR_URL", "http://localhost:8005").rstrip("/")

# director プロキシ越しの素材収集/LLM生成は長い（最大300s中継）。読み取りは短く。
HTTP_TIMEOUT_READ = float(os.getenv("MCP_TIMEOUT_READ", "15"))
HTTP_TIMEOUT_WRITE = float(os.getenv("MCP_TIMEOUT_WRITE", "300"))

# 課金/不可逆ツールの既定挙動。公開後継では製品安全要件（Docs/SUCCESSOR_PUBLIC_PACKAGE.md §4）。
# 現行は外部ホスト(Claude Code)の permission が ask で守るため既定 False で良いが、
# 後継のために tools 側にもガードの土台を残しておく。
AUTO_APPROVE_COST = os.getenv("MCP_AUTO_APPROVE_COST", "false").lower() == "true"
