# 06_editing.md
# editing-agent — ラフ編集データ（OTIO）生成コンテナ 仕様書＋実装計画

> **実装担当（Sonnet 4.6）へ：** 着手前に `MASTER_DESIGN.md`・`DATA_SCHEMA.md`・本書を読むこと。
> 本書セクション10の実装ステップを上から順に進め、1ステップ完了ごとに
> `Docs/WORK_LOG.md` に✅記録すること。設計判断で迷ったら作業を止めて確認を求めること。

---

## 1. 役割・スコープ

各コンテナの生成結果（台本・TTS音声・素材）を**1本のラフ編集タイムライン**に統合し、
ホスト側の DaVinci Resolve に読ませるデータを生成する。

- **やること**: tts.json / footage.json / script.json を読み、OTIO（OpenTimelineIO）タイムラインと
  SRT字幕ファイルを生成する。LLMは使わない（純粋なファイル変換、処理は数秒）。
- **やらないこと**: 映像のレンダリング、トランジション・エフェクト演出、BGM（未実装領域）、
  カット尻の微調整（人間がResolve上で行う。あくまで「ラフ」）。

| 項目 | 値 |
|---|---|
| コンテナ名 | editing-agent |
| ポート | 8006 |
| 入力 | `episodes/epNN/script.json`, `tts.json`, `footage.json` |
| 出力 | `episodes/epNN/edit/timeline.otio`, `subtitles.srt`, `edit.json` |
| 依存ライブラリ | `opentimelineio==0.17.*`（pure Python、GPU不要） |

### なぜOTIO＋SRTの2ファイルか（重要な設計判断）
DaVinci Resolve（18以降）は `.otio` をネイティブインポートできるが、
**OTIO内のテキスト/字幕トラックをResolveの字幕トラックとして解釈しない**。
そのため字幕は標準のSRTファイルとして別出力し、Resolve側で
`File → Import → Subtitle` で字幕トラックに読み込む運用とする。

### 字幕FCPXML（Text+）オプション（2026-06-19 追加）
SRTはResolveの「字幕」トラックになるが、位置・色・縁取り等のスタイルを行ごとに
プログラム制御できない。これを補うため、**字幕を別途 `subtitles.fcpxml` としても出力**できる。
- ResolveはFCPXMLの `<title>`（Apple "Basic Title" effect 参照）を **Text+ クリップ**として取り込む。
- 重要: **OTIOのfcpxmlアダプタは使わない**。本リポジトリの opentimelineio 0.17 にfcpxmlアダプタは
  同梱されておらず（`['otio_json','otiod','otioz']` のみ）、かつOTIOのFCPXは `<title>` を吐かない。
  → `fcpxml_subtitle_writer.py` で **tts.json から lxml で直接** FCPXMLを生成する（SRT再パースもしない）。
- 位置づけ: SRTを**廃止せず追加**する。`subtitle_format: srt|fcpxml|both`（既定 both）で選択。
- 統合モデル: `subtitles.fcpxml` は**字幕トラックのみ**のFCPXML。Resolveで本線(timeline.otio)と
  併せてImportし、字幕トラックを本線へドラッグして合流させる（唯一の手動操作。UIガイドに明記）。
- スタイル忠実度（2026-06-19 Resolve実機で確定。詳細・再現スパイクは memory `fcpxml-resolve-subtitle-fidelity`）:
  - **焼ける**（コンテナ側でtext-styleに書けば反映）: `fontColor`（話者別色＝中核）/ `fontSize` /
    `bold` / `italic` / `strokeColor`+`strokeWidth`（縁取り）/ 位置 `<adjust-transform>`。
  - **焼けない**（UIに出さない）: Drop Shadow / `alignment=left` / param-key方式の位置 / Outside Only。
  - 位置: `<adjust-transform position="0 adjustY">` が Resolveの **Transform Position Y**（センター=0・下が−）に
    `Y = adjustY × 10.8` で焼ける。下三分の一 ≈ Transform Y `−250` → `adjustY ≈ −23.1`。
    UI/データは Transform座標（センター0・下マイナス、既定 `−250`）で持ち、writerが `÷10.8` して書く。
  - 役割分担: Outside Only・背景字幕ボックス・影 は **Davinciで全字幕を選択→一括設定**（焼けない代わり1回で済む）。

