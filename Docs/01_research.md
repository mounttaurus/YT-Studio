# 01_research — research-agent（ラフ台本ダイジェスト）正本

> **本籍ドキュメント**。research-agent の役割・I/O・API・連携の正本はここ。
> 復活の経緯と全体位置づけは `PHASE_PLAN.md`（Phase 3）/ `MASTER_DESIGN.md`。スキーマは `DATA_SCHEMA.md §3`。
> 設計判断の蒸留は `memory/`。

## 1. 役割（旧版との決定的な違い）

| | 旧 research-agent（2026-06-10 不採用） | **新 research-agent（2026-06-21 復活）** |
|---|---|---|
| 立ち位置 | 「**何を作るか**」をAIが発掘（トレンド/ニュース→テーマ選定） | 「**何を作るか**」はユーザーが持ち込む |
| 仕事 | Web検索でネタ探し→research.json | 持ち込み素材を**読み込み整理して1本のラフ台本に蒸留** |
| 不採用理由との関係 | 円環構成で冗長化し不採用 | 上流発掘をしないので冗長に当たらない |

**一言でいうと:** ユーザーのドキュメント・テキスト・関連URL（＋任意の補助検索）を入力に、
**読み込みポイントを整理**し、**目標尺（長尺/短尺）に応じた構成**で**1本のラフ台本**を作り、
scripting-agent に渡す。最終セリフ化はしない（それは scripting の仕事）。

## 2. サービス
- ポート **8001**（GPU不要）。`docker-compose.yml` の base + override に定義。
- 入出力は全て `shared/projects/{id}/` のバインドマウント。

## 3. 頭脳（LLM）と協調

| 窓口 | 役割 | 鍵(env) | 備考 |
|---|---|---|---|
| **Gemini API（主）** | 長文脈での要点抽出・合成・検索グラウンディング | **`RESEARCH_GEMINI_API_KEY`**（無ければ `GEMINI_API_KEY` にフォールバック） | このプロジェクト専用キー。画像生成(NanoBanana)アカウントとクォータ分離 |
| **Cloudflare Workers AI（協調）** | ①テキストLLMフォールバック（Geminiがクォータ/レート超過時に Llama 3.3 70B へ自動退避）②Whisper STT で音声/動画ソースを書き起こし | `CLOUDFLARE_API_KEY` + `CLOUDFLARE_ACCOUNT_ID` | Geminiの**代替ではなく前後処理/保険**。埋め込みRAGでのソース選別はGeminiが丸呑みできるため当面不採用 |

**協調の線引き（バランス重視）:** Gemini=長文脈の合成頭脳。Cloudflare=保険＋音声の入口。
通常の素材量は Gemini 2.5 の無料大コンテキストが丸呑みできるので、チャンク化/ベクタ検索は導入しない。
（ソース量が常用で爆発したら埋め込みRAGを R+ で再検討＝保留）

## 4. データフロー

```
ユーザー: ドキュメント / 貼付テキスト / 関連URL / 目標尺
   │
   ▼ POST .../sources/upload | sources/text | sources/search
[research_sources.json]  ← 取り込み済みソースの作業セット
   │
   ▼ POST .../digest   （頭脳=Gemini, 任意でCloudflareフォールバック）
   │   ① ソース別 読み込みポイント抽出（引用付き）
   │   ② 目標尺に応じた構成（長尺=章立て深掘り / 短尺=要点圧縮）
   │   ③ 1本のラフ台本へ展開
   │
   ├─▶ rough_script.txt   ← scripting-agent が無改修で消費（既存の受け口）
   └─▶ research.json       ← 来歴（sources / reading_points / outline / engine）
```

## 5. REST API

| メソッド/パス | 入力 | 出力 | 副作用 |
|---|---|---|---|
| `GET /health` | — | status/設定状況 | 📖 |
| `GET /projects` | — | プロジェクト一覧 | 📖 |
| `GET /models` | — | 利用可能モデル（Gemini/Cloudflare） | 📖 |
| `GET /projects/{id}/sources` | — | research_sources.json | 📖 |
| `POST /projects/{id}/sources/upload` | file(pdf/docx/txt/json/音声) | source | ✍️（音声は🌐Whisper） |
| `POST /projects/{id}/sources/text` | title?, text? / url? | source | ✍️（urlは🌐取得） |
| `POST /projects/{id}/sources/search` | query, max? | 追加sources | 🌐🧠 Gemini検索グラウンディング |
| `DELETE /projects/{id}/sources/{sid}` | — | — | ✍️ |
| `POST /projects/{id}/digest` | target_duration_sec, model?, extra_instruction? | rough_script + research.json | ✍️🧠🌐 **核** |
| `GET /projects/{id}/digest` | — | rough_script + research.json | 📖 |

## 6. 連携（疎結合）
- **scripting への接続は `rough_script.txt` を書くだけ**。scripting-agent は既に
  `POST /projects/{id}/generate` の `rough_script` 入力 / `read_rough_script()` でこれを読む（無改修）。
- director-agent からは汎用プロキシ `/api/research/{path}` で中継（既存4本と同型・更新系は監査ログ）。

## 7. 罠・注意
- 専用キー `RESEARCH_GEMINI_API_KEY` を `.env` に追加後は `--force-recreate` が必要
  （env追加はリロードだけでは未反映。[[docker-env-file-hotreload]]）。
- グラウンディングは google-genai ネイティブ（`tools=[google_search]`）で引用URLを取る。LiteLLM経由は不安定。
- Geminiのモデル名は腐る（提供終了で404）。GA名で持つ／フォールバックを併用（[[gemini-direct-model-names-go-stale]]）。
- ポート 8001 と `youtube-auto-net` は1環境ずつ。旧stackを `down` してから `up`。
