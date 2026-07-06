# DATA_SCHEMA.md
# YouTube Auto — コンテナ間データスキーマ 完全定義書

> **Claude Codeへ：** このファイルはコンテナ間で受け渡されるすべてのJSONの仕様です。
> 各コンテナの実装時は必ずこのスキーマに従ってください。
> スキーマの変更が必要な場合は、このファイルを先に更新してから実装してください。
> **現在のスキーマバージョン: 2.0.0（episodes/構造）**

---

## 1. フォルダ構造とファイル依存関係

```
shared/projects/{YYYYMMDD_seq_slug}/
├── project.json              ← 全コンテナが読み書き（episodes[]でエピソード単位管理）
├── research.json             ← research-agent が書く
├── rough_script.txt          ← ラフ台本（任意）
├── drafts/                   ← 名前付き保存アーカイブ（参照用・後続パイプライン非対象）
│   └── {name}.json
├── episodes/
│   ├── ep01/
│   │   ├── script_draft.json ← scripting-agent が書く（作業中）
│   │   ├── script.json       ← scripting-agent が承認後に書く（確定・後続が読む）
│   │   ├── tts.json          ← tts-agent が書く
│   │   ├── footage.json      ← scrapping-agent が書く
│   │   ├── audio/            ← tts-agent が生成した音声ファイル
│   │   ├── footage/          ← scrapping-agent が確定DLした素材（Bロール）
│   │   ├── a_roll/           ← scrapping-agent が書く（Aロール: マンガ形式パネル＋aroll.json。§6d）
│   │   └── edit/              ← editing-agent が書く（OTIO+SRT+マニフェスト）
│   │       ├── timeline.otio
│   │       ├── subtitles.srt
│   │       └── edit.json
│   └── ep02/
│       └── ...（シリーズの場合）
└── output/                   ← 最終書き出し（video-edit が書く）
    └── ep01_final.mp4
```

### 共有ライブラリ（プロジェクト横断・再利用）
プロジェクト配下とは別に、`shared/` 直下に再利用ライブラリを置く（どのプロジェクトからも参照）。

```
shared/
├── characters/{char_id}/     ← キャラクター・ライブラリ（登場人物の唯一の本籍。§2b）
│   ├── character.json         ←   名前・外見・声バインディング・字幕名
│   ├── reference/             ←   一貫性の見本画像
│   └── generated/             ←   生成画像
├── voices/{engine}/           ← 声カタログ（選択可能なリファレンス音声。エンジン名前空間）
│   └── irodori/               ←   フラット運用: 1音声ファイル=1声、stem=voice_id（例: KUJO-OK.mp3 → KUJO-OK）
├── styles/                    ← 台本スタイル（scripting-agent。掛け合い/ナレ等の雛形。series_mode=trueで複数話シリーズ用。作成はUI/POST /styles/MCP create_style）
└── imagegen/                  ← 画風スタイル(styles.json)＋構図プリセット(panel_presets.json)
```

### スタイルエクスポート/インポート（2026-06-23追加）

**エンドポイント:** `GET /styles/{style_id}/export`（組み込み/ユーザー定義どちらも可）／`POST /styles/import`（scripting-agent）

```json
{
  "kind": "yt-studio-style",
  "export_schema_version": "1.0.0",
  "exported_at": "2026-06-23T10:00:00Z",
  "style": { /* style.json そのもの（prompt_template・balance_ratio等含む全フィールド） */ }
}
```

- インポートは封筒形式と生のstyle JSON（`speakers`/`structure`直下）の両方を受理する。
- **常に新規ユーザースタイルとして取り込む**＝既存スタイル（組み込み含む）の `style_id` と衝突する場合は自動で連番を振り直す（上書き不可）。`is_default` は常に `false` にリセットする（取り込みでアプリ全体の既定を変えないため）。
- `speakers[].character_id` が移行先に存在しなくてもエラーにせず未割当として安全縮退する。

※エンジンのモデル重み/キャッシュ（`tts-agent/irodori-models` のHFキャッシュ等）は各エンジン側に
置き、ここ（声カタログ）には含めない。声カタログは「人が差し替え・追加する選択可能な声」だけを持つ。

### 後続コンテナが参照するパス（重要）
| コンテナ | 読むファイル | 書くファイル |
|---|---|---|
| scripting-agent | `research.json`, `rough_script.txt` | `episodes/epNN/script_draft.json`, `episodes/epNN/script.json` |
| tts-agent | `episodes/epNN/script.json` | `episodes/epNN/tts.json`, `episodes/epNN/audio/` |
| scrapping-agent | `episodes/epNN/script.json` | `episodes/epNN/footage.json`, `episodes/epNN/footage/`, `episodes/epNN/a_roll/`（aroll.json＋パネル画像） |
| editing-agent | `episodes/epNN/script.json`, `tts.json`, `footage.json` | `episodes/epNN/edit/timeline.otio`, `subtitles.srt`, `edit.json` |
| video-edit（ホスト, DaVinci Resolve） | `episodes/epNN/edit/timeline.otio`, `subtitles.srt` | `output/epNN_final.mp4` |

### ⚠️ file_path の基準ディレクトリの差異（重要・editing-agent実装時に注意）
コンテナ間で `file_path` の解釈基準が統一されていない。各コンテナの出力形式自体は変更しないため、
**file_pathを参照する側が両方の基準を試して解決すること**。

| ファイル | `file_path` の例 | 基準ディレクトリ |
|---|---|---|
| `tts.json`（`audio_files[]`, `timeline[]`） | `episodes/ep01/audio/line_001.wav` | **プロジェクトルート相対** |
| `footage.json`（`clips[]`） | `footage/clip_003.mp4` | **エピソードディレクトリ相対**（`episodes/epNN/`） |

