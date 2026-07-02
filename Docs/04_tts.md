# containers/04_tts.md
# tts-agent コンテナ 詳細仕様書

> **★TTS の正本（Single Source of Truth）はこのファイル。**
> `07_tts_stereo_and_defaults.md` / `TTS_IMPROVEMENT_v1.1.md` / `TTS_IMPROVEMENT_v1.2.md` /
> `SESSION_LOG_TTS_v1.2.md` は**履歴・参照のみ（ルール源にしない）**。仕様が変わったらここを直す。
> 解決済みの罠は [`TTS_IRODORI_CHECKPOINT_NOTE.md`](TTS_IRODORI_CHECKPOINT_NOTE.md)。

> **Claude Codeへ：** これはtts-agentコンテナの完全な仕様書です。
> 実装前に必ず `MASTER_DESIGN.md` と `DATA_SCHEMA.md` を読んでください。
> このコンテナはPhase 1として**最初に実装**します。

---

## 1. このコンテナの役割

`script.json`（または手動入力テキスト）を受け取り、音声ファイルを生成する。
プロジェクトの心臓部。単体でも完全に動作する。

**入力：**
- `script.json`（scripting-agentが生成）
- または直接テキスト入力（WebUI / API）

**出力：**
- `shared/projects/{id}/audio/line_XXX.wav`（各セリフの音声）
- `shared/projects/{id}/tts.json`（タイムライン・メタ情報）

---

## 2. アーキテクチャ概要

```
[tts-agent コンテナ :8004]
    ├── FastAPI + WebUI (Alpine.js)
    ├── REST API
    ├── MCPサーバー
    └── Irodori-TTS-Server クライアント
              ↓ HTTP (OpenAI互換API)
[Irodori-TTS-Server コンテナ :8088]  ← 既製品をそのまま使用
    └── Irodori-TTS-500M-v3 モデル
              ↓
    音声ファイル (.wav)
```

**重要な設計判断：**
Irodori-TTS-Serverは既にDockerfileとOpenAI互換APIを持つ完成品。
tts-agentはそのクライアントとして動作し、WebUI・プロジェクト管理・MCP・
キャッシュを担当する。これにより将来の他エンジンへの差し替えも容易。

---

## 3. フォルダ構造

```
tts-agent/
├── Dockerfile
├── docker-compose.yml          ← tts-agent単体起動用
├── docker-compose.full.yml     ← Irodori-TTS-Serverも含む完全版
├── requirements.txt
├── .env.example
├── app/
│   ├── main.py                 ← FastAPIエントリポイント
│   ├── api/
│   │   └── routes.py           ← REST APIエンドポイント
│   ├── core/
│   │   ├── tts_client.py       ← Irodori-TTS-Serverへのクライアント
│   │   ├── script_parser.py    ← script.json / テキストのパース
│   │   ├── emotion_mapper.py   ← emotionフィールド→絵文字変換
│   │   ├── cache_manager.py    ← TTSキャッシュ管理
│   │   └── project_manager.py  ← project.json読み書き
│   ├── mcp/
│   │   └── server.py           ← MCPサーバー
│   └── static/
│       └── index.html          ← WebUI (Alpine.js)
├── voices/                     ← 参照音声ファイル置き場
│   └── README.md
└── tests/
    └── test_tts.py
```

---

## 4. emotionフィールド → 絵文字マッピング

Irodori-TTSは絵文字を文中に挿入してスタイルを制御する。
`script.json`の`emotion`フィールドを以下のルールで変換する。

```python
EMOTION_TO_EMOJI = {
    "neutral":   "",           # 絵文字なし（デフォルト）
    "happy":     "😊",         # 楽しげに、嬉しそうに
    "excited":   "😆",         # 喜びながら
    "sad":       "😭",         # 悲しみ
    "serious":   "📖",         # ナレーション、モノローグ
    "question":  "🤔",         # 疑問の声
    "angry":     "😠",         # 怒り
    "surprised": "😲",         # 驚き
    "shy":       "🫣",         # 恥ずかしそうに
    "whisper":   "👂",         # 囁き
    "confident": "😎",         # 得意げに
    "worried":   "😟",         # 心配そうに
    "gentle":    "🫶",         # 優しく
    "fast":      "⏩",         # 早口
    "slow":      "🐢",         # ゆっくりと
    "narration": "📖",         # ナレーション
}
```

**適用ルール：**
- 絵文字はテキストの**冒頭**に挿入（例：`😊こんにちは！`）
- `neutral`の場合は絵文字なし、テキストそのまま
- `speed`フィールドと`emotion`の`fast`/`slow`が重複する場合はspeedを優先

---

## 5. WebUI仕様

### 画面構成（シングルページ）