---

## 2. 入力データの実態調査結果（2026-06-13、「ラリーの秘密」ep01で確認）

### 2-1. file_path の基準ディレクトリが揃っていない（齟齬・要吸収）

| ファイル | file_pathの実例 | 基準 |
|---|---|---|
| tts.json (`audio_files[]`, `timeline[]`) | `episodes/ep01/audio/line_001.wav` | **プロジェクトルート相対** |
| footage.json (`clips[]`) | `footage/clip_003.mp4` | **エピソードdir相対** |

**対応方針**: 各コンテナのフォルダ構成・出力は変更しない（影響範囲が大きい）。
editing-agent のビルダー側で正規化する：

```
resolve_media_path(raw: str, project_dir: Path, episode_dir: Path) -> Path | None
  1) episode_dir / raw が存在すればそれ
  2) project_dir / raw が存在すればそれ
  3) どちらも無ければ None（warnings[]に記録、タイムライン上はGapで埋める）
```

### 2-2. その他の実態

- tts.json には `timeline[]` があり、`start_sec`/`end_sec`/`pause_after_sec` が**実測値で**入っている
  （ポーズ込みの絶対タイムライン。これをそのまま時間軸の正とする）
- 音声は 48kHz WAV、`audio/line_NNN.wav`
- footage.json の写真クリップは `duration_sec: 0.0`。**vecteezy由来の動画も duration 0.0 になり得る**
  （WORK_LOG 2026-06-12記載）→ `media_type=video` でも `duration_sec <= 0` なら写真と同様に扱う
- 同一セクションに複数クリップがあり、`line_ids` は重複する（例: intro に clip_003 と clip_004）
- 素材は縦動画（540x960）等の混在あり → Resolveが取り込み時にconformするのでラフカットでは許容
- プロジェクトIDに**日本語**がある（`ラリーの秘密`）→ パス/URLエンコードの検証が必須（セクション9）

---

## 3. 出力仕様

### 3-1. 出力フォルダ

```
shared/projects/{id}/episodes/epNN/
└── edit/                       ← editing-agent が書く（毎回上書き）
    ├── timeline.otio           ← Resolveにインポートするタイムライン
    ├── subtitles.srt           ← Resolveの字幕トラック用
    └── edit.json               ← 生成マニフェスト（下記）
```

### 3-2. edit.json スキーマ（DATA_SCHEMA.mdにStep 0で転記すること）

```json
{
  "schema_version": "1.0.0",
  "project_id": "ラリーの秘密",
  "episode": 1,
  "generated_at": "2026-06-13T10:00:00Z",
  "fps": 30,
  "path_style": "file_uri",
  "host_media_root": "<このリポのクローン先絶対パス>\\shared",
  "files": {
    "otio": "edit/timeline.otio",
    "srt": "edit/subtitles.srt"
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

### 3-3. project.json ステータス遷移（DATA_SCHEMA.mdにStep 0で転記すること）

- `episodes[].status` に **`editing` キーを追加**（MINOR変更：既存コンテナは無視して動作可能）
- editing-agent は実行時に `editing: running → done | error` を書く
- 正常完了時、`video_edit` が `not_started` なら `pending` に進める
  （= Resolve編集の準備が整ったことを意味する。`done` はホスト編集完了時に人間/video-editが付ける）

---

## 4. パスマッピング（コンテナ内パス → ホストパス）

OTIO/edit.json を読むのは**ホスト側のResolve**なので、メディア参照はホストのパスで書く必要がある。

- 環境変数 `HOST_SHARED_DIR`（例: `D:\Docker\Youtube-Auto\shared`）を導入
- コンテナ内 `/shared/projects/{id}/episodes/ep01/audio/line_001.wav`
  → `PureWindowsPath(HOST_SHARED_DIR) / "projects" / id / ...` に変換
- OTIOの `ExternalReference.target_url` への書き込み形式は環境変数 `OTIO_PATH_STYLE` で切替：
  - `file_uri`（既定）: `PureWindowsPath.as_uri()` → `file:///D:/Docker/.../line_001.wav`
    （日本語はpercent-encodeされる）
  - `windows`: 生のWindowsパス `D:\Docker\...\line_001.wav` をそのまま入れる
    （file_uri が Resolve実機で日本語パスをリンクできなかった場合のフォールバック。
    OTIOのtarget_urlは非URI文字列も許容され、Resolveは絶対パスも解釈する）