解決順序の推奨実装（editing-agentの`path_mapper.resolve_media_path`参照）:
1. `episode_dir / file_path` が存在すればそれを使う
2. なければ `project_dir / file_path` を試す
3. どちらも無ければ未解決として扱う（呼び出し側でフォールバック処理）

### 後方互換（schema_version 1.x プロジェクト）
起動時に自動マイグレーション済み。旧パス（`script.json`直置き・`audio/`直置き・`series/`）は
`_backup_{date}/` にコピーされ、新構造へ移行される。

---

## 2. project.json

**場所:** `shared/projects/{project_id}/project.json`
**役割:** プロジェクトのメタ情報とエピソード単位の進捗を管理する中心ファイル

```json
{
  "schema_version": "2.0.0",
  "id": "20250603_001_ai_news",
  "slug": "ai_news",
  "title": "AI最新ニュース",
  "channel": "default",
  "created_at": "2025-06-03T10:00:00Z",
  "updated_at": "2025-06-03T12:00:00Z",
  "language": "ja",
  "style": "banter_duo",
  "series_mode": false,

  "episodes": [
    {
      "number": 1,
      "title": "GPT-5発表の衝撃",
      "status": {
        "scripting":  "done",
        "tts":        "done",
        "footage":    "done",
        "editing":    "done",
        "video_edit": "pending"
      }
    },
    {
      "number": 2,
      "title": "価格と競合比較",
      "status": {
        "scripting":  "pending",
        "tts":        "not_started",
        "footage":    "not_started",
        "editing":    "not_started",
        "video_edit": "not_started"
      }
    }
  ],

  "pipeline_config": {
    "style_id":    "banter_duo",
    "llm_model":   "openrouter/anthropic/claude-sonnet-4-6",
    "tts_engine":  "aivis",
    "auto_approve": false
  },

  "config": {
    "llm_model": "openrouter/anthropic/claude-sonnet-4-6",
    "tts_engine": "aivis",
    "target_duration_sec": 300,
    "speakers": [],
    "tts": {
      "engine": "aivis",
      "speakers": [
        { "id": "speaker_a", "role": "main", "character_id": "001" },
        { "id": "speaker_b", "role": "sub",  "character_id": "002" }
      ],
      "default_speed": 1.0,
      "default_pause_after_sec": 0.3
    }
  },

  "errors": []
}
```

### status フィールドの値
| 値 | 意味 |
|----|------|
| `not_started` | 未着手（前工程も未完了） |
| `pending` | 実行待ち（前工程完了、いつでも開始可能） |
| `running` | 処理中 |
| `done` | 正常完了 |
| `error` | エラー停止（`errors`フィールドに詳細） |
| `skipped` | スキップ（手動入力等で代替済み） |

### episodes[].status のキーと遷移（editing追加、2026-06-13）
| キー | 書き手 | 説明 |
|---|---|---|
| `scripting` | scripting-agent | 台本生成・承認 |
| `tts` | tts-agent | 音声生成 |
| `footage` | scrapping-agent | 素材収集・確定 |
| `editing` | editing-agent | OTIO/SRT生成（**新規追加・MINOR変更**） |
| `video_edit` | ホスト（人間/video-edit） | DaVinci Resolveでの本編集 |

editing-agentの遷移ルール:
- 実行開始時に `editing: running` を書く
- 正常完了時 `editing: done`。さらに `video_edit` が `not_started` の場合のみ `video_edit: pending` に進める
  （= Resolve編集の準備が整ったことを示す。`video_edit: done` はホスト側が手動で設定する）
- 失敗時 `editing: error` ＋ `errors[]` に `stage: "editing"` で詳細追記

既存コンテナはこの新キーを無視して動作できるためMINOR変更として扱う（schema_versionは2.0.0を維持）。

### style フィールドの値
| 値 | 意味 |
|----|------|
| `dialogue` | 掛け合い形式（話者A・B） |
| `monologue` | 一人語り |
| `narration` | ナレーション形式 |

### errors フィールドの構造
```json
"errors": [
  {
    "stage": "tts",
    "timestamp": "2025-06-03T12:30:00Z",
    "code": "ENGINE_UNAVAILABLE",
    "message": "Aivis Speechサーバーに接続できません",
    "recoverable": true
  }
]
```

### config.tts.default_speed / default_pause_after_sec（2026-06-13追加）
「全セリフ生成」実行時にUIから保存される、音声生成のデフォルト速度・デフォルトポーズ。
- `default_speed`（float、既定1.0）: script.json各行の`speed`の一括適用元の値
- `default_pause_after_sec`（float、既定0.3）: script.json各行の`pause_after_sec`の一括適用元の値

`tts-agent`の`POST /projects/{project_id}/speakers`が`speakers`と合わせて保存する。
director-agent UIの「全行に適用」操作で、script.json各行の`speed`/`pause_after_sec`を
この値で一括上書きできる（行ごとの個別編集は引き続き可能）。
既存プロジェクトにこのフィールドが無い場合はUI側で既定値（1.0/0.3）を表示する。

### プロジェクトエクスポート/インポート（完全版zip、2026-06-24追加）

**エンドポイント:** `GET /projects/{project_id}/export`（zip）／`POST /projects/import`（multipart）（scripting-agent）

- 台本/キャラ/スタイルのJSON封筒とは異なり、**`shared/projects/{project_id}/` 配下を丸ごとzip化**したもの
  （`project.json` がzipルート直下、`episodes/`・`output/`・`director_log.json` 等も全て含む＝台本・素材・音声・
  編集情報を含む完全な1パッケージ）。
- 数百MB〜GB規模になり得るため、**メモリ上ではなく一時ファイル経由**で処理する（エクスポートは一時zipを
  `shared/`配下に作成してストリーミング返却、インポートはアップロードを一時ファイルへ書き込んでから展開）。
  一時ファイルは処理完了後に必ず削除する。
