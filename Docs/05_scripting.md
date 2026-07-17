# 05_scripting.md
# scripting-agent — 設計仕様書

> **承認日:** 2026-06-05
> **ステータス:** 実装中

---

## 1. 概要

Research Groupが出力した記事要約・キーワード・ラフスクリプトを受け取り、
選択されたスタイル・キャラクター設定に従ってTTS向けの完成台本（script.json）に仕上げるコンテナ。

---

## 2. 確定アーキテクチャ

### 採用決定事項

| 項目 | 決定 | 理由 |
|---|---|---|
| MCPレイヤ | **採用**（tts-agentと同形式） | 外部オーケストレーター（Claude Code等）からのツール呼び出しに対応 |
| 自律ループ型Agent | **不採用** | scripting は人間レビューが価値の核心。自律ループは迂回になる |
| 2パス生成（軽量品質改善） | **採用** | 1パス目：ドラフト生成 / 2パス目：構造・バランス検証+自動修正 |

---

## 3. ディレクトリ構造

```
scripting-agent/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── app/
    ├── main.py
    ├── api/
    │   ├── __init__.py
    │   └── routes.py
    ├── core/
    │   ├── __init__.py
    │   ├── llm_client.py        ← LiteLLM統合（OpenRouter/ChatGPT/Gemini/Ollama）
    │   ├── script_generator.py  ← 2パス生成エンジン
    │   ├── script_validator.py  ← 構造チェック・TTS向けJSON検証
    │   ├── style_registry.py    ← スタイル定義ローダー
    │   └── project_manager.py  ← shared/projects 読み書き
    ├── styles/                  ← スタイル定義JSON（後から追加可能）
    │   ├── banter_duo.json      ← 茶番劇風二人掛け合い
    │   ├── news_narration.json  ← ニュースナレーション
    │   └── duo_with_guest.json  ← 二人掛け合い＋ゲスト
    ├── mcp/
    │   ├── __init__.py
    │   └── server.py            ← MCPサーバー（tts-agentと同形式）
    ├── static/
    │   └── index.html           ← WebUI (Alpine.js)
    └── tests/
        └── test_scripting.py
```

---

## 4. データフロー

```
[INPUT]
  shared/projects/{id}/research.json   ← 記事要約・キーワード
  shared/projects/{id}/rough_script.txt ← ラフ台本（任意）

      ↓ スタイル選択 + LLM選択

[PASS 1] LLM呼び出し → ドラフト生成
  - スタイルJSONのプロンプトテンプレートを使用
  - キャラクター設定・構成セクションを注入

      ↓

[PASS 2] 検証・自動修正（script_validator.py）
  - 行数・話者バランスチェック（ルールベース）
  - 感情タグ・pauseの付与・正規化
  - TTS向けJSONスキーマ準拠チェック

      ↓ ユーザー確認

[ユーザーレビュー]
  - WebUIで行単位プレビュー（話者カラー表示）
  - 行クリックで直接編集
  - チャット欄でフィードバック → 再生成

      ↓ 承認

[OUTPUT]
  shared/projects/{id}/script.json     ← TTS向け完成台本
  project.json の status.scripting = "done" に更新
```

---

## 5. スタイル定義スキーマ

```json
{
  "style_id": "banter_duo",
  "style_name": "茶番劇風二人掛け合い",
  "description": "明るくテンポよい二人の掛け合い。ボケとツッコミ構造。",
  "speakers": [
    {
      "id": "speaker_a",
      "name": "ずんだもん",
      "role": "ツッコミ",
      "tone": "明るい・好奇心旺盛・語尾は「なのだ」",
      "default_emotion": "happy"
    },
    {
      "id": "speaker_b",
      "name": "四国めたん",
      "role": "ボケ",
      "tone": "落ち着き・少しズレた発言・丁寧語",
      "default_emotion": "neutral"
    }
  ],
  "structure": ["intro", "main_topic", "discussion", "summary", "outro"],
  "target_line_count": 30,
  "balance_ratio": {"speaker_a": 0.5, "speaker_b": 0.5},
  "prompt_template": "..."
}
```

**スタイルの追加方法:** `styles/` フォルダに新しいJSONファイルを置くだけ。コード変更不要。

---

## 6. LLM選択設計

LiteLLM を使用し、以下のプロバイダーを統一インターフェースで扱う：

| プロバイダー | LiteLLM モデル文字列 | 環境変数 |
|---|---|---|
| OpenRouter | `openrouter/openrouter/free`（無料モデル限定方針） | `OPENROUTER_API_KEY` |
| ChatGPT | `openai/gpt-4o` | `OPENAI_API_KEY` |
| Gemini | `gemini/gemini-1.5-pro` | `GEMINI_API_KEY` |
| Ollama（将来） | `ollama/llama3` | `OLLAMA_BASE_URL` |

`.env` の `DEFAULT_LLM_MODEL` でデフォルト指定。UIとAPIリクエストで上書き可能。

---

## 7. REST API エンドポイント