> 実装注意: パス連結は必ず `pathlib.PureWindowsPath` で行うこと（コンテナはLinuxなので
> `Path`で連結するとセパレータが壊れる）。`PureWindowsPath.as_uri()` は絶対パスで使用可能。

---

## 5. タイムライン構築アルゴリズム

### 5-0. 共通

- fps: リクエストパラメータ（既定は env `DEFAULT_FPS=30`）
- 秒→フレーム変換は `frame = round(sec * fps)` で**絶対時刻基準**（区間長の足し算で
  累積しない。丸め誤差のドリフト防止）
- `otio.schema.Timeline(name=f"{project_id}_ep{NN:02d}")`、
  `timeline.metadata["youtube_auto"]` に project_id / episode / generated_at / fps を記録

### 5-1. 音声トラック A1, A2, ...「話者ごと」（kind=Audio, 2026-06-23〜）

tts.json の `audio_files[].speaker_id` ごとに別トラックを割り当てる（出現順、`A{N}_{speaker_id}_{speaker_name}`）。
全トラックを同じ`(Gap, dur)`パターンで並行して進めることで、どのトラックも同じ絶対時刻に同期したまま、
自分の話者の行だけがClipになる（他話者の区間はGap）：

```
prev_end_frame = 0
for entry in timeline:  # tts.json timeline[] をorder順に処理
    start_f = round(entry.start_sec * fps)
    end_f   = round(entry.end_sec * fps)
    if start_f > prev_end_frame:
        全トラックに Gap(duration = start_f - prev_end_frame) を追加
    speaker = entryのline_idから引いたspeaker_id
    for track, sid in tracks:
        if sid == speaker:
            Clip(name=line_id, media_reference=ExternalReference(...), source_range=TimeRange(0, end_f-start_f))
        else:
            Gap(duration = end_f - start_f)
    prev_end_frame = end_f
```

- wavが実在しない行は warnings に記録し、全トラックに同尺のGapで埋める（同期維持）
- クリップの `metadata["youtube_auto"]` に speaker_name / text を入れる（Resolveでの確認用）
- **なぜトラック分割か**: 話者ごとにL/Rパンを振りたい要望があったが、OTIOのClip.effectsに
  汎用Effect(`effect_name="AudioPan"`)でパン値を持たせてもResolveのネイティブ.otioインポータは
  解釈しない（Pan=0.0のまま）ことを実機検証で確認済（`editing-agent/tmp/spike_pan_tracks.py`）。
  唯一の実用解は**トラック分割してResolve側でトラック単位のPan/Volumeを人間が設定する**こと。
  話者ごとに全選択もこのトラック分割で同時に解決する。

### 5-2. 映像トラック V1「Footage」（kind=Video）

1. footage.json の `clips[]` を `section` でグループ化（出現順保持）
2. 各セクションの**時間区間**を tts.json timeline から算出：
   `区間 = [グループ内全line_idsの最小start_sec, 最大(end_sec + pause_after_sec)]`
   （line_idがtts.jsonに無い場合はそのclipをスキップ＋warning）
