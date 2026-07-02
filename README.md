# YT Studio

YouTube動画の制作プロセス（リサーチ → 台本 → 素材収集 → 音声合成 → ラフ編集情報生成）を
自動化するマルチエージェントシステム。各エージェントはDockerコンテナとして動作する。

> **重要**: 本パッケージは「エージェント群(Docker) + MCPサーバー」のみを配布する。
> 実際にエージェント群を**自然言語で操作する頭脳（AIエージェント）はユーザー自身で用意する**
> 必要がある（[Claude Code](https://claude.com/claude-code) / [Goose](https://block.github.io/goose/) など、
> MCP (Model Context Protocol) に対応したAIクライアント）。内蔵チャットUIは無い。
> 最終的な映像編集（タイムライン上書き・素材確定）も、接続したAIエージェントが行う半自動システム。

## コンテナ構成

| コンテナ | ポート | GPU | 役割 |
|---------|--------|-----|------|
| scripting-agent | 8002 | | 脚本・絵コンテ生成（パイプラインの中心） |
| scrapping-agent | 8003 | | 映像素材収集・管理＋AI画像生成の制御 |
| tts-agent | 8004 | | テキスト→音声変換（APIレイヤー） |
| director-agent | 8005 | | 各エージェントへの命令中継・進捗表示（司令塔） |
| editing-agent | 8006 | | 編集情報（コマ割り・尺・字幕・OTIO）生成 |
| irodori-tts-server | 8088 | ✓ | TTS実推論エンジン（`--profile gpu`） |
| imagegen-agent | 8188 | ✓ | AI画像生成 ComfyUI（`--profile gpu`） |

## クイックスタート

前提: Docker Desktop（WSL2バックエンド／Mac/Linuxはネイティブ）+ Git + Python 3.10+。
GPUサービス（irodori-tts-server / imagegen-agent）には別途 NVIDIA GPU が必要（任意）。

1. このフォルダをダウンロード/展開する
2. インストーラを実行する

```powershell
# Windows
.\install.ps1                 # 軽量エージェントのみ
.\install.ps1 -Gpu             # GPUサービス込み
```

```bash
# Mac/Linux
chmod +x install.sh
./install.sh                  # 軽量エージェントのみ
./install.sh --gpu             # GPUサービス込み（NVIDIA + nvidia-container-toolkit が必要）
```

インストーラは冪等（再実行しても既存の `.env`・clone・モデルは保持される）。完了すると
`.mcp.json` が生成される（あなたの環境のパスを反映した、AIクライアント接続用の設定）。

3. **AIエージェントを接続する** — [Docs/MCP_CLIENT_SETUP.md](Docs/MCP_CLIENT_SETUP.md) を参照。
   Claude Code であれば、このフォルダで起動するだけで `.mcp.json` が自動認識される。

### 設定（`.env` 1枚）

利用者が編集するのは**ルートの `.env` 1枚だけ**（APIキー・モデル選択・ホスト依存値）。
雛形は `.env.example`。使う機能に応じて必要なキーだけ埋めればよい（LLM・ストック素材API・
画像生成API・TTS設定など）。

### 起動・停止（セットアップ後）

```bash
docker compose up -d                        # 開発（override自動適用・ホットリロード、軽量エージェント）
docker compose --profile gpu up -d          # + GPUサービス（irodori / imagegen）
docker compose -f docker-compose.yml up -d  # クリーン/本番相当（overrideなし）
docker compose down                         # 停止
```

### データ / モデルの置き場所（すべてホストの見えるフォルダ）

名前付きDocker Volumeは使わず、大容量データは全てバインドマウント。ユーザーが直接追加・差し替え・バックアップできる。

| パス | 内容 | 取得方法 |
|------|------|----------|
| `imagegen-agent/models/{checkpoints,vae,loras}` | SD / LoRA / VAE | UIでURL投入 → ここに保存（またはファイルを直接配置） |
| `shared/voices/irodori/` | 話者設定・参照音声 | ユーザー管理 |
| `tts-agent/irodori-models/` | Irodori HFモデルキャッシュ（数GB） | 初回GPU起動時にHFから自動DL |
| `shared/` | プロジェクト入出力 | 実行時に生成 |

## ドキュメント

- [データスキーマ](Docs/DATA_SCHEMA.md)
- [MCPクライアント接続設定](Docs/MCP_CLIENT_SETUP.md)
- [Windows利用時の注意](Docs/WINDOWS_TIPS.md)
- 各エージェント仕様: [リサーチ](Docs/01_research.md) / [TTS](Docs/04_tts.md) / [脚本](Docs/05_scripting.md) / [編集](Docs/06_editing.md)
- `mcp-agent/README.md` — MCPツール一覧・課金/不可逆操作の確認ゲートについて

## ライセンス

[MIT](LICENSE)