- **インポート時の衝突方針**: `project_id`（`new_project_id`省略時は元のproject.jsonの`id`を使用）が既存と
  一致したら **409** で拒否。`new_project_id`（multipartフィールド）を指定した時のみ別IDで取り込む（上書き不可）。
  取り込み後は `project.json` の `id` を実際に作成したIDへ補正する。
- `character_id`/`voice_id` 等の参照が移行先に存在しなくてもエラーにせず未割当として安全縮退する。

### config.tts.speakers[]（話者→キャラ割当, 2026-06-15統合）
**話者の名前・声・字幕はここで再定義しない。** `character_id` でキャラクター・ライブラリ（§2b）を
参照し、名前・声・字幕はキャラから解決する（二重定義の排除）。

| フィールド | 意味 |
|---|---|
| `id` | 台本上の役。`script.json` の `lines[].speaker_id` と対応（例 `speaker_a`） |
| `role` | 役割（main/sub 等。台本生成用） |
| `character_id` | この役を演じるキャラ（`shared/characters/{char_id}`）。**名前/声/字幕の出所** |

- **解決**: 生成時に `speaker_id → speakers[].character_id → character.json` を都度参照
  （スナップショットしない＝ドリフト無し）。TTSパネルは「役にキャラを選ぶ」だけ。
- **後方互換**: 旧形式（`name`/`voice`/`caption` 直書き・`character_id` 無し）はインライン値に
  フォールバック。移行時はUIでキャラ割当を促す。

---

## 2b. character.json（キャラクター・ライブラリ）

**場所:** `shared/characters/{char_id}/character.json`
**書き手/読み手:** scrapping-agent（CRUD・画像生成）、tts-agent（声の解決）、director-agent（UI）

登場人物の唯一の本籍。名前・外見・**声**・字幕名・リファレンス画像を一元管理する。
台本の話者やTTSの声・紙芝居の外見はここを参照するだけ（二重定義しない）。

```json
{
  "schema_version": "1.2.0",
  "char_id": "001",
  "name": "AOI",
  "caption": "",
  "appearance_prompt": "...",
  "description": "",
  "voice": {
    "engine": "irodori",
    "voice_id": "test-man02"
  },
  "reference_meta": {
    "upload_001.png": { "label": "顔/髪型を維持 (short pink hair, blue eyes)" },
    "upload_002.png": { "label": "制服デザインを再現" }
  },
  "generations": []
}
```

| フィールド | 意味 |
|---|---|
| `caption` | 字幕表示名。空なら `name` を使う |
| `appearance_prompt` | 画像生成用の外見プロンプト（紙芝居/キャラ生成が参照） |
| `voice.engine` | 声カタログのエンジン名前空間。空なら `config.tts.engine` を採用 |
| `voice.voice_id` | 声カタログ内の項目 → `shared/voices/{engine}/{voice_id}.{ext}`（フラット運用＝stemがvoice_id。例 `KUJO-OK`→`KUJO-OK.mp3`）。字幕名・性格は character.json が本籍のため声側はファイル名のみ。※構造化dir(profile.json)は将来拡張余地として未使用 |
| `reference_meta` | `reference/` 内画像の役割ラベル overlay（`{filename: {label}}`）。NanoBanana生成時に `Image N: <label>` としてプロンプトへ反映（人物/制服/ポーズ等の役割分担）。ファイル実体（glob）が存在の正、これはラベルの上乗せのみ |

- **多言語/多エンジン**は将来 `voice` を配列 `voices: [{engine, lang, voice_id}]` に拡張可能（現状は単一）。
- **schema_version**: character.json は **1.2.0**（`reference_meta` 追加＝後方互換のMINOR）。
  旧キャラ（voice/reference_meta 無し）は未割当・ラベル空として扱う。

### キャラエクスポート/インポート（zipバンドル、2026-06-23追加）

**エンドポイント:** `GET /characters/{char_id}/export`（zip）／`POST /characters/import`（multipart）（scrapping-agent）
**書き手/読み手:** director UIキャラタブの「📤エクスポート/📥キャラインポート」ボタン

- zip構成: `character.json`（`generations`は除外＝`generated/`実体を伴わず無意味）＋ `reference/*`（参照画像のみ。`generated/`は除外＝容量大・再生成可能）＋ `voice/{voice_id}.{ext}`（`voice.engine`/`voice.voice_id` が指す音声ファイル本体。`shared/voices/{engine}/` から1本だけ同梱＝完全なワンパッケージ）。
- **声カタログはキャラ横断のグローバル名前空間**。インポート先に同名ファイルが既に存在する場合は**上書きせず流用**する（同じ声として扱う）。
- **インポート時の衝突方針**: `char_id` が既存と一致したら **409** で拒否。`new_char_id`（multipartフィールド）を指定した時のみ別IDで取り込む（上書き不可）。
- 取り込んだキャラは `voice`/`styles`/`reference_meta` 等の欠落フィールドを生成時の既定値で補う（旧スキーマのzipでも読める）。また `schema_version` は取り込み時に現行値へスタンプし直す（中身は新スキーマなのに版番号だけ古い、というドリフトを防ぐ。補完と再スタンプの本籍は `character_manager.normalize_character()`）。
- `voice.voice_id` が移行先の声カタログに存在しなくてもエラーにせず、UI側で「未割当」として表示する。

---

## 3. research.json（2026-06-21 新スコープで再定義）

**場所:** `shared/projects/{project_id}/research.json`
**書き手:** research-agent（:8001）
**読み手:** scripting-agent（参考来歴。台本生成の主入力は `rough_script.txt`）

