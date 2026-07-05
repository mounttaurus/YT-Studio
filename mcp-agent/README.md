# mcp-agent — 全工程を自然言語で司令するMCPサーバー（Stage1）

YouTube Auto の全コンテナ（research-agentを除く）を**自然言語で司令**するためのMCPサーバー。
頭脳とエージェントループは **外部ホスト（Claude Code / Goose 等）に借りる**。
本サーバーは「ツール」だけを公開する薄い層で、実体は director(:8005) の既存プロキシ経由で各コンテナを叩く。

> 設計の本籍: `Docs/MCP_AGENT_RESEARCH.md`（能力カタログ）／ `Docs/SUCCESSOR_PUBLIC_PACKAGE.md`（後継=ワンパッケージ構想）
> 中核 `tools.py` は **現行MCPと後継ワンパッケージの共有1正本**（トランスポート/頭脳 非依存）。

## 構成
| ファイル | 役割 |
|---|---|
| `tools.py` | **ツール層（中核・1正本）**。純async関数＋副作用分類。後継ワンパッケージへ持ち出す資産 |
| `director_client.py` | director(:8005) への薄いHTTPクライアント（I/O分離） |
| `server.py` | stdio MCPサーバー（tools.py を FastMCP で包むだけ） |
| `config.py` | env 既定値（`DIRECTOR_URL` 他） |

## 前提
- director-agent(:8005) が起動していること（`docker compose up -d` → `curl http://localhost:8005/health`）。
- stdio 方式＝**新コンテナ・新ポートなし**。ホスト常駐プロセスとして外部ホストが起動する。

## セットアップ
```bash
cd mcp-agent
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
```

## Claude Code に挿す
リポ直下の `.mcp.json` に登録済み。Claude Code をこのリポで起動すると `yt-studio` サーバーが認識される
（初回は接続承認のプロンプトが出る）。動作確認: 「プロジェクト一覧を見せて」→ `list_projects` が呼ばれる。

## 他のAgent UIに挿す
Claude Desktop / Cursor / Cline / Windsurf / Continue / VS Code / Goose 等の設定例は
[Docs/MCP_CLIENT_SETUP.md](../Docs/MCP_CLIENT_SETUP.md) を参照（接続情報3項目は本READMEの内容と同一）。

## 現在のツール（29個・`tools.py` の `TOOLS` レジストリが正本）

副作用は📖READ（読取専用）／✍️WRITE（可逆な書込）／🌐COST（外部API課金・クォータ消費）／
⚙️GPU（GPU占有）／⏳ASYNC（バックグラウンド実行）の組合せ。**COST/GPUは確認ゲート対象**
（`tools.needs_confirmation()` が True を返す）。

### コスト監視
| ツール | 副作用 | 説明 |
|---|---|---|
| `check_openrouter_credits` | 📖 READ | OpenRouter残高（累計購入額/消費額/残り） |
| `check_vecteezy_quota` | 📖 READ | Vecteezyダウンロードクォータ残 |

### 読み取り・プロジェクト
| ツール | 副作用 | 説明 |
|---|---|---|
| `list_projects` | 📖 READ | 全プロジェクト一覧 |
| `project_status` | 📖 READ | 指定話の進捗（status辞書・台本有無・行数） |
| `list_styles` | 📖 READ | 台本スタイル一覧（generate_script の style_id 候補） |
| `create_style` | ✍️ WRITE | 台本スタイルを新規作成（series_mode対応・shared/styles/にJSON保存） |
| `update_style` | ✍️ WRITE | ユーザースタイルをfull-replace更新（組み込みは不可） |
| `delete_style` | ✍️ WRITE | ユーザースタイルを削除（組み込みは保護） |
| `create_project` | ✍️ WRITE | 新規プロジェクト作成 |

### 台本（scripting）
| ツール | 副作用 | 説明 |
|---|---|---|
| `generate_script` | ✍️ WRITE | 台本をドラフト生成（可逆。approve まで確定しない） |
| `generate_series_script` | ✍️ WRITE | シリーズ台本を一括ドラフト生成（各話とも確定は別途approve） |
| `approve_script` | ✍️ WRITE | ドラフトを script.json に確定 |

### 素材収集（scrapping）
| ツール | 副作用 | 説明 |
|---|---|---|
| `generate_queries` | ✍️ WRITE | 確定台本→検索クエリ生成（要 approve 先行） |
| `search_footage` | ✍️🌐 WRITE+COST | 素材検索（外部API到達。要 queries 先行） |
| `auto_select_footage` | ✍️ WRITE | LLMが候補を自動選択（確定はしない） |
| `select_footage` | ✍️🌐 WRITE+COST | 採用候補をDL確定（不可逆・Vecteezyクォータ消費） |