3. セクション区間を、グループ内クリップ数で**頭から等分割**（MVP仕様。配分の最適化はResolve上で人間が行う）
4. 各クリップの配置：
   - `media_type=video` かつ `duration_sec > 0`:
     配置尺 = `min(clip.duration_sec, 割当尺)`。クリップ尺が足りない分は Gap
     （ラフカットなので編集者が埋める。ループ・freezeはしない）
   - `media_type=photo` または `duration_sec <= 0`:
     配置尺 = 割当尺フル（静止画は任意の尺で配置できる）
5. セクション間に素材の無い区間があれば Gap
6. セクション開始位置に `otio.schema.Marker`（name=セクションID、color=GREEN）を
   トラックではなく**タイムライン直下のstack由来でなくV1トラックの該当クリップ**…ではなく、
   実装簡略化のため **V1トラック上の各セクション先頭クリップに marked_range=クリップ先頭** で付与する

### 5-3. SRT字幕（subtitles.srt）

- tts.json `audio_files[]` を order順に、`timeline[]` の start/end を使って生成
- テキストは常に `text`（`processed_text` は使わない — 絵文字プレフィックスはTTS用であり字幕に出さない）。
  `caption` は字幕本文に使わない（DATA_SCHEMA.md §2b: キャラの字幕表示名/TTS VoiceDesignスタイル
  指示であり行ごとの字幕テキストではない。混同すると話者名だけの字幕になる＝2026-06-23修正済みバグ）
- 時刻形式 `HH:MM:SS,mmm`
- オプション `speaker_prefix: bool`（既定false）: trueなら `ずんだもん: こんにちは…` 形式
- 将来拡張（MVP対象外）: 話者別SRT分割（Resolveで話者ごとに字幕スタイルを変えたい場合）

---

## 6. REST API

共通仕様（MASTER_DESIGN 8章）に準拠。処理が数秒で終わるため**同期実行**とし、
run は完了後にレスポンスを返す（バックグラウンドジョブ・キャンセル機構は持たない）。

```
GET  /health
     → {"status":"ok", "otio_version":"0.17.0", "host_shared_dir":"D:\\...", "configured": true}
GET  /projects                              ← shared/projects 一覧（既存コンテナと同形式）
GET  /projects/{id}/episodes                ← エピソード一覧＋前工程ステータス
POST /projects/{id}/episodes/{n}/edit/run
     body: { "fps": 30, "speaker_prefix": false, "path_style": "file_uri" }  ※全て任意
     → 200 { "ok": true, "edit": {edit.jsonの内容} }
     → 409 前工程未完（tts/footageがdone以外）※force=trueで強行可
     → 422 入力ファイル不正
GET  /projects/{id}/episodes/{n}/edit/result   ← edit.json をそのまま返す（無ければ404）
```

バリデーション（DATA_SCHEMA 9章準拠）:
- `episodes/epNN/tts.json` と `footage.json` の存在・schema_version確認
- `status.tts == done` / `status.footage == done` チェック（`force`でスキップ可）
- エラーは project.json `errors[]` へ append（stage="editing"）

---

## 7. WebUI（app/static/index.html、Alpine.js）

既存コンテナ（scrapping-agent等）のレイアウト・配色を踏襲。MVPは1画面：

1. プロジェクト選択ドロップダウン → エピソード選択
2. 前工程ステータス表示（scripting / tts / footage の done可視化、入力ファイルの有無）
3. オプション: fps（24/30/60）、字幕話者プレフィックスon/off
4. 「OTIO生成」ボタン → 実行 → 結果表示：
   - 出力3ファイルの**ホスト側絶対パス**（コピーしやすく表示）
   - warnings一覧
   - **Resolve取り込み手順ガイド**（静的テキストで常設）:
     ```
     1. Resolveのプロジェクトfpsを生成fpsに合わせる（タイムライン設定）
     2. File → Import → Timeline → timeline.otio
     3. メディアがオフラインの場合: Media Poolで右クリック → Relink
     4. File → Import → Subtitle → subtitles.srt → タイムラインの字幕トラックへ
     ```