```
┌─────────────────────────────────────────────┐
│  🎙️ TTS Agent                    [エンジン状態]│
├─────────────────────────────────────────────┤
│  [タブ: プロジェクト] [タブ: 直接入力]           │
├─────────────────────────────────────────────┤
│  プロジェクトタブ：                              │
│  ┌─────────────────────────────────────────┐ │
│  │ プロジェクト選択: [ドロップダウン]           │ │
│  │ script.json状態: ✅ 読み込み済み           │ │
│  │ セリフ数: 42行 / 推定時間: 4分32秒         │ │
│  └─────────────────────────────────────────┘ │
│                                               │
│  話者設定：                                    │
│  ┌──────────────┐  ┌──────────────┐          │
│  │ Speaker A     │  │ Speaker B     │          │
│  │ [声: sample▼] │  │ [声: alice▼]  │          │
│  │ [試聴 ▶]      │  │ [試聴 ▶]      │          │
│  └──────────────┘  └──────────────┘          │
│                                               │
│  [🎬 全セリフ生成]  [⏸ 中断]  [📁 出力フォルダ]  │
│                                               │
│  進捗: ████████░░░░  18/42 (42%)             │
│                                               │
│  ─── 生成ログ ───────────────────────────── │
│  ✅ line_001: ずんだもん → 3.2秒              │
│  ✅ line_002: 四国めたん → 2.8秒 [キャッシュ]   │
│  🔄 line_003: 処理中...                        │
└─────────────────────────────────────────────┘
```

### 直接入力タブ

```
┌─────────────────────────────────────────────┐
│  テキスト入力:                                 │
│  ┌─────────────────────────────────────────┐ │
│  │ こんにちは！テストです。                    │ │
│  └─────────────────────────────────────────┘ │
│                                               │
│  話者: [alice▼]  スタイル: [😊 happy▼]        │
│  速度: [1.0    ]  ステップ数: [40]             │
│                                               │
│  [▶ 生成]                                     │
│                                               │
│  ▶ 生成された音声（再生コントロール）            │
└─────────────────────────────────────────────┘
```

---

## 6. REST API エンドポイント

### 共通エンドポイント（全コンテナ共通）
```
GET  /health
GET  /projects
POST /projects/{id}/run
GET  /projects/{id}/status
POST /projects/{id}/cancel
```

### tts-agent固有エンドポイント

```
# 話者管理
GET  /voices                          ← 利用可能な参照音声一覧
POST /voices                          ← 参照音声アップロード
DELETE /voices/{voice_id}             ← 参照音声削除

# プレビュー
POST /preview                         ← テキスト→音声（即時返却）
Request: {
  "text": "こんにちは",
  "voice": "alice",
  "emotion": "happy",
  "speed": 1.0
}
Response: audio/wav (バイナリ)

# プロジェクト処理
POST /projects/{id}/run               ← script.jsonから全セリフ生成
POST /projects/{id}/run/line/{line_id} ← 特定セリフのみ再生成

# スクリプト直接投入（scripting-agent不使用の場合）
POST /projects/{id}/script/text      ← プレーンテキストをscript.jsonに変換
Request: {
  "text": "台本テキスト全文",
  "speakers": [
    {"id": "speaker_a", "voice": "alice"},
    {"id": "speaker_b", "voice": "bob"}
  ],
  "style": "dialogue"
}
```

---

## 7. MCPサーバー仕様

Claude Codeから自然言語で操作できるツール群。

```python
# ツール一覧
@mcp_tool
def tts_generate_project(project_id: str) -> dict:
    """プロジェクトのscript.jsonから全音声を生成する"""

@mcp_tool
def tts_preview(text: str, voice: str, emotion: str = "neutral") -> str:
    """テキストをプレビュー生成し、ファイルパスを返す"""

@mcp_tool
def tts_list_voices() -> list:
    """利用可能な参照音声の一覧を返す"""

@mcp_tool
def tts_regenerate_line(project_id: str, line_id: str) -> dict:
    """特定のセリフを再生成する"""

@mcp_tool
def tts_get_status(project_id: str) -> dict:
    """プロジェクトのTTS進捗を返す"""

@mcp_tool
def tts_upload_voice(file_path: str, voice_id: str) -> dict:
    """参照音声ファイルを登録する"""
```

---

## 8. Dockerファイル構成

### docker-compose.yml（tts-agent単体）

```yaml
version: "3.9"
services:
  tts-agent:
    build: .
    ports:
      - "8004:8004"
    environment:
      - SHARED_DIR=/shared
      - IRODORI_SERVER_URL=http://irodori-tts-server:8088
    volumes:
      - ${SHARED_DIR:-./shared}:/shared
      - ./voices:/app/voices
    depends_on:
      - irodori-tts-server

  irodori-tts-server:
    image: irodori-tts-server  # Irodori-TTS-Serverのイメージ
    ports:
      - "8088:8088"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    volumes:
      - irodori_models:/models
      - ./voices:/app/voices

volumes:
  irodori_models:
```

### .env.example

