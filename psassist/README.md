# psassist — Photoshop 半自動組版

Photoshop のマンガパネル組版を**半自動化**する。Aロール ＝「セリフ1行＝マンガ1コマ」
のパネルが対象。**YT-Studio の一部**（`psassist/`）で、動くのは**ホスト側**。

**完全自動化ではない。**「既定値を機械が置き、違うところをユーザーが一言で直す」
のが方針。判断させるより、直すのを一瞬にする方が速いと実測で判断した。

## なぜ本リポに融合したか（2026-08-24）

当初は Photoshop MCP サーバーを立てて**外から YT-Studio を助けるスピンアウト**として
分離していた（`ps-assist` / `ps-assist-net`、ポート 8010・8011）。しかし
**MCP もコンテナも使わずに第1話196枚が完走した**ため、分離する理由が消えた。
`psassist-agent/`（API層・MCP層・compose）は棚上げで、実運用は下記のホスト工程だけ。

`shared/` を挟んで director と繋がる。**HTTP では繋がない** ── 検査がホストの
スクリプトである以上、叩ける API がそもそも存在しないため（`Docs/QA_UI_PLAN.md` §3）。

## 構成

```
[ホスト] scripts/*.py            プラン生成・抜き・検査（Photoshop 不要のものが多い）
[ホスト] host-bridge/*.py        win32com → Photoshop（COM に触れるのはここだけ）
      │  DoJavaScript
[Photoshop 2026]  jsx/*.jsx      無知能な実行係
      │  ファイル
[shared/] projects/.../psassist/ ← director-agent (:8005) が読む
```

**ホスト常駐は不可避。** Linux コンテナから Windows COM には到達できない。
YT-Studio の `HOST_SHARED_DIR`（DaVinci Resolve がホストの実パスを要求する）と同種の割り切り。

## 設定

**ルート `.env` 1枚**（本リポの規約）。`PSA_EPISODE_DIR` を対象エピソードへ向ける。
`PSA_BACKGROUNDS_DIR` は未設定なら `HOST_SHARED_DIR/backgrounds`、`PSA_BUBBLES_PSD` は
未設定なら `psassist/assets/bubbles.psd`。**ホスト固有の絶対パスをコードに埋めない**
（他のマシンで黙って存在しない場所を見に行くため）。

**JSX は徹底的に無知能にする。** Photoshop 内でのデバッグは高コストなので、
判断は全て Python 側に置く。唯一の例外がテキストの実測フィット（下記）。

## 6つの工程

| # | 工程 | 実装 | 実績（第1話196枚） |
|---|---|---|---|
| 1 | 背景抜き（Select Subject → 透過PNG） | `scripts/batch_cutout.py` | 196/196 成功・16分（4.6秒/枚） |
| 2 | バブル仮配置（bubbles.psd から複製・変形・反転） | `jsx/build_panel.jsx` | 重なり 0 が 90% |
| 3 | セリフ流し込み（禁則は Python で確定＋実測フィット） | `core/kinsoku.py` | 禁則違反 0・英数トークン分断 0 |
| 4 | 背景合成＋スマートフィルター | `jsx/build_panel.jsx` | 全カテゴリにブラー＋色調整を先置き |
| 5 | 合成結果の検査（QAレポート・`--watch`で保存を見張る） | `scripts/qa_check.py` | 196枚50秒・要対応14/助言9/問題なし173 |
| 6 | 納品用 1920×1080 PNG 書き出し | `jsx/export_png.jsx` | ベクター再描画のため Photoshop 必須 |

### 出来上がるレイヤー構造（下→上）

```
背景候補（psy_/com_ 非表示） … 目玉の切替で差し替え
部屋背景の予備 ×2（非表示）  … 光源判定が人の見え方とずれた時の控え
採用した背景（表示）          … SmartObject + ガウスぼかし SmartFilter
adjust                        … Hue/Saturation を中立値で先置き
eff_（集中線・非表示）        … キャラの後ろ・背景の前
Layer 0（抜き済みキャラ）
bubble_Talk N 1
text
```

### 吹き出しとキャラの重なり解消（3段階）

上の段ほど失うものが少ない。判定は透過マスクの2次元 AND で厳密に行う（AI 不要）。