---

## 8. コンテナ構成

```
editing-agent/
├── Dockerfile                  ← python:3.11-slim、pip install -r requirements.txt
├── docker-compose.yml          ← port 8006、youtube-auto-net（external: true）参加
├── requirements.txt            ← fastapi / uvicorn[standard] / opentimelineio==0.17.* / python-dotenv
├── .env.example
│     SHARED_DIR=/shared
│     HOST_SHARED_DIR=D:\Docker\Youtube-Auto\shared
│     DEFAULT_FPS=30
│     OTIO_PATH_STYLE=file_uri
├── app/
│   ├── main.py                 ← FastAPI初期化、staticfiles
│   ├── api/routes.py
│   ├── core/
│   │   ├── project_manager.py  ← 既存コンテナ（director-agent）からコピー、読み書き対応
│   │   ├── timeline_builder.py ← セクション5のアルゴリズム（OTIO組み立て）
│   │   ├── srt_writer.py
│   │   └── path_mapper.py      ← セクション4（resolve_media_path / to_host_path / to_target_url）
│   └── static/index.html
└── tests/
    └── test_builder.py         ← フィクスチャは tests/fixtures/ に「ラリーの秘密」ep01の
                                   tts.json/footage.json/script.json をコピーして使用
```

> **教訓の遵守（WORK_LOG 2026-06-07）**: コンテナが参照するデータは必ず `app/` 配下または
> マウント済みボリューム内に置く。ホストの `reference/`・`Docs/` は実行時に存在しない。

docker-compose.yml の volumes は既存コンテナに合わせる：
```yaml
    volumes:
      - ./app:/app/app
      - ../shared:/shared
```

---

## 9. リスクと検証チェックリスト（Resolve実機 E2E）

実装完了後、ユーザーのResolve（ホスト側）で以下を確認する。
**齟齬が出た場合の調整箇所も併記**（フォルダ構成の変更はこれらで解決しない場合の最終手段）。

| # | 確認項目 | NGだった場合の調整 |
|---|---|---|
| 1 | timeline.otio がインポートできる（Resolve 18+） | OTIOバージョンを下げる / Resolveのバージョン確認 |
| 2 | 音声31クリップが正しい位置に並び、再生できる（話者ごとに分かれた複数トラック合計） | パスマッピング（HOST_SHARED_DIR）を確認 |
| 3 | **日本語プロジェクト名のパス**のメディアがリンクされる | `OTIO_PATH_STYLE=windows` で再生成して再試行 |
| 4 | 写真クリップ（jpg/png）が指定尺で配置される | 静止画の扱いをResolveが拒む場合はwarning化し手動配置に切替 |
| 5 | 動画クリップ（縦動画含む）が配置される | conform設定の案内をUIガイドに追記 |
| 6 | セクションマーカーが見える | マーカー付与位置（クリップ→トラック）を変更 |
| 7 | subtitles.srt のタイミングが音声と一致する | fps丸め処理を確認 |
| 8 | A1/A2が話者ごとに分かれ、片方のトラックを全選択しても他話者の行が混ざらない | `_build_audio_tracks` のspeaker_id紐付けを確認 |

その他のリスク:
- **opentimelineio 0.17系のAPI**: `otio.schema.ExternalReference(target_url=...)` 等の
  シグネチャはバージョン差がある。pinしたバージョンのドキュメントに従うこと
- **BGM・キャラ立ち絵は本コンテナのスコープ外**（キャラ画像はPhase 4保留中。
  将来 `character_assets.json` ができたら V2 トラックとして追加する設計余地だけ残す
  —— トラック構築を関数分離しておけば足せる）

---

## 10. 実装ステップ（Sonnet 4.6向けチェックリスト）

> 1ステップ完了ごとに WORK_LOG.md へ記録。compose変更時は `docker compose up -d --build` で反映。
> ネットワーク `youtube-auto-net` は external（既存）。新規作成しないこと。