> **スコープ転換の注記:** 旧 research.json は「トレンド/ニュース発掘→テーマ選定」型だった（2026-06-10 に冗長として不採用）。
> 新 research-agent は**ユーザーが持ち込む素材（ドキュメント・テキスト・URL）を読み込み整理し、目標尺に応じた
> 1本のラフ台本に蒸留する**ダイジェスト役。よって research.json は「来歴（どのソースのどの要点から、どんな構成の
> ラフ台本を作ったか）」の記録に再定義する。**台本生成への実入力は `rough_script.txt`**（既存の受け口を無改修で利用）。
>
> **研究工程の状態管理:** project.json に専用フィールドは足さない。`research.json` の有無と `status` で表す
> （`running` / `done` / `error`）。`rough_script.txt` が書かれていれば scripting 工程はそれを入力にできる。

```json
{
  "schema_version": "1.1.0",
  "project_id": "20250603_001",
  "status": "done",
  "generated_at": "2026-06-21T10:30:00Z",
  "target_duration_sec": 300,

  "engine": {
    "model": "gemini/gemini-2.5-pro",
    "grounding_used": true,
    "fallback_used": false
  },

  "sources": [
    {
      "id": "S1",
      "kind": "document",         // document(アップロード) | text(貼付) | url | grounded(検索発見) | transcript(音声書き起こし)
      "title": "企画メモ.pdf",
      "url": null,
      "chars": 8421,
      "added_at": "2026-06-21T10:10:00Z"
    },
    {
      "id": "S2",
      "kind": "grounded",
      "title": "OpenAI announces ...",
      "url": "https://example.com/news/001",
      "chars": 0,
      "added_at": "2026-06-21T10:12:00Z"
    }
  ],

  "reading_points": [
    { "text": "GPT-5は推論能力が大幅に向上した", "sources": ["S1", "S2"] },
    { "text": "競合との価格差が論点になっている",   "sources": ["S1"] }
  ],

  "outline": [
    { "order": 1, "section": "導入", "beat": "視聴者の関心を引くフック", "target_sec": 30, "sources": ["S1"] },
    { "order": 2, "section": "本論1", "beat": "GPT-5の進化点", "target_sec": 120, "sources": ["S1", "S2"] }
  ],

  "rough_script_chars": 2310     // 書き出した rough_script.txt の文字数（実体は別ファイル）
}
```

- `rough_script.txt`（プロジェクト直下・既存）＝**ラフ台本の実体**。research-agent が `outline` と `reading_points` を
  人が読める構成テキスト（タイトル案／章ごとの要点と根拠／語り口の方向性）に展開して書き出す。**最終セリフ化は
  scripting-agent の仕事**（research-agent は確定台本を書かない）。
- `sources[].kind` の `grounded` は Gemini の Google検索グラウンディングで自動発見した出典、`transcript` は
  Cloudflare Whisper で音声/動画ソースを書き起こしたもの。
- MINOR変更（既存コンテナは research.json を無視して動作可）。`schema_version` は research.json 単体で 1.1.0。

---

## 4. script.json

**場所:** `shared/projects/{project_id}/script.json`
**書き手:** scripting-agent
**読み手:** tts-agent, scrapping-agent

```json
{
  "schema_version": "1.0.0",
  "project_id": "20250603_001",
  "generated_at": "2025-06-03T11:00:00Z",
  "total_duration_sec": 298,

  "lines": [
    {
      "id": "line_001",
      "order": 1,
      "speaker_id": "speaker_a",
      "speaker_name": "ずんだもん",
      "text": "こんにちは！今週もAIニュースを一緒に見ていくのだ！",
      "emotion": "happy",
      "speed": 1.0,
      "pause_after_sec": 0.5,
      "section": "intro",
      "notes": ""
    },
    {
      "id": "line_002",
      "order": 2,
      "speaker_id": "speaker_b",
      "speaker_name": "四国めたん",
      "text": "今週は特にOpenAIの発表が話題になりましたね。",
      "emotion": "neutral",
      "speed": 1.0,
      "pause_after_sec": 0.3,
      "section": "main",
      "notes": "やや専門的な説明のため、ゆっくり目でも可"
    }
  ],

  "sections": [
    { "id": "intro", "label": "イントロ",   "line_ids": ["line_001", "line_002"] },
    { "id": "main",  "label": "メイン",     "line_ids": [] },
    { "id": "outro", "label": "アウトロ",   "line_ids": [] }
  ],

  "metadata": {
    "line_count": 2,
    "estimated_duration_sec": 298,
    "style": "dialogue",
    "checked_by_director": true,
    "check_passed": true,
    "check_notes": ""
  }
}
```

### speaker_id / speaker_name の解決（2026-07-01・表示ドリフト根治）
- `speaker_id` は台本上の役（`config.tts.speakers[].id` と対応）。**名前・声の唯一の本籍はキャラ**（§2b）。
- `speaker_name` は**表示キャッシュ**。scripting-agent は台本を返す時（`GET .../script`・`export-text`）に
  `speaker_id → config.tts.speakers[].character_id → character.json.name` で**都度解決して上書き**する
  （`_apply_live_speaker_names` / `character_reader.resolve_cast_names`）。保存済みの値はスナップショットのため、
  キャラをリネームしても表示が追従しない旧問題（例「Luka」のまま「ルカ」にならない）をこれで断つ。ディスクには書き戻さない。
- **話者は配役済みキャラから選ぶ（エキストラ廃止）**。UIの行編集の話者ドロップダウンは `character_id` を持つ役だけを出し、
  Voice はキャラ本籍から自動解決する（行編集で声を `project.json` へインライン保存しない＝旧スキーマ撤去）。
  `字幕表示名`は `caption`（§2b）、話者の同定名は `name`。台本プレビューが出すのは `name`。