| 段 | 手段 | 失うもの | 実績 |
|---|---|---|---|
| ① | キャラを移動 | なし | 101枚 |
| ② | キャラを縮小（バブルと反対側の下角が基点） | キャラが小さくなる | 11枚 |
| ③ | バブルを縮小 | 文字が小さくなる | 22枚 |
| — | 無変換 | — | 84枚 |

## 実測に基づく設計判断

すべて LUKAandAOI 第1話の既存PSD **129枚の全数解析**による。詳細は
`core/spec.py`（数値の正本）と `memory/psd-layout-has-no-rule.md`。

- **寸法の式は作らない。** 文字数とバブル寸法の相関は r=0.23 で、規則は存在しない
  （手作業のばらつき）。代わりに Photoshop で実描画を測り、収まるまでフォントを
  2px 刻みで縮める。この方式で **196枚中189枚が自動で収まる**
- **形状と左右は予測しない。** ルカの形状は emotion を使っても約60%、左右は64%止まり。
  既定＝最頻値を置き、`PATCH /plan/{line_id}` で1コール変更できるようにする
- **反転は資産に持たない。** ExtendScript の反転は1行・無劣化。しかも既定シェイプ
  （`Talk 10` / `Talk 1`）はどちらも中央尻尾なので、通常運用では反転が発生しない
- **縦書きは不採用。** 台本の23%に英数字を含み、`USA` `DNA` `.30-06` は縦中横に
  収まらない。プランに `direction` 項はあるので将来の切替は可能
- **1つの吹き出しに収まる上限は実測 151字**（`FONT_MIN`=30px・既定サイズのバブル）。
  第1話196枚の合成結果PSDから逆算した。次点は146字・135字と続き、収まらなかったのは
  **403字の1件だけ**（`line_168`）。`spec.SPLIT_MAX_CHARS`（140）は余裕を見た分割の目安。
  ⚠️ **分割提案は2分割固定にしない。** 403字を2分割しても1つ201字＝上限の1.3倍でまだ
  溢れる。`kinsoku.split_for_bubbles` は必要な数だけ割る（貪欲法＝順序固定なら最小分割数）
- **長すぎるセリフは尺の問題でもある。** `line_168` は音声59.9秒（中央値9.4秒）で、
  1枚の静止画を1分持たせることになる。文字が入らない行は、**編集上も割るべき行**である
  ことが多い

## ホスト常駐 `host_worker.py`（推奨・director の🔍合成チェックと繋がる）

```bash
python scripts/host_worker.py
```

全プロジェクト・全エピソードの `psassist/` を1プロセスで見張る（`--episode` 指定は無い）。
**`qa_check.py --watch` を内包して置き換える**（2つ常駐させない）。director-agent の
🖼️Aロールタブ → 🔍合成チェックはこのプロセスの生死を見て「🖼 納品PNGを更新」ボタンを出す
（`shared/_psassist/worker.json` のハートビート・詳細は `Docs/AROLL_TAB_REDESIGN_PLAN.md` Phase 0）。
出しっぱなしにしておけば、PSDの保存監視も納品PNGの書き出しもここから動く。

### ⚠️ 見張り先（どの `shared` を見るか）

優先順位は **`--shared` → `PSA_SHARED_DIR` → `HOST_SHARED_DIR`**。

```bash
# リポと shared が揃っている通常の環境（HOST_SHARED_DIR が使われる）
python scripts/host_worker.py

# チェックアウトとは別の（例: 稼働中の）shared を見張る
python scripts/host_worker.py --shared "<別のリポ>/shared"
```

**`HOST_SHARED_DIR` に固定していないのは、psassist がホストのスクリプトだから。**
リポのチェックアウトと、コンテナがマウントしている `shared` が**同じとは限らない**
（`PSA_EPISODE_DIR` が既にその形＝別の `shared` 配下のエピソードを指せる）。
向き先を間違えても**エラーにならず、ただボタンが出ないだけ**なので、起動時に
**見つけたエピソード数を必ず表示する**。`0件` と出たら向き先が違う。

実害の記録（2026-08-30）: 開発チェックアウトの `HOST_SHARED_DIR` を見張ってしまい、
稼働中の director が読む `<稼働側>/shared/_psassist/worker.json` が永遠に生まれず、
「ボタンが出ない」だけが症状として出た。