### キャラ台帳・配役
| ツール | 副作用 | 説明 |
|---|---|---|
| `list_characters` | 📖 READ | 登録済みキャラ一覧 |
| `get_character` | 📖 READ | キャラ1件の詳細（appearance_prompt/voice等） |
| `create_character` | ✍️ WRITE | 新規キャラ登録 |
| `update_character` | ✍️ WRITE | 既存キャラの部分更新（声バインド差替含む） |
| `list_voices` | 📖 READ | 声カタログ一覧（voice_id群） |
| `assign_cast` | ✍️ WRITE | 役（speaker）にキャラを配役 |

### 音声合成（tts）
| ツール | 副作用 | 説明 |
|---|---|---|
| `run_tts` | ⚙️⏳ GPU+ASYNC | 全行を音声合成（バックグラウンド・ローカルGPU推論・外部課金なし） |

### ラフ編集（editing）
| ツール | 副作用 | 説明 |
|---|---|---|
| `build_timeline` | ✍️ WRITE | OTIO/SRT/FCPXML生成（要 tts/footage done） |

### 自由生成（imagegen・台本非依存）
| ツール | 副作用 | 説明 |
|---|---|---|
| `list_imagegen_styles` | 📖 READ | 自由生成で使えるスタイル一覧 |
| `free_generate` | 🌐⚙️ COST+GPU | テキストから画像生成（NanoBanana課金 or ComfyUI GPU） |
| `free_audio` | 🌐 COST | LyriaでBGM/効果音生成 |
| `free_save` | ✍️ WRITE | staging候補をdirect_output/へ確定保存 |

### 紙芝居パネル
| ツール | 副作用 | 説明 |
|---|---|---|
| `list_panel_presets` | 📖 READ | 表情/ポーズ/ショット/アングル/シーンのプリセットID |
| `generate_character_panel` | 🌐 COST | キャラの紙芝居パネル画像生成（NanoBanana） |

### リサーチ（research・探索→蒸留→ラフ台本）
| ツール | 副作用 | 説明 |
|---|---|---|
| `research_list_sources` | 📖 READ | 収集済みソース一覧 |
| `research_search` | 🌐 COST | Webグラウンディング検索でソース収集（要Gemini APIキー） |
| `research_add_source` | ✍️ WRITE | テキスト貼付/URL取得でソースを1件追加 |
| `research_digest` | ✍️ WRITE | 収集ソースを蒸留してラフ台本(rough_script.txt)を作成 |
| `research_get_digest` | 📖 READ | 蒸留結果（research メタ＋rough_script）を読み取る |

## Claude Code 許可設定の指針
MCPツールは既定で承認プロンプト(ask)が出る＝**COST/GPU系は既定のままで安全に守られる**。
摩擦を減らすなら READ系のみ allow に寄せる（`.claude/settings.json` の `permissions.allow`）:
```
mcp__yt-studio__list_projects, mcp__yt-studio__project_status, mcp__yt-studio__list_styles,
mcp__yt-studio__list_characters, mcp__yt-studio__get_character, mcp__yt-studio__list_voices,
mcp__yt-studio__list_panel_presets, mcp__yt-studio__list_imagegen_styles,
mcp__yt-studio__research_list_sources, mcp__yt-studio__research_get_digest,
mcp__yt-studio__check_openrouter_credits, mcp__yt-studio__check_vecteezy_quota
```
**COST/GPU系（search_footage / select_footage / run_tts / free_generate / free_audio /
generate_character_panel / research_search）は allow に入れない**（[[permission-allowlist-policy]]）。

## 課金ガード（重要）
書込/課金/GPUツールは `tools.py` で副作用クラス（READ/WRITE/COST/GPU/ASYNC）を持ち、
**現行は外部ホストの permission（ask）が無断実行を止める**。後継ワンパッケージではこの分類を
根拠に自前の確認ゲートを掛ける（`Docs/SUCCESSOR_PUBLIC_PACKAGE.md` §4＝公開時の製品安全要件）。
Claude Code以外のクライアントから接続する場合は確認ゲートの有無が異なるため
[Docs/MCP_CLIENT_SETUP.md](../Docs/MCP_CLIENT_SETUP.md) の注意点も合わせて確認すること。