### emotion フィールドの値
| 値 | 意味 |
|----|------|
| `neutral` | 通常 |
| `happy` | 明るい・楽しい |
| `sad` | 悲しい・落ち着いた |
| `excited` | 興奮・テンション高め |
| `serious` | 真剣・重要な情報 |
| `question` | 疑問・問いかけ |

### 台本エクスポート封筒（プロジェクト間の持ち込み用、2026-06-23追加）

**エンドポイント:** `GET /projects/{project_id}/episodes/{n}/export`（scripting-agent）
**書き手/読み手:** director・scripting 両UIの「📤台本エクスポート/📥台本インポート」ボタン

```json
{
  "kind": "yt-studio-script",
  "export_schema_version": "1.0.0",
  "exported_at": "2026-06-23T09:00:00Z",
  "source_project_id": "20250603_001",
  "source_episode": 1,
  "script": { /* script.json または script_draft.json そのもの */ }
}
```

- **インポート** `POST /projects/{project_id}/episodes/{n}/import` は封筒形式（`kind`+`script`）と
  script.json生の中身（`lines`直下）の**両方**を受理する＝ファイル名・ラップの有無に依存しない。
- **衝突方針**: 対象の話に既に確定済み `script.json` があれば **409** で拒否。`?force=true` を明示した時のみ上書きする
  （新規プロジェクト/空の話への取り込みが既定の運用で、誤上書きを防ぐ）。
- `speaker_id`/`character_id` は移行先に存在しなくてもエラーにせず未割当として安全縮退する（既存の未割当ガードと同じ流儀）。

---

## 5. tts.json

**場所:** `shared/projects/{project_id}/tts.json`
**書き手:** tts-agent
**読み手:** video-edit（ホスト側）

**音声形式:** 48kHz/16bit/ステレオ(2ch、L/R同一データのデュアルモノ)のWAV。
tts-agentがIrodori-TTS-Server生成のモノラル音声を自動でステレオ化して保存する。

```json
{
  "schema_version": "1.0.0",
  "project_id": "20250603_001",
  "generated_at": "2025-06-03T12:00:00Z",
  "engine": "aivis",

  "audio_files": [
    {
      "line_id": "line_001",
      "order": 1,
      "speaker_id": "speaker_a",
      "speaker_name": "ずんだもん",
      "text": "こんにちは！今週もAIニュースを一緒に見ていくのだ！",
      "file_path": "audio/line_001.wav",
      "duration_sec": 3.2,
      "sample_rate": 44100,
      "generated_at": "2025-06-03T12:00:05Z",
      "cache_hit": false
    }
  ],

  "timeline": [
    {
      "line_id": "line_001",
      "file_path": "audio/line_001.wav",
      "start_sec": 0.0,
      "end_sec": 3.2,
      "pause_after_sec": 0.5
    }
  ],

  "metadata": {
    "total_audio_duration_sec": 298.4,
    "file_count": 2,
    "engine_version": "aivis-2.0.1",
    "all_generated": true
  }
}
```

---

## 6. footage.json（2026-06-12 実装版）

**場所:** `shared/projects/{project_id}/episodes/epNN/footage.json`
**書き手:** scrapping-agent
**読み手:** editing-agent（予定）、video-edit（ホスト側）

```json
{
  "schema_version": "1.0.0",
  "project_id": "20250603_001",
  "episode": 1,
  "generated_at": "2025-06-03T12:30:00Z",

  "clips": [
    {
      "id": "clip_001",
      "candidate_id": "pexels_video_12345",
      "section": "intro",
      "line_ids": ["line_001", "line_002"],
      "type": "stock",
      "media_type": "video",
      "source": "pexels",
      "original_url": "https://pexels.com/video/12345",
      "file_path": "footage/clip_001.mp4",
      "duration_sec": 10.0,
      "resolution": "1920x1080",
      "keywords": ["AI", "technology", "future"],
      "license": "pexels-free",
      "notes": ""
    }
  ],

  "metadata": {
    "clip_count": 1,
    "total_footage_duration_sec": 310.0,
    "all_downloaded": true
  }
}
```

### フィールド補足
| フィールド | 値 |
|---|---|
| `id` | stock/AI由来は `clip_NNN`、ユーザー素材は `user_NNN`（ファイル名と一致） |
| `candidate_id` | 選択元候補のID（`pexels_video_*` / `pixabay_photo_*` / `ai_{style}_{seed}`）。UIのDL済みバッジ照合に使用。ユーザー素材には無し |
| `type` | `stock`（素材サイト・AI生成）/ `user`（ユーザーアップロード） |
| `media_type` | `video` / `photo` |
| `source` | `pexels` / `pixabay` / `ai` / `user_upload` |
| `license` | `pexels-free` / `pixabay-content-license` / `ai-generated` / `user` |

AI生成素材（source=ai）は確定時に `shared/footage_pool/ai_generated/{candidate_id}.png` にもコピーされる。

---

## 6b. footage_draft.json（作業中ファイル）

**場所:** `shared/projects/{project_id}/episodes/epNN/footage_draft.json`
**書き手・読み手:** scrapping-agent（クエリ生成→検索→選択の作業状態を保持）

```json
{
  "schema_version": "1.0.0",
  "project_id": "20250603_001",
  "episode": 1,
  "generated_at": "...",
  "searched_at": "...",
  "extra_prompt": "ユーザーが指定した追加指示",
  "sections": [
    {
      "section": "intro",
      "line_ids": ["line_001", "line_002"],
      "summary": "そのセクションの映像イメージ要約（日本語）",
      "queries": ["cat eyes closeup", "vertical pupil"],
      "candidates": [
        {
          "candidate_id": "pexels_video_12345",
          "media_type": "video",
          "source": "pexels",
          "query": "cat eyes closeup",
          "original_url": "...",
          "download_url": "...",
          "thumbnail_url": "...",
          "duration_sec": 8.0,
          "resolution": "1920x1080",
          "photographer": "...",
          "license": "pexels-free"
        }
      ],
      "selected": ["pexels_video_12345"]
    }
  ]
}
```