工程1〜4（プラン生成・切り抜き・組版）は下記の通り単発実行のまま
（Phase 5 でジョブキュー化を検討）。

## 使い方

```bash
# 設定はリポジトリルートの .env 1枚（PSA_EPISODE_DIR を対象エピソードに向ける）。
# 以下は psassist/ から実行する。
python scripts/build_plan.py  # panel_plan.json を生成（Photoshop 不要）
python scripts/batch_cutout.py                    # 工程1（約9秒/枚）
python host-bridge/bridge.py --plan <plan> --lines line_006,line_040   # 工程2-4

# 工程5: 合成結果の検査（Photoshop 不要・196枚で約50秒）
python scripts/qa_check.py --episode <episode_dir>
# 単発の直し作業ならこれでもよい（保存するたびに その1枚だけ 検査し直す）。
# director と連携させたいなら上の host_worker.py を使う。
python scripts/qa_check.py --episode <episode_dir> --watch
# 工程6: 納品用 1920×1080 PNG（Photoshop を占有する。作業中は流さない）
# ⚠️ --resume は「PNGが存在すればスキップ」でmtimeを見ない。直した行の再書き出しには
#    --lines を明示すること（host_worker.py のジョブも同じ理由で --resume を使わない）。
python host-bridge/export_png.py --episode <episode_dir> --all --resume
```

検査結果は `psassist/qa_report.json` に出る。director-agent（:8005）の
**🖼️Aロールタブ → 🔍 合成チェック**が同じファイルを共有フォルダ越しに読んで、
全コマのサムネイル一覧＋指摘箇所の赤枠として表示する（**HTTPで繋がない**。
詳細は `Docs/QA_UI_PLAN.md` §3・§10）。

**直す作業のループ:** `--watch` を出しっぱなしにして Photoshop で保存すると、
その1枚だけ検査し直される。ブラウザでは `🔄 再読込` を押すだけ。
直したのに書き出し直していない行は `EXPORT_STALE`（書き出しが古い）として助言に出るので、
**「検査は緑なのに納品物は古い」という一番危ない状態**を取りこぼさない。

ホスト側の追加依存: `pip install psd-tools`（`scripts/qa_check.py` が PSD を読む）。

`assets/bubbles.psd`（9レイヤー・正位置のみ）はユーザーが用意する。
仕様は `assets/README.md`。**このPSDの見た目がそのまま出力の見た目になる。**

## 罠

- **`doc.paste()` を画像の取り込みに使わない。** 内容をキャンバス中央に置くため元座標が
  壊れる。`layer.duplicate(targetDoc, ...)` を使うこと。実害: 抜き済みキャラが 704px →
  397px へ約307px ずれ、**196枚全部**が吹き出しに被った。同じコード内で `duplicate` を
  使っていた吹き出しは正しかったため、**キャラだけがずれる**という気づきにくい形で出た
- **検証は計算値でなく出力PSDの bbox で行う。** 上のバグはプラン側の計算では
  「重なり0」と出ていた。1枚は必ず実物のレイヤー bbox を読んで突き合わせる
- **重なりは2次元で測る。** 「左端 vs 右端」の1次元比較では、吹き出しの下にはみ出た肩を
  重なりと誤判定する
- **結合キーは `line_id` のみ。** 既存PSDのファイル名 `panel_040_line_040.psd` は
  先頭に `order` を焼いており、台本に行を挿入すると全部ズレる（YT-Studio
  `Docs/DATA_SCHEMA.md` §6d が警告する既知の罠）
- **光源判定は人の見え方とずれることがある。** `light_dx` は「画像のどこが明るいか」で
  測るので、窓が画面中央寄りに描かれた絵では機械と人の判断が食い違う（実例 line_014）。
  実害の無い程度だが、部屋背景の予備2枚を控えさせて目玉の切替で直せるようにしている
- **`aroll.json` の `background_id` は実作業と一致しない。** 自動割当は全パネルに
  `loc_*`（場所背景）を振るが、実際はルカのリアクション面に `psy_*`（心理）・
  `com_*`（コミック）を当てている。仮置きとして扱い、コンタクトシートを見て直す
- **Photoshop は単一プロセス。** バッチ実行中に別の COM 操作を挟まない