```bash
# 共有フォルダパス（ホスト側）
SHARED_DIR=./shared

# Irodori-TTS-Server設定
IRODORI_SERVER_URL=http://irodori-tts-server:8088
IRODORI_DEFAULT_VOICE=
IRODORI_DEFAULT_NUM_STEPS=40

# tts-agent設定
TTS_AGENT_PORT=8004
LOG_LEVEL=INFO

# キャッシュ設定
TTS_CACHE_ENABLED=true
TTS_CACHE_DIR=/shared/tts_cache
```

---

## 9. 処理フロー

```
POST /projects/{id}/run
    │
    ├── 1. project.jsonを読み込み、status確認
    │        scripting: done または skipped であること
    │
    ├── 2. script.jsonを読み込み、lines[]を取得
    │
    ├── 3. project.jsonのstatus.tts = "running"に更新
    │
    ├── 4. 各lineをループ処理：
    │   ├── キャッシュ確認（text + voice + engine のハッシュ）
    │   ├── キャッシュHIT → ファイルをコピー、スキップ
    │   └── キャッシュMISS →
    │       ├── emotion → 絵文字変換（emotion_mapper）
    │       ├── テキスト前処理（絵文字を先頭に付加）
    │       ├── POST /v1/audio/speech → Irodori-TTS-Server
    │       ├── 音声ファイルをshared/projects/{id}/audio/に保存
    │       └── キャッシュに登録
    │
    ├── 5. tts.jsonを生成（timeline含む）
    │
    └── 6. project.jsonのstatus.tts = "done"に更新
```

---

## 10. tts.jsonの更新仕様

`DATA_SCHEMA.md`のtts.jsonに加え、以下フィールドを追加：

```json
{
  "audio_files": [
    {
      "line_id": "line_001",
      "processed_text": "😊こんにちは！今週もAIニュースを一緒に見ていくのだ！",
      "emotion_emoji": "😊",
      "voice_id": "zundamon",
      "cache_hit": false,
      ...
    }
  ]
}
```

`processed_text`：絵文字を挿入後の実際にTTSに渡したテキスト（デバッグ用）

---

## 11. エラーハンドリング

| エラー | 対応 |
|--------|------|
| Irodori-TTS-Server接続不可 | project.jsonにエラー記録、status=error |
| 特定line生成失敗 | そのlineをスキップして続行、errorsに記録 |
| キャッシュ書き込み失敗 | 警告ログのみ、処理続行 |
| script.json不正 | status=error、処理中断 |

---

## 12. 実装チェックリスト

### Phase 1-A: 基盤
- [ ] フォルダ構造作成
- [ ] Dockerfile作成
- [ ] FastAPI基本セットアップ
- [ ] /healthエンドポイント
- [ ] 環境変数読み込み（.env）

### Phase 1-B: コア機能
- [ ] `tts_client.py` — Irodori-TTS-Serverへのリクエスト
- [ ] `emotion_mapper.py` — emotion→絵文字変換
- [ ] `cache_manager.py` — キャッシュ読み書き
- [ ] `script_parser.py` — script.json / テキストパース
- [ ] `project_manager.py` — project.json読み書き

### Phase 1-C: API
- [ ] `POST /preview` — 即時生成
- [ ] `POST /projects/{id}/run` — プロジェクト処理
- [ ] `GET /voices` — 話者一覧
- [ ] `POST /voices` — 音声アップロード

### Phase 1-D: WebUI
- [ ] index.html — Alpine.jsベース
- [ ] プロジェクトタブ
- [ ] 直接入力タブ
- [ ] 進捗表示

### Phase 1-E: MCP
- [ ] `mcp/server.py` — MCPサーバー実装
- [ ] 全ツール実装

### Phase 1-F: テスト・確認
- [ ] docker-compose up で単体起動確認
- [ ] /health レスポンス確認
- [ ] プレビュー生成確認
- [ ] プロジェクト処理確認
- [ ] キャッシュ動作確認

---

## 13. 実装時の注意点

1. **Irodori-TTS-ServerのDockerfileを流用する** — `docker-compose.full.yml`でサブコンテナとして組み込む。独自ビルドは不要。

2. **voices/フォルダを共有する** — tts-agentとIrodori-TTS-Serverの両方が同じvoicesフォルダをマウントする。

3. **GPUはIrodori-TTS-Serverに集中** — tts-agent自体はCPUのみで動作する。GPUリソースは全てIrodori-TTS-Serverに渡す。

4. **モデルは初回起動時にHuggingFaceから自動ダウンロード** — Dockerボリュームでキャッシュされるため、2回目以降は不要。

5. **OpenAI互換APIの`speed`パラメータ** — `speed`は0.25〜4.0。`script.json`の`speed`フィールドと同じ値をそのまま渡せる。

6. **長文は自動チャンキング** — Irodori-TTS-Serverが自動で分割処理するため、tts-agent側では対応不要。

---

*最終更新: 2025-06-03 | バージョン: 1.0.0*