- `candidates` は検索のたびに総入れ替えされるが、AI生成由来（source=ai）の候補は温存される
- AI生成候補の `download_url` はコンテナ間URL（imagegen-agent:8188）、`thumbnail_url` はブラウザ用（localhost:8188）

---

## 6c. edit.json（2026-06-13 新規）

**場所:** `shared/projects/{project_id}/episodes/epNN/edit/edit.json`
**書き手:** editing-agent
**読み手:** video-edit（ホスト側 / DaVinci Resolve操作者）

script.json（字幕テキスト）・tts.json（音声・実測タイムライン）・footage.json（映像素材）を統合し、
同ディレクトリに生成した `timeline.otio` / `subtitles.srt` / `subtitles.fcpxml`（任意）のマニフェストを記録する。
`files.fcpxml` は `subtitle_format` が `fcpxml`/`both` のときのみ存在する（任意キー＝後方互換）。

```json
{
  "schema_version": "1.0.0",
  "project_id": "20250603_001",
  "episode": 1,
  "generated_at": "2026-06-13T10:00:00Z",
  "fps": 30,
  "path_style": "file_uri",
  "host_media_root": "<このリポのクローン先絶対パス>\\shared",
  "files": {
    "otio": "edit/timeline.otio",
    "srt": "edit/subtitles.srt",
    "fcpxml": "edit/subtitles.fcpxml"
  },
  "timeline": {
    "duration_sec": 257.85,
    "video_clip_count": 19,
    "audio_clip_count": 31,
    "subtitle_count": 31,
    "marker_count": 5
  },
  "warnings": [
    { "code": "MEDIA_NOT_FOUND", "message": "footage/clip_099.mp4 が見つかりません（Gapで代替）" }
  ]
}
```

### フィールド補足
| フィールド | 値 |
|---|---|
| `fps` | OTIO構築時のフレームレート（既定30、リクエストで24/60に変更可） |
| `path_style` | `file_uri`（`file:///D:/...`形式、既定） / `windows`（生のWindowsパス、日本語パスでfile_uriが機能しない場合のフォールバック） |
| `host_media_root` | `HOST_SHARED_DIR`環境変数の値。OTIO内のメディア参照の生成元パス |
| `warnings[].code` | `MEDIA_NOT_FOUND`（音声/素材ファイルが見つからずGapで代替） / `LINE_NOT_IN_TIMELINE`（footageのline_idsがtts.jsonのtimelineに無くセクション尺を算出できない） |

timeline.otio構成: V1（Footage、セクション単位で等分割配置）+ A1, A2, ...（話者ごとに別トラック、
tts.json timeline実測値で配置。OTIOのパン情報はResolveが解釈しないため、トラック分割が
Pan/Volumeをトラック単位でResolve側操作可能にする唯一の実用解）+ セクション開始位置にMarker。
subtitles.srtはtts.jsonのtimeline実測値とtts.jsonの`text`から生成
（`caption`は§2bの字幕表示名/TTS VoiceDesignスタイル指示であり字幕本文には使わない）。

### config.subtitle_style（2026-06-19 新規・字幕FCPXMLのスタイル本籍）

**場所:** `project.json` の `config.subtitle_style`（プロジェクト単位。話者別スタイルの単一本籍）。
editing-agent が `subtitles.fcpxml`（Text+）生成時に読む。**焼ける項目のみ**持つ（実機確定。
焼ける/焼けないの全マトリクスは memory `fcpxml-resolve-subtitle-fidelity` / Docs/06_editing.md）。

```json
{
  "config": {
    "subtitle_style": {
      "position_y": -250,
      "default": {
        "font": "Yu Gothic", "font_size": 72, "color": [1.0, 1.0, 1.0, 1.0],
        "bold": false, "italic": false,
        "stroke_color": [0.0, 0.0, 0.0, 1.0], "stroke_width": 3
      },
      "per_speaker": {
        "speaker_a": { "color": [1.0, 0.9, 0.2, 1.0] },
        "speaker_b": { "color": [0.0, 1.0, 1.0, 1.0] }
      }
    }
  }
}
```

| フィールド | 意味 |
|---|---|
| `position_y` | 縦位置。**Resolve Transform座標（センター=0・下が負）**。既定 -250 ≒ 下三分の一。writerが `÷(height/100)` して adjust-transform に焼く |
| `default` | 全話者の既定スタイル。`per_speaker[speaker_id]` が個別キーを上書き |
| `color`/`stroke_color` | RGBA 0–1 の配列 |
| `per_speaker` キー | tts.json の `speaker_id`。話者ごとに色等を変える（Resolveで一括変換しづらいのをコンテナ側で解決） |

未設定でも writer は `DEFAULT_STYLE`（白文字・縁取り3・position_y -250）で動作する。
Outside Only・背景ボックス・Drop Shadow はFCPXMLに焼けないため、Davinciで全字幕を選択して一括設定する。

---

## 6d. aroll.json（2026-07-05 新規 — Aロール＝マンガ形式パネル）

**場所:** `shared/projects/{project_id}/episodes/epNN/a_roll/aroll.json`
**書き手・読み手:** scrapping-agent（プロンプト生成→バッチ画像生成の作業状態＋成果の正本）
**画像:** 同ディレクトリ `a_roll/panel_{order:03d}_{line_id}.png`