### Step 0: スキーマ文書の先行更新（実装ルール: スキーマはDATA_SCHEMA.mdが先）
- [ ] `DATA_SCHEMA.md` に「6c. edit.json」追加（本書3-2）
- [ ] 同 1章のフォルダ構造・参照パス表に editing-agent 行と `edit/` を追加
- [ ] 同 2章 status に `editing` キー追加と遷移ルール（本書3-3）を記載
- [ ] tts.json / footage.json の file_path 基準ディレクトリの差異を**注記として明文化**（本書2-1）

### Step 1: コンテナ基盤
- [ ] フォルダ構造作成（本書8章）、Dockerfile / docker-compose.yml / requirements.txt / .env.example / .env
- [ ] `app/main.py` + `GET /health`
- [ ] **確認**: `docker compose up -d --build` → `curl http://localhost:8006/health` が200

### Step 2: コアビルダー
- [ ] `core/path_mapper.py`（resolve_media_path / to_host_path / to_target_url、PureWindowsPath使用）
- [ ] `core/timeline_builder.py`（本書5章。A1→V1→マーカーの順に実装）
- [ ] `tests/fixtures/` に「ラリーの秘密」ep01 の3JSONをコピーし、`tests/test_builder.py` 作成
  - 音声クリップ数=31、開始フレーム=timeline実測と一致、写真の配置尺、欠落ファイル→Gap+warning
- [ ] **確認**: `docker compose exec editing-agent pytest` 全通過。
  生成OTIOを `otio.adapters.read_from_file` でラウンドトリップできること

### Step 3: SRT＋マニフェスト＋ステータス更新
- [ ] `core/srt_writer.py`（本書5-3）
- [ ] edit.json 書き出し、project.json の `editing` ステータス更新＋`video_edit→pending`昇格＋errors追記
- [ ] **確認**: ユニットテスト（SRT時刻形式、caption優先、絵文字が混入しないこと）

### Step 4: REST API＋実データE2E
- [ ] `api/routes.py`（本書6章）
- [ ] **確認**: `POST /projects/ラリーの秘密/episodes/1/edit/run` →
  `shared/projects/ラリーの秘密/episodes/ep01/edit/` に3ファイル生成、
  edit.jsonのwarningsが妥当（clip_001/002はfootage.jsonに無い等の実態と整合）、
  ホスト側エクスプローラーからファイルが見えること

### Step 5: WebUI
- [ ] `static/index.html`（本書7章）
- [ ] **確認**: ブラウザで http://localhost:8006 → 生成→結果・ガイド表示

### Step 6: Resolve実機検証（ユーザー協働、ここはチャットで案内しながら）
- [ ] セクション9のチェックリストを上から消化
- [ ] NG項目があれば対応表に従い修正（特に#3 日本語パスは要注意）
- [ ] 結果をWORK_LOGに記録（OKだった path_style、Resolveバージョン）

### Step 7: director-agent 連携（別コンテナ改修、影響は新規エンドポイント追加のみ）
- [ ] `EDITING_AGENT_URL=http://editing-agent:8006` を env 追加
- [ ] `routes.py` に `/api/editing/{path:path}` 汎用プロキシ追加（既存 `/api/scrapping/` の実装を踏襲）
- [ ] UIの「編集情報生成」プレースホルダーボタンを実機能化（run実行→結果表示）
- [ ] **確認**: director-agent UIからOTIO生成が通ること

### Step 8: 仕上げ
- [ ] `app/mcp/server.py`（`edit_generate(project_id, episode, fps)` 1ツールのみ、既存パターン踏襲）
- [ ] `MASTER_DESIGN.md` 3章のeditng-agent行を「稼働中」に、14章相当の節を追記
- [ ] `PHASE_PLAN.md` 進捗サマリー更新
- [ ] WORK_LOG 最終記録

---

*作成: 2026-06-13（プラン: Fable 5 / 実装担当: Sonnet 4.6）*