```
GET  /health
GET  /styles                              ← 利用可能スタイル一覧
GET  /llm-models                          ← 利用可能LLM一覧
POST /styles                              ← スタイル新規作成（series_mode含む）
PUT  /styles/{style_id}                   ← スタイル更新
PATCH /projects/{id}/style                ← プロジェクトにスタイルを紐付ける
     body: { style_id }                   ← 副作用: style.speakers[]をconfig.tts.speakers[]の
                                             初期値として同期する（generate_script以外の経路でも
                                             配役の入れ物が空にならないように・2026-07-17修正）
POST /projects/{id}/generate              ← 台本生成
     body: { style_id, llm_model, rough_script?, extra_instruction?, target_line_count? }
POST /projects/{id}/generate-series       ← シリーズ（複数話）生成。各話を独立episodes/epNN/へ保存
     body: { style_id, llm_model?, rough_script?, episode_count?, extra_instruction? }
     ※ style.series_modeはUIのボタン表示だけをゲートする値でこのAPI自体はチェックしない
POST /projects/{id}/regenerate            ← フィードバックを元に台本全体をゼロから再生成
     body: { feedback, llm_model?, episode_number }
     ※ 既存ドラフトの本文はプロンプトに含まれない（ラフ台本+SEOキーワード+feedbackから作り直す）
       ＝フル生成とほぼ同じトークンコストが毎回かかる。部分修正なら下のregenerate-linesを使う
POST /projects/{id}/episodes/{n}/regenerate-lines  ← 指定行だけをLLMで書き直す（可逆・低コスト）
     body: { line_ids: [...], feedback, llm_model? }
     ※ 台本全文をコンテキストに渡すが出力は変更行のみ＝regenerateよりトークンコストが低い。
       line_idsは飛び飛びでも良い（全文脈を毎回渡すため文脈は崩れない・実機検証済み）。
       行の追加・削除はできない（既存行の書き換え専用）
POST /projects/{id}/approve               ← 承認 → script.json確定
POST /projects/{id}/episodes/{n}/import   ← 外部で書いた完成台本を取り込む（LLM生成スキップ）
     body: { script, title?, confirm?, style_name?, estimated_duration_sec?, llm_model? }
     ← confirm=false(既定)はドラフト保存のみ・true は即時確定
     ← style_name/estimated_duration_sec/llm_modelは台本タブ(スタイル/推定時間/使用LLM)の
       表示用メタデータ（自己申告値・検証されない）。style_name未指定時は「オリジナル」と表示
     query: force?                        ← 確定済み話への上書きは force=true 必須
GET  /projects/{id}/script                ← 現在の台本取得
PATCH /projects/{id}/script/line/{order} ← 行単位直接編集
POST /projects/{id}/script/line           ← 行挿入（after_orderの直後。0で先頭）
DELETE /projects/{id}/script/line/{order} ← 行削除
PATCH /projects/{id}/script/line/{order}/move  ← 隣接行と入れ替え（同一section内のみ・2026-07-17追加）
     body: { direction: "up"|"down" }     ← セクション境界をまたぐ移動は400で拒否
GET  /projects                            ← プロジェクト一覧
GET  /docs                                ← FastAPI自動生成ドキュメント
```

行挿入/削除/移動/直接編集の4つはDirector-Agent UI専用の機能でMCPツール化していない
（人間が台本プレビューで直接いじる操作という位置づけ・2026-07-17時点）。

---

## 8. MCPツール一覧

外部からの実際の呼び名は `mcp-agent/tools.py` が正（このセクションの関数名はそれに合わせてある）。

```
list_styles()                                          → styles[]
create_style(style_name, description, speakers, structure, ...)  → 新規スタイル
update_style(style_id, ...)                            → スタイル更新
set_project_style(project_id, style_id)                → プロジェクトへスタイル紐付け＋配役初期化
create_project(title, channel?, slug?)                 → 新規プロジェクト
generate_script(project_id, episode_number, style_id, ...)  → script_draft
generate_series_script(project_id, style_id, episode_count?, ...)  → 各話のscript_draft
approve_script(project_id, episode_number)              → script.json path
import_script(project_id, episode_number, script, title?, confirm?, force?,
               style_name?, estimated_duration_sec?, llm_model?)  ← 外部台本取込
get_script(project_id, episode_number?, draft?)         → script JSON
regenerate_lines(project_id, episode_number, line_ids, feedback, llm_model?)  ← 部分再生成（低コスト）
list_projects()                                          → projects[]
project_status(project_id)                               → 話ごとの進捗
```

---

## 9. ユーザー確認フロー（WebUI）

```
[1] プロジェクト選択
[2] スタイル選択（カード形式）
[3] LLM選択
[4] 生成実行 → ローディング表示
[5] 台本プレビュー
    - 話者別カラー表示
    - 感情タグ・pause表示
    - 行クリック → インライン編集
[6] アクション選択
    ├─ [承認] → script.json出力・project.json更新
    └─ [再生成] → チャット欄でフィードバック入力 → PASS1から再実行
```

---

## 10. output: script.json スキーマ

既存の `shared/projects/{id}/script.json` スキーマに準拠（DATA_SCHEMA.md参照）。
`metadata.style` フィールドに使用スタイルIDを記録。

---

*最終更新: 2026-06-05 | バージョン: 1.0.0*