Aロールの方針転換（2026-07）: Aロール＝素材取得ではなく**セリフ1行＝マンガ1コマ**のキャラ画像。
LLM（Gemini無料枠→OpenRouter無料）がセリフを解釈して演出プロンプトと登場キャラ(1〜2人)を判定し、
NanoBanana（参照画像同梱）でパネルを生成する。吹き出しはユーザーが編集時に手作業で載せる
（画像内には文字を描かせない）。Bロールは従来どおり素材タブ（footage.json）。

```json
{
  "schema_version": "1.0.0",
  "project_id": "20250603_001",
  "episode": 1,
  "aspect": "16:9",
  "style": "kamishibai",
  "generated_at": "...",
  "panels": [
    {
      "line_id": "line_001",
      "order": 1,
      "section": "intro",
      "speaker_id": "speaker_a",
      "speaker_name": "Luka",
      "text": "セリフ本文（UI表示用スナップショット）",
      "characters": ["002"],
      "prompt": "演出のみの英語プロンプト（表情/ポーズ/ショット/構図）",
      "prompt_source": "llm | user",
      "status": "pending | done | failed",
      "image": "panel_001_line_001.png",
      "provider": "nanobanana",
      "error": null,
      "generated_at": "..."
    }
  ]
}
```

- `prompt` は**演出部分のみ**。スタイル接頭辞（styles.json）とキャラ外見（appearance_prompt）は
  生成時に合成される＝後からスタイルを替えてもプロンプト再生成不要
- `prompt_source: "user"`（手編集）はプロンプト一括再生成（overwrite=true）でも保持される
- `characters` は char_id 最大2人。話者→キャラ解決の正本は `project.json config.tts.speakers[].character_id`
- 空テキスト行はパネル対象外（マニフェストに含まれない）
- バッチ生成は1行ごとにこのファイルへ書き出す＝中断・再開（only_missing）が常に安全
- OpenRouterへの課金退避は `allow_paid_fallback=true` の時だけ（既定OFF。Free表示でも実課金のため）

---

## 7. seo_pack.json（2026-07-06 新規 — YouTube SEOオプティマイザ）

**場所:** `shared/projects/{project_id}/seo_pack.json`
**書き手:** research-agent（`:8001`）
**読み手:** scripting-agent（台本生成時に `for_script` を注入）、director-agent（UI表示用）

ラフ台本からジャンル・シードキーワードをLLMで推定し、YouTube Data API v3で市場データ
（タグ・競合チャンネル・下克上動画・コメント分析）を収穫して、SEO強化指示を作成。

```json
{
  "schema_version": "1.0.0",
  "project_id": "20250603_001",
  "generated_at": "2026-07-06T10:00:00Z",
  "source_hash": "sha256(rough_script.txt)",

  "engine": {
    "model": "gemini/gemini-2.5-flash",
    "fallback_used": false
  },

  "genre_frame": {
    "genre": "都市伝説エンタメ",
    "audience": "20-40代の好奇心強い層",
    "competitor_archetypes": ["心霊スポット系YouTuber", "謎解き系チャンネル"],
    "seed_queries": ["都市伝説 怖い", "心霊スポット", "謎の失踪事件"],
    "own_keywords": ["都市伝説", "実話", "心霊", "謎", "未解決事件"]
  },

  "harvest": {
    "tags": [
      { "tag": "都市伝説", "count": 342 },
      { "tag": "心霊", "count": 289 }
    ],
    "channels": [
      {
        "channel_id": "UCxxxx",
        "title": "怖い動画公式",
        "subscribers": 1250000,
        "appearances": 8
      }
    ],
    "upset_videos": [
      {
        "video_id": "video_001",
        "title": "知られざる失踪事件の真相",
        "channel_title": "真実の光",
        "views": 5000000,
        "subscribers": 120000,
        "ratio": 41.67
      }
    ],
    "videos_analyzed": 150
  },

  "viewer_insights": {
    "praise": "実話系のストーリーに好反応。謎解きの過程が評価される",
    "complaints": "長い前置きは飽きられやすい。スキップが多い",
    "questions": "本当にあった事件なのか？根拠は？"
  },

  "gap_analysis": {
    "missing_tags": ["実話", "未解決", "検証"],
    "multiplier_ideas": "台本に『実際の証言者インタビュー』を盛り込むことで、下克上動画の差別化要因になる。",
    "title_patterns": ["～の真相【実話】", "【検証】～は本当にあった？"]
  },

  "for_script": "台本には以下の要素を盛り込むこと:\n- 『実話ベース』『検証』の明示（視聴者の信用獲得）\n- タイトルに【実話】【検証】を含める\n- コメント欄からよくある『証拠は？』に答える構成\n- 類似チャンネルより短く（平均6分以下）、テンポよく",

  "partial": false,
  "partial_reason": null
}
```

| フィールド | 意味 |
|---|---|
| `source_hash` | `rough_script.txt` のsha256。鮮度判定（再実行タイミングの判定に使う） |
| `engine.fallback_used` | APIキー未設定時のフォールバック（Gemini直叩き使用） |
| `harvest.tags[]` | YouTube動画が実際に使っている上位タグ（頻度順） |
| `harvest.channels[]` | シードクエリ複数に跨がって出現する競合チャンネル（出現回数順） |
| `harvest.upset_videos[]` | 購読者数に対して異常に再生数が多い「下克上動画」（比率2.0以上）。新進チャンネルが成功した事例 |
| `viewer_insights.*` | コメント欄自動分析（称賛・不満・疑問） |
| `gap_analysis.missing_tags` | 市場で使われているのに台本に無いキーワード |
| `gap_analysis.multiplier_ideas` | これを台本に足すと伸びやすい要素の提案 |
| `gap_analysis.title_patterns` | 伸びている動画のタイトルパターン例 |
| `for_script` | scripting-agent に注入するSEO指示文（「## SEO最適化情報」として prompt に追記） |
| `partial` | 日次クォータ超過で途中終了した場合 `true`。`partial_reason` に理由を記録 |

