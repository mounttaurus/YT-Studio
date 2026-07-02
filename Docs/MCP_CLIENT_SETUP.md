# MCP-Agent — 他のAgent UI接続マニュアル

`mcp-agent/server.py`（stdio MCPサーバー）を Claude Code / Goose 以外の Agent UI から使うための手順。
中核仕様は `mcp-agent/README.md` が本籍。本ファイルは「クライアント別の挿し方」だけを扱う索引。

## 前提

1. director-agent(:8005) が起動していること。
   ```
   docker compose up -d
   curl http://localhost:8005/health
   ```
2. `mcp-agent/.venv` がセットアップ済みであること（未setupなら）。
   ```bash
   cd mcp-agent
   python -m venv .venv
   ./.venv/Scripts/python.exe -m pip install -r requirements.txt
   ```
3. stdio方式＝新コンテナ・新ポートは不要。各Agent UIがこのPythonプロセスをサブプロセスとして起動する。

## 接続情報（共通）

どのクライアントも最終的には以下の3項目を聞いてくる。クライアントごとに書式（キー名・設定ファイルの場所）が違うだけ。

| 項目 | 値 |
|---|---|
| command | `<このリポのクローン先絶対パス>\mcp-agent\.venv\Scripts\python.exe` |
| args | `<このリポのクローン先絶対パス>\mcp-agent\server.py` |
| env | `DIRECTOR_URL=http://localhost:8005` |

> `<このリポのクローン先絶対パス>` は `install.ps1`/`install.sh` を実行した環境ごとに異なる。
> 一番確実なのは、Claude Code用に自動生成された **リポ直下の `.mcp.json`** を開き、
> そこに書かれている実際の `command`/`args` の値をそのまま他クライアントの設定にコピーすること。
> 手で打つ場合は、Windowsパスを JSON に書くときバックスラッシュを `\\` でエスケープする（後述の例は全てエスケープ済み）。

## 共通JSON雛形

多くのクライアント（Claude Desktop / Cursor / Cline / Windsurf）は実質同じスキーマ
（`mcpServers` 直下にサーバー名キー）を使う。まずこれをコピーし、設定ファイルの場所だけクライアント別の節で確認する。

```json
{
  "mcpServers": {
    "yt-studio": {
      "command": "<このリポのクローン先絶対パス>\\mcp-agent\\.venv\\Scripts\\python.exe",
      "args": ["<このリポのクローン先絶対パス>\\mcp-agent\\server.py"],
      "env": {
        "DIRECTOR_URL": "http://localhost:8005"
      }
    }
  }
}
```

## クライアント別の設定場所

### Claude Code（参考・既設定）
リポ直下の [.mcp.json](../.mcp.json) に登録済み。このリポで `claude` を起動すれば自動認識（初回は接続承認プロンプト）。

### Claude Desktop
設定ファイル: `%APPDATA%\Claude\claude_desktop_config.json`（Windows）。
上記の共通JSON雛形をそのまま `mcpServers` にマージし、Claude Desktopを再起動する。
設定UIからも開ける: 設定 → Developer → Edit Config。

### Cursor
- プロジェクト単位: リポ直下に `.cursor/mcp.json` を作成し、共通雛形をそのまま貼る。
- グローバル: `%USERPROFILE%\.cursor\mcp.json`。
Cursor の Settings → MCP に一覧表示され、有効化トグルがある。

### Cline（VS Code拡張）
VS Code内で Cline パネル → MCP Servers アイコン → "Configure MCP Servers" を開くと
`cline_mcp_settings.json` が開く（実体は `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`）。
共通雛形と同じスキーマでそのまま追記する。

### Windsurf（Codeium）
設定ファイル: `%USERPROFILE%\.codeium\windsurf\mcp_config.json`。
共通雛形と同じスキーマ。Windsurf の Settings → Cascade → MCP Servers からGUI編集も可能。

### Continue
`%USERPROFILE%\.continue\config.yaml` に以下を追記（YAML形式・キー名が異なる点に注意）:
```yaml
mcpServers:
  - name: yt-studio
    command: <このリポのクローン先絶対パス>\mcp-agent\.venv\Scripts\python.exe
    args:
      - <このリポのクローン先絶対パス>\mcp-agent\server.py
    env:
      DIRECTOR_URL: http://localhost:8005
```

### VS Code（GitHub Copilot Chat / Agent mode）
リポ直下に `.vscode/mcp.json` を作成。トップキーが `servers`（`mcpServers` ではない）で、
各エントリに `"type": "stdio"` が必要な点が他クライアントと異なる:
```json
{
  "servers": {
    "yt-studio": {
      "type": "stdio",
      "command": "<このリポのクローン先絶対パス>\\mcp-agent\\.venv\\Scripts\\python.exe",
      "args": ["<このリポのクローン先絶対パス>\\mcp-agent\\server.py"],
      "env": { "DIRECTOR_URL": "http://localhost:8005" }
    }
  }
}
```

### Goose（参考・既設定）
Extensions → Add → タイプ **STDIO** で以下を入力:
- Command: `<このリポのクローン先絶対パス>\mcp-agent\.venv\Scripts\python.exe`
- Args: `<このリポのクローン先絶対パス>\mcp-agent\server.py`
- Env: `DIRECTOR_URL=http://localhost:8005`

> 上記のファイルパス・キー名は各クライアントのバージョンで変わることがある。設定UIに
> 「MCP」「Add Server」的な項目があれば、そこから共通JSON雛形の3項目（command/args/env）を
> 埋める方が設定ファイルを直接編んでいくより事故が少ない。

## 動作確認

接続後、チャットで「プロジェクト一覧を見せて」のように頼み、`list_projects` ツールが呼ばれてプロジェクト一覧が返れば疎通成功。

## 注意点（重要）

- **COST/GPU系ツールの確認ゲートはクライアント依存**。Claude Codeの `permissions`（ask）のような
  「実行前に人間が承認する」仕組みが無い／既定offのクライアントでは、`search_footage` /
  `select_footage` / `run_tts` / `free_generate` / `generate_character_panel` / `research_search`
  などが**無確認で即実行される**（外部API課金・クォータ消費・不可逆DLを含む）。
  当該クライアントの「ツール実行前確認」設定を必ず有効化してから使うこと。
  どのツールがCOST/GPU分類かは `mcp-agent/tools.py` の `TOOLS` レジストリが正本。
- 複数クライアントから同時に接続しても director-agent 側は通常のHTTP APIとして受けるため
  競合はしないが、同じプロジェクトに対して同時に書き込み系ツールを叩くのは避ける。
- `DIRECTOR_URL` を変える場合（director を別ポート/別ホストで動かす等）は env の値だけ差し替える。