- **鮮度判定:** scripting-agent は台本生成時、`source_hash` が現在の `rough_script.txt` と一致して
  いれば既存 seo_pack を使用。異なれば再生成（ユーザーが台本を更新したと判定）。

---

## 7b. publish_pack.json（2026-07-06 新規 — 公開メタデータ）

**場所:** `shared/projects/{project_id}/episodes/epNN/publish_pack.json`
**書き手:** research-agent（確定台本から）
**読み手:** director-agent（動画公開時のメタデータ取得・Resolve連携用）

確定 `script.json` と `seo_pack.json` から YouTube公開メタデータ（タイトル案・概要欄・
ハッシュタグ・タグ）を自動生成。

```json
{
  "schema_version": "1.0.0",
  "project_id": "20250603_001",
  "episode": 1,
  "generated_at": "2026-07-06T11:00:00Z",

  "titles": [
    "【実話検証】都市伝説は本当か？最新の失踪事件を徹底調査",
    "都市伝説の真相を調べた結果【衝撃】",
    "検証 〜 本当にあった怖い事件の真実"
  ],

  "description": "00:00 イントロ\n05:30 事件の背景\n...\n\n【参考資料】\n- 〇〇新聞の報道\n- YouTubeコメント欄からの情報\n\n▼チャンネル登録はこちら\nhttp://...\n\n▼グッズショップ\nhttp://...",

  "hashtags": ["#都市伝説", "#心霊スポット", "#検証"],

  "tags": ["都市伝説", "心霊", "実話", "謎", "検証"]
}
```

| フィールド | 意味 |
|---|---|
| `titles[]` | YouTube側に提案する複数案（SEOに強い順） |
| `description` | 概要欄テンプレート（チャプター・関連リンク・利用素材を含む） |
| `hashtags` | ハッシュタグ（YouTube初期3個は上位タグとして有効） |
| `tags` | タグ設定用（YouTube側は最大30個、内容に無関係なタグは除外される） |

---

## 7c. youtube_cache/（2026-07-06 新規 — クォータ・レスポンスキャッシュ）

**場所:** `shared/youtube_cache/`
**管理:** research-agent（写字スレッド / 並行読み有）

YouTube Data API v3 呼び出しのクォータ予算管理とレスポンスキャッシュ。

```
shared/youtube_cache/
├── quota_ledger.json          ← 日次クォータ使用量台帳（太平洋時間 0時リセット）
└── responses/
    └── {sha1(endpoint+params)}.json  ← レスポンスキャッシュ（TTL 72時間既定）
```

### quota_ledger.json
```json
{
  "date_pt": "2026-07-06",
  "used": 4250
}
```
- `date_pt`: 太平洋時間での日付（YouTube APIのクォータは太平洋時間0時にリセットされるため、日付が変わったら`used`を0に戻す判定に使う）
- `used`: その日のクォータ使用量（毎日 0 からスタート。`YOUTUBE_DAILY_QUOTA_BUDGET=8000` が上限）
- 各API呼び出し前に`_check_and_reserve()` が消費コストをチェック。超過なら `QuotaBudgetExceeded`

### response cache

```json
{
  "fetched_at": "2026-07-06T10:00:00+00:00",
  "data": { /* YouTube API v3のレスポンス JSON */ }
}
```

| エンドポイント | クォータコスト | キャッシュ有効性 |
|---|---|---|
| `search.list` | 100 | あり（TTL適用） |
| `videos.list` | 1/50動画 | あり |
| `channels.list` | 1/50チャンネル | あり |
| `commentThreads.list` | 1 | あり |

---

## 7d. config.seo（project.json 新規フィールド、2026-07-06）

**場所:** `project.json` の `config.seo`
**役割:** 台本生成時の自動SEO最適化フラグ

```json
{
  "config": {
    "seo": {
      "auto": true
    }
  }
}
```

| フィールド | 既定 | 意味 |
|---|---|---|
| `auto` | `true` | scripting-agent が `POST /projects/{id}/generate` 実行時、自動で seo_pack を発火するかの制御 |

- `config.seo.auto=false` にセットすると、ユーザーが明示的に SEO ボタンを押さない限りスキップ（開発/テスト用）

---

## 8. キャッシュ管理

### tts_cache/
TTSの再生成を防ぐキャッシュ。同じテキスト・話者・エンジンの組み合わせならキャッシュを使用。

```
shared/tts_cache/
└── {md5(text + speaker_id + engine)}.wav
```

キャッシュインデックス: `shared/tts_cache/index.json`
```json
{
  "entries": [
    {
      "hash": "a1b2c3d4...",
      "text": "こんにちは！",
      "speaker_id": "speaker_a",
      "engine": "aivis",
      "file_path": "tts_cache/a1b2c3d4.wav",
      "created_at": "2025-06-03T12:00:00Z"
    }
  ]
}
```

---

## 8. スキーマバージョン管理ルール

- `schema_version` は `MAJOR.MINOR.PATCH` 形式
- **MAJOR** 変更：後方互換性なし（全コンテナ更新が必要）
- **MINOR** 変更：フィールド追加（既存コンテナは無視して動作可能）
- **PATCH** 変更：説明・コメントのみの修正
- 現在のバージョン: **1.0.0**

---

## 9. バリデーション

各コンテナは処理開始時に入力JSONのバリデーションを行う。
バリデーションエラーは`project.json`の`errors`フィールドに書き込み、処理を中断する。

必須チェック項目：
- `schema_version` の存在確認
- `project_id` の一致確認
- 前工程の`status`が`done`または`skipped`であること
- 必須フィールドの存在確認

---

*最終更新: 2025-06-03 | バージョン: 1.0.0*
