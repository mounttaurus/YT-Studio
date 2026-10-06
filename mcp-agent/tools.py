"""ツール層（MCP-Agentの中核・トランスポート/頭脳 非依存）。

ここは「現行 stdio MCPサーバー」と「後継ワンパッケージ」の **共有1正本**。
FastMCP からも素の関数呼び出しからも使えるよう、純粋な async 関数で書く
（Docs/SUCCESSOR_PUBLIC_PACKAGE.md §2）。

各ツールは副作用クラスを持つ（READ/WRITE/COST/GPU/ASYNC）。
COST/GPU は将来の課金ガード（§4）の確認ゲート対象。現行は外部ホストの permission が
ask で守るが、後継ではこの分類を根拠にツール層自身が確認ゲートを掛ける。

パラメータ名は実コードのリクエストモデルに厳密に合わせている（推測でAPIを作らない）:
  generate→GenerateRequest / search→SearchRequest / select→SelectRequest / edit→EditRunRequest 等。
"""
import asyncio
import base64
import time
from enum import Enum
from pathlib import Path
from typing import Optional

import director_client as dc


class SideEffect(str, Enum):
    READ = "read"      # 📖 読み取りのみ・安全
    WRITE = "write"    # ✍️ ファイル書込（可逆だが状態変更）
    COST = "cost"      # 🌐 外部API課金/クォータ消費
    GPU = "gpu"        # GPU占有
    ASYNC = "async"    # ⏳ バックグラウンド実行


# COST/GPU を含むツールは確認ゲート対象（後継ワンパッケージで強制／現行はホストのaskが守る）。
_CONFIRM = {SideEffect.COST, SideEffect.GPU}


def needs_confirmation(side_effects: list[SideEffect]) -> bool:
    return bool(_CONFIRM.intersection(side_effects))


# ── コスト監視（累計の実額・クォータ残） ──────────────────────────────
#
# セッション内カウンタではなく、外部サービス側のアカウント実額/実クォータを問い合わせる。
# 1呼び出しごとの確認ゲート(COST分類)を補完する「累計の可視化」。費用は発生しない(READ)。

async def check_openrouter_credits() -> dict:
    """OpenRouterの残高(累計購入額total_credits/累計消費額total_usage/remaining)を返す。

    NanoBanana・Lyriaがフォールバックで使う分もこの残高に含まれる(同一OPENROUTER_API_KEY共有)。
    Gemini直叩き分(画像/音声)はこの残高には現れない(別会計・追跡手段なし=既知の残課題)。
    """
    return await dc.get("api/scripting/llm-usage")


async def check_vecteezy_quota() -> dict:
    """Vecteezyのダウンロードクォータ残等のアカウント情報を返す(無料枠は月500件)。

    検索/サムネ表示は無料・確定DL(select_footageでvecteezy選択時)のみクォータを消費する。
    """
    return await dc.get("api/scrapping/vecteezy/account")


async def check_youtube_quota() -> dict:
    """YouTube Data APIの日次クォータ台帳（太平洋時間リセット・自主予算8000）を返す。

    外部API課金監視の一環＝generate_publish_pack 等の SEO 工程の影響を把握する。
    """
    return await dc.get("api/research/youtube/quota")


# ── 読み取りツール ────────────────────────────────────────────────

async def list_projects() -> list[dict]:
    """全プロジェクトの一覧（id / title / channel / episodes）を返す。

    どのプロジェクトに対して作業するかを決める起点。書き込みは一切しない。
    """
    data = await dc.get("projects")
    return data.get("projects", []) if isinstance(data, dict) else data


async def project_status(project_id: str) -> dict:
    """指定プロジェクトの各話の進捗（status辞書・台本有無・行数）を返す。

    「次に何ができるか」を判断する材料。status は
    {research, scripting, tts, footage, video_edit} を含む（欠けは未着手）。
    project_id は前方一致で解決される（director準拠）。
    """
    data = await dc.get(f"projects/{project_id}/episodes")
    episodes = data.get("episodes", []) if isinstance(data, dict) else data
    return {"project_id": project_id, "episodes": episodes}


async def list_styles() -> list[dict]:
    """利用可能な台本スタイル一覧を返す。generate_script の style_id の候補。

    enum を静的スキーマに埋め込めないため、生成前にこれで有効な style_id を確認する。
    """
    data = await dc.get("api/scripting/styles")
    return data.get("styles", data) if isinstance(data, dict) else data


async def set_project_style(project_id: str, style_id: str) -> dict:
    """プロジェクトにスタイルを紐付ける。スタイルの話者定義を config.tts.speakers[] の初期値として
    同期する副作用がある（既存の手動配役は上書きしない・可逆WRITE）。

    generate_script/generate_series_script は生成の副作用として自動でこれを行うが、
    import_script で外部台本を取り込む場合はこれを先に呼ばないと、配役(assign_cast)が
    「unknown speaker_id」で失敗する（config.tts.speakers[] が空のため）。
    外部台本モードでは create_style → set_project_style → (必要なら assign_cast) →
    import_script の順で呼ぶこと。
    """
    return await dc.request(
        "PATCH", f"api/scripting/projects/{project_id}/style",
        json={"style_id": style_id},
    )


async def create_style(style_name: str, description: str,
                       speakers: list[dict], structure: list[dict],
                       target_line_count: int = 30,
                       line_count_mode: str = "auto",
                       content_mode: str = "short",
                       series_mode: bool = False) -> dict:
    """新規の台本スタイルを作成する（shared/styles/ にJSON保存・可逆WRITE）。

    speakers[] = {name, voice_id, character_id, role, tone, default_emotion}。
    voice_id/character_id は当てずっぽう禁止＝先に list_voices / list_characters で実在値を確認する。
    structure[] = {id, label, description}（説明に目安行数を書くとLLMが従う）。
    content_mode: short(3分前後)|long(8分超)。series_mode=True で前後編等の複数話シリーズ用になる。
    line_count_mode: auto|fixed。fixed時のみ target_line_count が厳密目標になる。
    prompt_template と balance_ratio はサーバ側で自動生成される（話者比率は均等割り）。
    """
    body = {"style_name": style_name, "description": description,
            "speakers": speakers, "structure": structure,
            "target_line_count": target_line_count,
            "line_count_mode": line_count_mode,
            "content_mode": content_mode, "series_mode": series_mode}
    return await dc.request("POST", "api/scripting/styles", json=body)


async def update_style(style_id: str, style_name: str, description: str,
                       speakers: list[dict], structure: list[dict],
                       target_line_count: int = 30,
                       line_count_mode: str = "auto",
                       content_mode: str = "short",
                       series_mode: bool = False) -> dict:
    """ユーザースタイルを丸ごと上書き更新する（full-replace・組み込みスタイルは404）。

    部分更新ではない＝ list_styles で現状を読み、変更を織り込んだ全体を渡すこと
    （read-modify-write。assign_cast と同じ流儀）。パラメータの意味は create_style と同じ。
    """
    body = {"style_name": style_name, "description": description,
            "speakers": speakers, "structure": structure,
            "target_line_count": target_line_count,
            "line_count_mode": line_count_mode,
            "content_mode": content_mode, "series_mode": series_mode}
    return await dc.request("PUT", f"api/scripting/styles/{style_id}", json=body)


async def delete_style(style_id: str) -> dict:
    """ユーザースタイルを削除する（組み込みスタイルは404で保護される・ファイル1枚の削除）。

    既存プロジェクトが参照している style_id を消すと再生成時に選び直しが必要になる。消す前に一言確認。
    """
    return await dc.request("DELETE", f"api/scripting/styles/{style_id}")


async def create_project(title: str, channel: str = "default",
                         slug: Optional[str] = None) -> dict:
    """新規プロジェクトを作成する(shared/projects/にディレクトリ生成)。一気通貫フローの起点。"""
    body = {"title": title, "channel": channel}
    if slug is not None:
        body["slug"] = slug
    return await dc.request("POST", "api/scripting/projects/new", json=body)


# ── 台本（scripting / 可逆WRITE） ────────────────────────────────

async def generate_script(project_id: str, episode_number: int, style_id: str,
                          extra_instruction: Optional[str] = None,
                          rough_script: Optional[str] = None,
                          llm_model: Optional[str] = None,
                          target_line_count: Optional[int] = None) -> dict:
    """台本を生成してドラフト(script_draft.json)に保存する（確定ではない＝可逆）。

    style_id は list_styles の値から選ぶ。生成後 approve_script で確定するまで script.json は変わらない。
    target_line_count を指定するとこの話だけスタイル既定の行数を上書きする（厳密目標＝fixed 扱い）。
    未指定ならスタイル既定の行数・モードに従う。
    llm_model省略時は既定(直接Anthropic API経由のSonnet 4.6)を使用。モデルをテストしたい時のみ明示指定する。
    OpenRouter経由(openrouter/...)を指定する場合は無料モデル限定（有料は拒否される）。有料モデルを
    使いたい場合は anthropic/... , openai/... , gemini/... のオリジナルAPIを直接指定すること
    （OpenRouterは中間業者でマージンが乗るため、同じモデルを直接叩く方が安い）。

    ⚠️ **承認済み台本がある話でこれを呼び直すと、次の approve_script で line_id が
    振り直され、Aロールの絵とTTSの音声の紐付けが全滅する。** 言い回しの修正だけなら
    regenerate_lines を使うこと。呼ぶ前に project_status で当該話の生成物の有無を確認する
    （UIにはこの確認ダイアログがあるが、MCP経由はガードが無い＝ここが唯一の歯止め）。
    """
    body = {"style_id": style_id, "episode_number": episode_number}
    if extra_instruction is not None:
        body["extra_instruction"] = extra_instruction
    if rough_script is not None:
        body["rough_script"] = rough_script
    if llm_model is not None:
        body["llm_model"] = llm_model
    if target_line_count is not None:
        body["target_line_count"] = target_line_count
    return await dc.request("POST", f"api/scripting/projects/{project_id}/generate", json=body)


async def approve_script(project_id: str, episode_number: int) -> dict:
    """指定話のドラフトを承認し script.json として確定し、続けてAロールのプロンプトと
    背景の自動割当まで用意する（director:8005 🖼️Aロールタブの「承認」ボタンと同じ挙動）。

    ⚠️ **従来の承認（一括）。行ワークベンチの運用では `adopt_llm_proposal`（LLMの案を正本へ採用）→
    `confirm_lines`（行の確定・音声の自動作り直し・コマの下ごしらえ）に分かれた**（LINE_WORKBENCH_PLAN D8）。
    確定の運用が始まっている話数（`get_line_states` の enabled=true）では、こちらの承認は下書きを丸ごと
    正本へ上書きするので使わない（差分を見て選んで採用する `adopt_llm_proposal` を使う）。
    運用外の話数・新規の話数では従来どおり使える。

    戻り値は台本確定の結果に加えて aroll（panel_count/warnings）・aroll_error・
    background（assigned等）・background_error を含む。**プロンプト生成/背景割当が
    失敗しても台本の承認自体は成功する**（各 *_error に理由が入るだけ）。
    ⚠️ 承認のたびにLLM呼び出しが挟まるぶん、以前よりわずかに遅くなる。
    """
    return await dc.request(
        "POST", f"projects/{project_id}/episodes/{episode_number}/approve-and-prepare",
        json={},
    )


async def regenerate_lines(project_id: str, episode_number: int, line_ids: list[str],
                           feedback: str, llm_model: Optional[str] = None) -> dict:
    """既存ドラフトの指定行だけをLLMで書き直す（他行・順序・話者・セクションは変更しない・可逆WRITE）。

    generate_script/regenerate（台本全体をゼロから作り直す）よりトークンコストが低い
    （台本全文をコンテキストに渡すが、出力は変更対象の行だけ）。line_idsは飛び飛びでもよい
    （台本全文を毎回渡すため文脈は保たれる）。line_idはget_scriptで確認する。
    """
    return await dc.request(
        "POST", f"api/scripting/projects/{project_id}/episodes/{episode_number}/regenerate-lines",
        json={"line_ids": line_ids, "feedback": feedback, "llm_model": llm_model},
    )


async def import_script(project_id: str, episode_number: int, script: dict,
                        title: Optional[str] = None, confirm: bool = False,
                        force: bool = False, style_name: Optional[str] = None,
                        estimated_duration_sec: Optional[float] = None,
                        llm_model: Optional[str] = None) -> dict:
    """コンテナ外（呼び出し元エージェント自身の執筆など）で作った完成台本を取り込む。

    script は {"lines": [...], "sections": [...]} 形式（generate_script が返す script と同じ構造）。
    lines[] の各要素には最低限 id / order / speaker_id / text / section が必要。
    id は行の一意識別子（例 "line_001"。"line_id" ではない＝Director-Agent UIの行プレビューは
    line.id をキーにレンダリングするため、id が無い/別名だと全行が最終行1件に潰れて表示される）。
    sections[].line_ids はこの id 値の配列。
    speaker_id は project_status や list_characters/assign_cast で確認した配役済みの役ID
    （例 "speaker_a"）を使うこと。キャラ名や自作IDは不可＝タイムライン生成時に未割当扱いになる。

    confirm=false（既定）はドラフト保存のみ（可逆）。この後 approve_script で確定する想定。
    confirm=true は即時確定し、既に確定済み台本がある話への上書きは force=true が必要。

    style_name / estimated_duration_sec / llm_model は台本タブ（行数/スタイル/推定時間/使用LLM）の
    表示用メタデータ。コンテナ側では検証しない自己申告値：
    - style_name: ユーザーが list_styles 由来の既存スタイルを指定してそれに従って書いた場合はその
      style_name を渡す。特定のスタイルを指定されず自由に構成した場合は省略可（未指定時はUIに
      「オリジナル」と表示される）。
    - estimated_duration_sec: 台本行の文字数とpause_after_secから見積もった秒数
      （irodoriエンジン実測較正値: 約263字/分。Docs/05_scripting.md参照）。
    - llm_model: 台本を実際に執筆したモデルの識別子（例 "claude-code/claude-sonnet-5"）。
      generate_script のllm_model（コンテナ内でAPIを叩くモデル名）とは別物＝呼び出し元エージェント
      自身の自己申告。
    """
    body = {"script": script, "confirm": confirm}
    if title is not None:
        body["title"] = title
    if style_name is not None:
        body["style_name"] = style_name
    if estimated_duration_sec is not None:
        body["estimated_duration_sec"] = estimated_duration_sec
    if llm_model is not None:
        body["llm_model"] = llm_model
    return await dc.request(
        "POST", f"api/scripting/projects/{project_id}/episodes/{episode_number}/import",
        json=body, params={"force": force},
    )


async def get_script(project_id: str, episode_number: int = 1, draft: bool = True) -> dict:
    """指定話のドラフトまたは確定済み台本を返す（import_script後の取り込み確認等に使う）。

    サブ行（1行に複数の絵を当てる・Docs/SUBLINE_PLAN.md）を持つ行は lines[].parent_line_id で
    判別できる（グループの先頭行は parent_line_id === 自分の id。無ければ普通の行）。
    """
    return await dc.get(
        f"api/scripting/projects/{project_id}/script",
        params={"draft": draft, "episode": episode_number},
    )


# ─── 台本の行の操作（director の行の操作の窓口 `lines/{op}`・Docs/LINE_WORKBENCH_PLAN.md §3・§8・W5） ─────
#
# 台本の行を変える操作は**すべて director の窓口 1 つを通る**（I1）。UI（ワークベンチ）も MCP も同じ窓口を呼ぶので、
# どちらから直しても音声・コマ（Aロール）・確定の状態が同じように追随する。呼び出し側が後処理を書く必要は無い。
#   - 操作の前に台本のスナップショットを履歴へ残す（`undo_line_op` で戻せる）
#   - 台本の変更は確定点。後処理（音声の孤立扱い・コマの追加・台本とズレ印）が失敗しても台本は戻らず、warnings に載る
#   - 後処理はすべて無料（画像は生成しない）。外した音声・コマは消さず保管する（Undo で戻る）
# 行は `line_id`（推奨・不変）か `order`（行番号・get_script で確認）で指す。`dry_run=True` は何も変えずに
# 「この操作で起きること」（台本・音声・絵・仕上がりの4レーン）だけを返す。
# ⚠️ 校正（誤字脱字・文法）は `import_script` で丸ごと書き戻さず、`update_script_line` で1行ずつ直す（D6）。
#    丸ごと書き戻しは行IDの採番カウンタや音声・コマとの対応を壊す経路になる。

def _line_target(line_id: Optional[str], order: Optional[int]) -> dict:
    if line_id is None and order is None:
        raise ValueError("line_id（推奨）か order（行番号）のどちらかを指定してください")
    return {"line_id": line_id} if line_id is not None else {"order": order}


async def _line_op(project_id: str, episode_number: int, op: str, body: dict, dry_run: bool = False) -> dict:
    return await dc.request(
        "POST", f"projects/{project_id}/episodes/{episode_number}/lines/{op}",
        params={"dry_run": True} if dry_run else None, json=body)


async def update_script_line(project_id: str, episode_number: int, line_id: Optional[str] = None,
                             order: Optional[int] = None, text: Optional[str] = None,
                             emotion: Optional[str] = None, speaker_id: Optional[str] = None,
                             speaker_name: Optional[str] = None, speed: Optional[float] = None,
                             pause_after_sec: Optional[float] = None, notes: Optional[str] = None,
                             dry_run: bool = False) -> dict:
    """台本の1行を直す（校正はこれで1行ずつ・窓口経由・可逆WRITE）。

    直せる項目: text（本文）/ emotion（声の感情＝TTSの演技。絵の表情ではない）/ speaker_id（話者。
    同じサブ行グループの全行に伝わる）/ speed / pause_after_sec / notes。指定した項目ごとに窓口の
    操作（edit → emotion → speaker）を順に1回ずつ実行する（Undo も操作ごと）。
    本文を直した行は音声が「要再生成」、絵が「古い」の印になる（確定の運用中の話数では「未確定」にもなる）。
    確定（confirm_lines）すると音声が自動で作り直される。
    戻り値: results[]（操作ごとの応答・impact／warnings／applied を含む）。dry_run=True は何も変えない。
    ⚠️ 複数項目を指定して途中の操作が失敗した場合、それ以前の操作は適用済みのまま。
    """
    target = _line_target(line_id, order)
    plan: list[tuple[str, dict]] = []
    edit = {k: v for k, v in (("text", text), ("speed", speed), ("pause_after_sec", pause_after_sec), ("notes", notes)) if v is not None}
    if edit:
        plan.append(("edit", {**target, **edit}))
    if emotion is not None:
        plan.append(("emotion", {**target, "emotion": emotion}))
    if speaker_id is not None:
        plan.append(("speaker", {**target, "speaker_id": speaker_id, **({"speaker_name": speaker_name} if speaker_name else {})}))
    if not plan:
        raise ValueError("変える項目（text / emotion / speaker_id / speed / pause_after_sec / notes）を1つ以上指定してください")
    results = [await _line_op(project_id, episode_number, op, body, dry_run) for op, body in plan]
    return {"results": results, "warnings": [w for r in results for w in r.get("warnings", [])]}


async def insert_script_line(project_id: str, episode_number: int, after_line_id: Optional[str] = None,
                             after_order: Optional[int] = None, text: str = "",
                             speaker_id: Optional[str] = None, emotion: Optional[str] = None,
                             dry_run: bool = False) -> dict:
    """指定行の直後に新しい行を挿入する（窓口経由・可逆WRITE）。

    after_line_id か after_order（0＝先頭に挿入）で位置を指す。speaker_id を省略した時の話者は台本側の既定。
    新しい行は音声が未生成・コマが要る状態で追加される（コマは窓口が空のコマを作る＝プロンプトと在庫の絵は確定時に用意）。
    """
    if after_line_id is None and after_order is None:
        raise ValueError("after_line_id か after_order（0＝先頭）を指定してください")
    body: dict = {"text": text}
    body.update({"after_line_id": after_line_id} if after_line_id is not None else {"after_order": after_order})
    for k, v in (("speaker_id", speaker_id), ("emotion", emotion)):
        if v is not None:
            body[k] = v
    return await _line_op(project_id, episode_number, "insert", body, dry_run)


async def move_script_line(project_id: str, episode_number: int, direction: str,
                           line_id: Optional[str] = None, order: Optional[int] = None,
                           dry_run: bool = False) -> dict:
    """行を1つ上（up）か下（down）へ動かす（窓口経由・可逆WRITE）。

    セクションをまたぐ・サブ行のグループをまたぐ移動はできない（400）。音声・コマはIDで結ばれているので追随する。
    """
    return await _line_op(project_id, episode_number, "move", {**_line_target(line_id, order), "direction": direction}, dry_run)


async def delete_script_line(project_id: str, episode_number: int, line_id: Optional[str] = None,
                             order: Optional[int] = None, dry_run: bool = False) -> dict:
    """行を台本から外す（窓口経由・可逆WRITE）。音声・コマ・在庫の使用回数は**消さずに保管**され、
    undo_line_op で台本に戻せば元どおり戻る。"""
    return await _line_op(project_id, episode_number, "delete", _line_target(line_id, order), dry_run)


async def confirm_lines(project_id: str, episode_number: int, line_ids: Optional[list[str]] = None) -> dict:
    """行を確定する（D8②・窓口経由）。line_ids 省略で「未確定のすべて」。

    確定すると、**音声が自動で作り直される（ローカルGPU・無料・非同期）**ほか、新しい行のコマの下ごしらえ
    （サブ行は親のコマの引き継ぎ・独立した行は感情のルール・背景の割当。**LLMは呼ばない**・画像は生成しない）が走り、
    再合成（組版）の対象に入る。英語の演出プロンプトは絵を新規生成する時に作られる（2026-10-01 以降）。
    確定の運用が始まっていない話数（`get_line_states` の enabled=false）は、先に `start_confirmations` か
    `adopt_llm_proposal` で運用を始める。戻り値の applied.tts.queued が音声を作り直し中の行。
    エンジンが止まっていると音声は「作り直し待ち」のまま残る（起動後に run_tts か画面の「未生成・要再生成を生成」）。
    """
    body = {"line_ids": line_ids} if line_ids is not None else {}
    return await dc.request("POST", f"projects/{project_id}/episodes/{episode_number}/lines/confirm", json=body)


async def start_confirmations(project_id: str, episode_number: int, baseline: bool = True) -> dict:
    """その話数の「確定の運用」を始める（D19）。baseline=True は今の台本の全行を確定済みとして記録し、
    以後は直した行だけが未確定になる。本番の既存話数は、ユーザーが始めるまで何も変わらない
    （運用外の話数は確定でゲートされず、従来どおり全行が組版などの対象）。"""
    return await dc.request("POST", f"projects/{project_id}/episodes/{episode_number}/lines/confirmations/start",
                            json={"baseline": baseline})


async def audit_episode(project_id: str, episode_number: int, full: bool = False) -> dict:
    """1話の整合検査（READ・何も書かない）。台本→確定→音声→絵→仕上がりの**どこまで最新か**と、
    **次にやること**（ツール名・引数・費用の種類つき）を順に返す。

    ユーザーが台本に手を入れた後、後続の処理を任された時は**必ず最初にこれ**を呼ぶ。
    layers.{script,confirm,tts,aroll,final}.state: ok（最新）/ behind（追随が要る）/ unknown（他コンテナに繋がらない）/
    na（その環境や話数に無い工程）。issues[] は code・severity（error=次へ進む前に直す・warn=直したほうがよい・
    info=知っておく）・line_ids。next_actions[] は上から順に実行する想定で、cost は free/gpu（無料・ローカル）/
    paid（課金）/photoshop（Photoshop占有）/user（ユーザー作業）、confirm=true は**実行前にユーザーへ一文で確認**。
    headline は「台本 150行 ／ 確定 150/150 ／ 音声 150/150 ／ 絵 148/150 ／ 仕上がり 112/150」の1行要約。
    既定は短い出力（issue の行IDは先頭8件＋more。実行に要る全IDは next_actions[].args にある）。全IDが要る時は full=True。
    他コンテナが重い時は数十秒かかることがある（待つ）。
    """
    return await dc.get(f"projects/{project_id}/episodes/{episode_number}/audit",
                        params={"full": "true"} if full else None, timeout=120)


async def get_line_states(project_id: str, episode_number: int) -> dict:
    """各行の確定状態と音声の作り直し待ち（READ）。enabled=false は確定の運用が始まっていない話数。"""
    return await dc.get(f"projects/{project_id}/episodes/{episode_number}/lines/state")


async def undo_line_op(project_id: str, episode_number: int, force: bool = False) -> dict:
    """窓口を通した直前の台本の変更を1つ戻す（可逆WRITE）。台本だけでなく、外した音声・コマも一緒に戻る
    （消していないため）。**行の確定・作り直した音声は戻らない**（台本の変更だけを戻す）。
    窓口を通さない変更（旧画面・import_script など）が間にあると 409（戻すとその変更も消えるため）。
    強制するなら force=True。戻せる件数は get_line_history の undoable。"""
    return await dc.request("POST", f"projects/{project_id}/episodes/{episode_number}/lines/undo",
                            params={"force": True} if force else None)


async def get_line_history(project_id: str, episode_number: int, limit: int = 20) -> dict:
    """窓口を通した操作の履歴（新しい順・READ）。undoable は戻せる件数。"""
    return await dc.get(f"projects/{project_id}/episodes/{episode_number}/lines/history", params={"limit": limit})


async def get_llm_proposal(project_id: str, episode_number: int) -> dict:
    """LLMの案（下書き）と正本の行ごとの差分（READ）。採用する前にこれで確認する。

    kind: none（差なし）/ initial（正本がまだ無い＝下書きが最初の台本）/ lines（行ごとの差分）/
    replace（全文の再生成＝行IDが振り直される）。replace は adopt_llm_proposal に replace_all=True が要る。
    """
    return await dc.get(f"projects/{project_id}/episodes/{episode_number}/lines/proposal")


async def adopt_llm_proposal(project_id: str, episode_number: int, line_ids: Optional[list[str]] = None,
                             replace_all: bool = False, dry_run: bool = False) -> dict:
    """LLMの案（generate_script・regenerate_lines・import_script(confirm=false) が書いた下書き）を正本へ採用する
    （D8①・窓口経由・Undo可）。採用した行は「未確定」になり、続けて confirm_lines で確定する。

    line_ids で採用する行を選べる（省略で差のある行すべて）。全文の再生成（replace）は行IDが振り直されて
    音声・絵との紐付けが全て切れるので replace_all=True の明示が要る（先に dry_run で確認）。
    最初の採用（正本がまだ無い）は元に戻せない。この話数の確定の運用は採用で始まる。
    従来の `approve_script`（承認＋Aロールのプロンプト・背景の下ごしらえ）とは別物で、下ごしらえは確定の時に走る。
    """
    body: dict = {"replace_all": replace_all}
    if line_ids is not None:
        body["line_ids"] = line_ids
    return await _line_op(project_id, episode_number, "adopt", body, dry_run)


async def split_line(project_id: str, episode_number: int, position: int, order: Optional[int] = None,
                     line_id: Optional[str] = None, dry_run: bool = False) -> dict:
    """行を文字位置で前後に分ける（§5「分ける」・窓口経由・可逆WRITE）。

    position は行の text をこの文字位置で前後に分ける（0 < position < 文字数）。前半は元の行のIDのまま、
    後半は新しいサブ行として直後に入る（戻り値の new_line_ids）。**窓口経由なので、音声とコマも追随する**
    （前半は要再生成・後半は未生成／後半のコマができ、前半の絵は「古い」にならない）。
    position は get_script で確認した本文の文字数を基準に数える。句読点（。！？、）の位置に合わせると自然。
    行は line_id か order（行番号）で指す。
    """
    return await _line_op(project_id, episode_number, "split", {**_line_target(line_id, order), "position": position}, dry_run)


async def merge_line_with_next(project_id: str, episode_number: int, order: Optional[int] = None,
                               line_id: Optional[str] = None, dry_run: bool = False) -> dict:
    """行と次の行のセリフ本文をつなげる（§5「結合」・窓口経由・可逆WRITE）。

    話者・セクション・グループが一致しない隣接行は結合できない。残るのは前の行のID、次の行は台本から外れる
    （**その行の音声・コマは消さず保管**＝undo_line_op で戻せる。以前は呼び出し側で音声削除が要ったが不要）。
    """
    return await _line_op(project_id, episode_number, "merge", _line_target(line_id, order), dry_run)


async def add_subline(project_id: str, episode_number: int, order: Optional[int] = None, text: str = "",
                      emotion: Optional[str] = None, line_id: Optional[str] = None, dry_run: bool = False) -> dict:
    """同じグループの新しいサブ行を、指定行の直後に追加する（§5「追加」・窓口経由・可逆WRITE）。

    話者・セクションは指定行から引き継ぐ（サブ行だけ話者を変えることはできない）。emotion 省略時も引き継ぐ。
    """
    body = {**_line_target(line_id, order), "text": text}
    if emotion is not None:
        body["emotion"] = emotion
    return await _line_op(project_id, episode_number, "add-subline", body, dry_run)


async def get_split_proposal(project_id: str, episode_number: int, order: int,
                             limit: Optional[int] = None) -> dict:
    """自動区切りの候補を計算する（§9-1・純粋な検査でdraftは変更しない）。

    上限字数（既定55字≈12秒。§0-2の実測式）を超える行に対し、句点優先・最少分割で
    区切り位置を提案する。戻り値 pieces[] は各片の {start,end,text,split_review}
    （split_review=true は読点で切った箇所＝人の目で確認した方がよい）。候補が無い
    （既に上限内、または読点も句点も無い1文）場合は pieces が空リストになる＝
    その場合は split_line で手動の位置を指定すること。
    """
    params: dict = {"episode": episode_number}
    if limit is not None:
        params["limit"] = limit
    return await dc.get(
        f"api/scripting/projects/{project_id}/script/line/{order}/split-proposal",
        params=params,
    )


async def apply_split_proposal(project_id: str, episode_number: int, order: Optional[int] = None,
                               limit: Optional[int] = None, line_id: Optional[str] = None,
                               dry_run: bool = False) -> dict:
    """自動区切りの候補をその行1つに適用する（§9-3「行ごと」・窓口経由・可逆WRITE）。

    最初の片は元の行のIDのまま、残りは新しいサブ行として連続追加される。候補が2片未満（上限内、または
    区切れない）の場合は400エラー。音声・コマは窓口が追随させる（split_line と同じ）。
    """
    body = _line_target(line_id, order)
    if limit is not None:
        body["limit"] = limit
    return await _line_op(project_id, episode_number, "split-apply", body, dry_run)


async def apply_split_proposal_all(project_id: str, episode_number: int, limit: Optional[int] = None,
                                   dry_run: bool = False) -> dict:
    """話数全体で、上限字数を超える行すべてに自動区切りを適用する（§9-3「話数全体」・窓口経由・可逆WRITE）。

    対象は呼び出し時点で上限を超えている行。**全体で1回の操作として履歴に積まれる**（undo_line_op 1回で戻る）。
    先に dry_run=True で影響（何行が区切られるか）を確認するとよい。
    """
    body = {"limit": limit} if limit is not None else {}
    return await _line_op(project_id, episode_number, "split-apply-all", body, dry_run)


async def generate_series_script(project_id: str, style_id: str,
                                  episode_count: Optional[int] = None,
                                  extra_instruction: Optional[str] = None,
                                  rough_script: Optional[str] = None,
                                  llm_model: Optional[str] = None) -> dict:
    """シリーズ台本を一括生成し、各話を episodes/epNN/ にドラフト保存する（各話とも確定はapprove_script別途）。

    episode_count省略時はLLMが適切な話数を判断する。各話のepisode_numberはこの結果の
    episode_number群から取得し、以降の工程(approve_script等)に渡す。
    llm_model省略時は既定(直接Anthropic API経由のSonnet 4.6)を使用。OpenRouter経由(openrouter/...)を
    指定する場合は無料モデル限定（有料は拒否される）。有料で使うなら anthropic/... , openai/... ,
    gemini/... のオリジナルAPIを直接指定すること。

    ⚠️ **承認済み台本がある話をこれで作り直すと、次の approve_script で line_id が
    振り直され、Aロールの絵とTTSの音声の紐付けが全滅する。** 事前に project_status で
    各話の生成物の有無を確認すること（generate_script と同じ注意）。
    """
    body = {"style_id": style_id}
    if episode_count is not None:
        body["episode_count"] = episode_count
    if extra_instruction is not None:
        body["extra_instruction"] = extra_instruction
    if rough_script is not None:
        body["rough_script"] = rough_script
    if llm_model is not None:
        body["llm_model"] = llm_model
    return await dc.request("POST", f"api/scripting/projects/{project_id}/generate-series", json=body)


# ── 素材収集（scrapping） ────────────────────────────────────────

async def generate_queries(project_id: str, episode_number: int,
                           extra_prompt: Optional[str] = None,
                           model: Optional[str] = None) -> dict:
    """確定script.jsonからセクション別の検索クエリをLLMで生成し footage_draft.json に保存する。

    前提: 該当話が approve_script 済み（script.json 必須）。可逆WRITE＋LLM。
    model省略時は既定(直接Anthropic API経由のSonnet 4.6)を使用。OpenRouter経由(openrouter/...)を
    指定する場合は無料モデル限定（有料は拒否される）。有料で使うなら anthropic/... , openai/... ,
    gemini/... のオリジナルAPIを直接指定すること。
    """
    body: dict = {}
    if extra_prompt is not None:
        body["extra_prompt"] = extra_prompt
    if model is not None:
        body["model"] = model
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/queries", json=body)


async def search_footage(project_id: str, episode_number: int,
                         media: str = "video", per_query: int = 4,
                         sources: Optional[list[str]] = None) -> dict:
    """footage_draftのクエリで素材を検索し候補を集める。要 generate_queries 先行。

    sources: pexels/pixabay/vecteezy（既定 ["pexels"]）。検索自体は無料だが外部API到達＝COST分類。
    media: video|photo|both。確定DLは select_footage（Vecteezyはここでクォータ消費）。
    """
    body = {"media": media, "per_query": per_query, "sources": sources or ["pexels"]}
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/search", json=body)


async def auto_select_footage(project_id: str, episode_number: int,
                              model: Optional[str] = None) -> dict:
    """LLMが候補から尺・意味に合う素材を自動選択する（確定は select_footage で人間承認）。要 search 先行。

    model省略時は既定(直接Anthropic API経由のSonnet 4.6)を使用。OpenRouter経由(openrouter/...)を
    指定する場合は無料モデル限定（有料は拒否される）。
    """
    body: dict = {}
    if model is not None:
        body["model"] = model
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/auto_select", json=body)


async def select_footage(project_id: str, episode_number: int,
                         selections: list[dict]) -> dict:
    """採用候補をダウンロードして footage.json を確定する（不可逆＝外部DL／Vecteezyはクォータ消費）。

    selections: [{"section": "intro", "candidate_ids": ["..."]}, ...]。
    COST: 確定DLが走る。auto_select_footage の結果を踏まえて選ぶのが通常。
    """
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/select",
        json={"selections": selections})


# ── キャラ台帳（characters / 登録＝台本参加の入口・可逆WRITE） ──────────
#
# 「台本に出られるのは登録済みキャラだけ」という階層は据え置く（自然な設計）。
# ここで開けるのは“登録という入口”だけ＝頭脳がキャラを起こし・配役できるようにする。
# 台帳の本籍は shared/characters/{char_id}/（footage系とは別系統）。
# director プロキシ経由: GET/POST api/scrapping/characters, PATCH api/scrapping/characters/{id}。

async def list_characters() -> dict:
    """登録済みキャラの一覧を返す（id / name / 外見 / 声バインド等）。配役・声割当の前に現状把握。"""
    return await dc.get("api/scrapping/characters")


async def get_character(char_id: str) -> dict:
    """キャラ1件の詳細を返す（appearance_prompt や voice={engine,voice_id} の確認に使う）。"""
    return await dc.get(f"api/scrapping/characters/{char_id}")


async def create_character(char_id: str, name: str, appearance_prompt: str,
                           description: str = "", caption: str = "",
                           voice: Optional[dict] = None) -> dict:
    """新規キャラを台帳に登録する（shared/characters/{char_id}/ 生成・可逆WRITE）。

    char_id は [a-z0-9][a-z0-9_-]* に一致する英数スラッグ（既存と衝突すると409）。先に list_characters で確認。
    appearance_prompt = 外見の固定プロンプト（一貫性の核・紙芝居/画像生成の前提）。
    caption は字幕表示名（空なら name）。voice={engine,voice_id} は既存の声カタログ（list_voices）から
    選んで割り当てる。外部APIで“新しい声を作る”工程はこのツールには含まれない（別フェーズ）。
    """
    body: dict = {"char_id": char_id, "name": name,
                  "appearance_prompt": appearance_prompt,
                  "description": description, "caption": caption}
    if voice is not None:
        body["voice"] = voice
    return await dc.request("POST", "api/scrapping/characters", json=body)


async def update_character(char_id: str, name: Optional[str] = None,
                           description: Optional[str] = None,
                           appearance_prompt: Optional[str] = None,
                           caption: Optional[str] = None,
                           voice: Optional[dict] = None) -> dict:
    """既存キャラを部分更新する（指定フィールドのみ上書き・可逆WRITE）。

    voice={engine,voice_id} を渡すと声バインドを丸ごと置換する（= 既存カタログの声を当て直す）。
    既存の声カタログは list_voices で確認（irodori エンジンは engine="irodori"）。
    """
    body: dict = {}
    for k, v in (("name", name), ("description", description),
                 ("appearance_prompt", appearance_prompt), ("caption", caption)):
        if v is not None:
            body[k] = v
    if voice is not None:
        body["voice"] = voice
    return await dc.request("PATCH", f"api/scrapping/characters/{char_id}", json=body)


async def list_voices() -> dict:
    """選択可能な声カタログ一覧を返す（voice_id 群）。update_character / 配役の voice 指定に使う。

    irodori エンジンのローカル参照音声が本体。返る id がそのまま voice_id（engine="irodori"）。
    """
    return await dc.get("api/tts/voices")


async def _resolve_full_project_id(project_id: str) -> tuple[str, dict]:
    """project_id（前方一致可）を tts の実プロジェクトに解決し、(実id, project_dict) を返す。"""
    data = await dc.get("api/tts/projects")
    projects = data.get("projects", []) if isinstance(data, dict) else data
    exact = [p for p in projects if p.get("id") == project_id]
    pref = exact or [p for p in projects if str(p.get("id", "")).startswith(project_id)]
    if not pref:
        raise dc.DirectorError(f"project not found: {project_id}")
    if len(pref) > 1:
        ids = ", ".join(p.get("id", "") for p in pref)
        raise dc.DirectorError(f"project_id ambiguous: {project_id} -> {ids}")
    return pref[0]["id"], pref[0]


async def assign_cast(project_id: str, assignments: dict) -> dict:
    """役（speaker）にキャラを割り当てる＝配役（config.tts.speakers[].character_id を更新・可逆WRITE）。

    assignments = {speaker_id: character_id, ...}。speaker_id は台本の speaker_id（list_projects の
    speakers[].id / 台本行の speaker_id）。character_id は list_characters の char_id。空文字で割当解除。
    保存先は project.json の config.tts.speakers（役→キャラ割当の唯一の本籍）。声・字幕は割当先キャラから
    解決されるためここでは character_id のみ触る。run_tts の前提（未割当の役はスキップ/409 ガード）を満たす。
    """
    full_id, project = await _resolve_full_project_id(project_id)
    speakers = project.get("speakers", []) or []
    known = {sp.get("id") for sp in speakers}
    unknown = [sid for sid in assignments if sid not in known]
    if unknown:
        raise dc.DirectorError(
            f"unknown speaker_id(s): {', '.join(unknown)} / known: {', '.join(sorted(map(str, known)))}")
    for sp in speakers:
        if sp.get("id") in assignments:
            sp["character_id"] = assignments[sp["id"]]
    return await dc.request(
        "POST", f"api/tts/projects/{full_id}/speakers", json={"speakers": speakers})


# ── 音声合成（tts / 課金＋非同期） ───────────────────────────────

async def run_tts(project_id: str, episode_number: int) -> dict:
    """指定話の全行を音声合成する（バックグラウンド・ローカルGPU推論=irodori-tts-server、外部課金なし）。

    前提: 役にキャラ/声が割当済み（config.tts.speakers[].character_id が本籍）。未割当の役は
    tts-agent 側でスキップ/409 ガードされる。進捗は project_status / tts.json で追う。
    """
    return await dc.request(
        "POST", f"projects/{project_id}/episodes/{episode_number}/tts/run")


# ── ラフ編集（editing / 可逆WRITE） ──────────────────────────────

async def build_timeline(project_id: str, episode_number: int, fps: int = 30,
                         subtitle_format: str = "both", force: bool = False,
                         path_style: str = "file_uri", speaker_prefix: bool = False) -> dict:
    """OTIO/SRT/FCPXML のラフ編集データを生成する。前提: tts/footage が done（force で上書き可）。

    subtitle_format: srt|fcpxml|both。path_style: file_uri|windows。
    """
    body = {"fps": fps, "subtitle_format": subtitle_format, "force": force,
            "path_style": path_style, "speaker_prefix": speaker_prefix}
    return await dc.request(
        "POST", f"api/editing/projects/{project_id}/episodes/{episode_number}/edit/run", json=body)


# ── 自由生成（imagegen / 台本非依存・[[free-studio-tab]]） ──────────────

async def list_imagegen_styles() -> dict:
    """自由生成(free_generate)で使えるスタイル一覧を返す（style名→定義の辞書）。style の候補。"""
    data = await dc.get("api/scrapping/imagegen/styles")
    return data


async def free_generate(prompt: str, provider: str = "nanobanana",
                        style: str = "realistic", count: int = 2,
                        aspect: str = "16:9", model: str = "") -> dict:
    """台本非依存でテキストから画像を count 枚生成し staging 候補にする（t2i のみ）。

    provider: nanobanana(外部API課金) | comfy(ローカルSD/GPU・無料)。参照画像i2iは非対応（テキストのみ）。
    model: nanobananaのみ有効。空なら既定(NANOBANANA_MODEL)。廉価版で大量生成したい時は
    "gemini-3.1-flash-lite-image" 等を明示指定する（詳細 Docs/IMAGE_GEN_COST.md）。
    確定保存は free_save。COST/GPU 分類＝確認ゲート対象。
    """
    form = {"provider": provider, "mode": "t2i", "prompt": prompt,
            "style": style, "count": count, "aspect": aspect, "model": model}
    return await dc.request("POST", "api/scrapping/imagegen/free/generate", data=form)


async def free_audio(prompt: str) -> dict:
    """Lyriaで BGM/効果音を1本生成し staging 候補(MP3)にする（外部API課金）。確定は free_save。"""
    return await dc.request("POST", "api/scrapping/imagegen/free/audio/generate",
                            json={"prompt": prompt})


async def free_tts(text: str, voice: str, caption: str = "", emotion: str = "neutral",
                   speed: float = 1.0, lang: str = "") -> dict:
    """台本に紐づかない話者音声を1本生成し staging 候補(WAV)にする（ローカルGPU・外部課金なし）。

    voice は list_voices の id を使う（新しい声の概念は作らない）。lang省略/"ja"=irodori、
    それ以外=omnivoice（多言語・voiceに .ref.wav/.ref.txt が要る）。生成物はエキストラ扱いで
    tts.json/OTIOには載らない（DaVinciでの手動追加が前提）。確定保存は free_save を流用する。
    """
    return await dc.request("POST", "api/tts/free/generate", json={
        "text": text, "voice": voice, "caption": caption,
        "emotion": emotion, "speed": speed, "lang": lang or None,
    })


async def free_save(name: str, save_name: str = "") -> dict:
    """staging の候補(name・画像/音声/動画)を direct_output/ に確定保存する。save_name は任意の確定名。"""
    return await dc.request("POST", "api/scrapping/imagegen/free/save",
                            json={"name": name, "save_name": save_name})


async def free_video(prompt: str, provider: str = "grok", image_path: str = "",
                     image_name: str = "", last_frame_path: str = "", last_frame_name: str = "",
                     duration: int = 0, resolution: str = "720p", aspect: str = "16:9",
                     audio: bool = False, model: str = "",
                     confirm_cost: bool = False) -> dict:
    """動画を1本生成する（外部API課金）。静止画を渡せば i2v、無ければ t2v。

    **2段階で使う**: まず confirm_cost=False で呼ぶと課金せず費用の目安(estimate.usd)だけ返る。
    ユーザーに金額を伝えて了承を得てから、同じ引数＋confirm_cost=True で再度呼ぶと投入される。
    投入後は job_id が返るので free_video_status(job_id) で完成を待つ（実測 約35秒）。
    完成品は staging 候補(mp4)になり、確定保存は free_save（生成条件のJSONも一緒に運ばれる）。

    provider:
      - "grok"（Grok Imagine Video）: 1-15秒・480p/720p/1080p・縦横比7種・audio選択可。
        カメラの動きの指示がよく効く。実費 1.5・720p で約$0.142/秒（5秒≈$0.71・表示価格の約1.8倍）。
        model 空=grok-imagine-video-1.5 / 安価版 "grok-imagine-video"（720pまで）。
      - "veo"（Google Veo 3.1・GEMINI_API_KEY）: 4/6/8秒・720p/1080p(8秒必須)・16:9/9:16のみ・
        **常に音声付き**。表示価格 lite $0.05/秒・fast $0.10/秒・標準 $0.40/秒（720p）。安いが
        カメラ指示が効きにくくポーズが動きやすい（実測）。応答に費用が無い＝実費はGoogleの請求画面で確認。
        model 空=veo-3.1-lite-generate-preview / "veo-3.1-fast-generate-preview" / "veo-3.1-generate-preview"。
        **last_frame（最後の絵）を渡すと 最初の絵→最後の絵 をつなぐ補間動画**になる（カット間のつなぎ向け）。
    image_path / last_frame_path: ホスト上の画像パス（MCPサーバーが読んで送る）。
    image_name / last_frame_name: direct_output/ か _staging/ にある既存画像名。各々どちらか一方。
    duration: 0=プロバイダの既定（grok 5秒 / veo 4秒）。
    1本の上限は VIDEO_MAX_COST_USD（既定$3）で、超える指定は confirm しても拒否される。COST分類。
    """
    body = {"prompt": prompt, "provider": provider, "model": model, "duration": duration or None,
            "resolution": resolution, "aspect": aspect, "audio": audio,
            "image_name": image_name, "last_frame_name": last_frame_name,
            "confirm_cost": confirm_cost}
    for key, path in (("image_b64", image_path), ("last_frame_b64", last_frame_path)):
        if path:
            p = Path(path)
            if not p.is_file():
                return {"error": f"file not found: {path}"}
            body[key] = base64.b64encode(p.read_bytes()).decode()
    return await dc.request("POST", "api/scrapping/imagegen/free/video/generate", json=body)


async def free_video_status(job_id: str, wait_seconds: int = 180) -> dict:
    """free_video のジョブ状態を返す。wait_seconds の間 5秒おきに確認し、完了/失敗で即返す。

    status: pending/done/failed/expired/error。done なら candidate(staging の mp4 名・URL)と
    cost_usd(実費)が入る。wait_seconds=0 なら1回だけ確認する。
    """
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        # 完成時はこの呼び出し内で mp4(数MB)を取り込むので、読み取り用の短いタイムアウトは使わない
        job = await dc.request("GET", f"api/scrapping/imagegen/free/video/jobs/{job_id}")
        if not isinstance(job, dict) or job.get("status") != "pending" or time.monotonic() >= deadline:
            return job
        await asyncio.sleep(5)


# ── 紙芝居パネル（character panel / 台本→キャラ画像の橋渡し） ──────────
#
# POST /characters/{id}/panel の script_ref は記録専用（実コード注釈）で、
# 「台本のどの場面にどの表情/ポーズを当てるか」の判断はUI操作（人間）に委ねられている。
# ここでは頭脳(LLM)がその判断を担う＝R6(紙芝居プリセット選択)の実体化。

async def list_panel_presets() -> dict:
    """紙芝居パネル生成の構造化入力プリセット(表情/ポーズ/ショット/アングル/シーン)を返す。

    emotion_id/pose_id/shot_id/angle_id/scene_id は必ずここのid集合から選ぶこと(自由文不可)。
    """
    return await dc.get("api/scrapping/panel/presets")


async def generate_character_panel(char_id: str, emotion_id: str = "", pose_id: str = "",
                                    shot_id: str = "", angle_id: str = "", scene_id: str = "",
                                    background_mode: str = "flat", extra_prompt: str = "",
                                    count: int = 1,
                                    script_ref: Optional[dict] = None) -> dict:
    """キャラの紙芝居パネル画像を生成する(NanoBanana・外部API課金)。

    emotion_id等は list_panel_presets の id から選ぶ。script_ref={project_id,episode,line_id}は
    記録専用(生成には使わない・後で「どの行のための画像か」を追跡するため)。
    前提: 対象キャラに appearance_prompt が設定済み(キャラ詳細 GET /characters/{id} で確認)。
    """
    body = {"emotion_id": emotion_id, "pose_id": pose_id, "shot_id": shot_id,
            "angle_id": angle_id, "scene_id": scene_id, "background_mode": background_mode,
            "extra_prompt": extra_prompt, "count": count}
    if script_ref is not None:
        body["script_ref"] = script_ref
    return await dc.request("POST", f"api/scrapping/characters/{char_id}/panel", json=body)


# ── 背景アーカイブ（台本に縛られない再利用可能な背景ライブラリ） ─────────────
#
# Aロール（キャラ単体パネル）の背後に貼る前提の絵。Photoshop側で拡大・ぼかしを掛ける
# ため生成側は加工しやすさを優先する（Docs/BACKGROUND_ARCHIVE.md §1「原則」）。
# 語彙・設計判断の正本は同Doc。id は必ず list_background_presets で確認してから使う。

async def list_background_presets() -> dict:
    """背景生成の構造化入力プリセット(spot/motif/era/light/camera/framing/mood/form/effect)を返す。

    spot_id等は必ずここのid集合から選ぶこと(自由文不可)。framing の id は list_panel_presets の
    shot と同一語彙(Aロールのslot.shotからそのまま引ける)。
    """
    return await dc.get("api/scrapping/backgrounds/presets")


async def list_backgrounds(category: str = "", spot: str = "", motif: str = "",
                            era: str = "", light: str = "", framing: str = "",
                            mood: str = "", q: str = "") -> dict:
    """背景アーカイブの索引を検索する(AND条件・各引数は空なら無視)。

    台本行の演技(slot.emotion等)に合う背景を選ぶ時、まずここで既存在庫を確認する
    (無ければ generate_background で新規作成)。
    """
    params = {"category": category, "spot": spot, "motif": motif, "era": era,
              "light": light, "framing": framing, "mood": mood, "q": q}
    return await dc.get("api/scrapping/backgrounds", params=params)


async def generate_background(category: str, spot: str = "", motif: str = "", era: str = "",
                               light: str = "", camera: str = "", framing: str = "",
                               form: str = "", effect: str = "",
                               mood: Optional[list[str]] = None, model: str = "",
                               count: int = 1, is_keyframe: bool = False,
                               note: str = "") -> dict:
    """構造化入力から背景を生成し、shared/backgrounds/へ保存・索引登録する(NanoBanana・外部API課金)。

    category="location": spot(部屋の場所)・motif(調度品)・era(現代の携行品)のいずれか1つ+light+framing。
    category="psych"/"comic": form/effectのいずれか1つ。
    model省略時は既定(NANOBANANA_MODEL)。廉価版で量産したい時は "gemini-3.1-flash-lite-image" を
    明示指定する(背景は加工前提のためLiteで十分・詳細 Docs/IMAGE_GEN_COST.md)。
    count>1はキーフレーム作成・新規パターン初回試行の時だけ推奨(通常の量産はcount=1)。
    COST分類＝確認ゲート対象。
    """
    body = {"category": category, "spot": spot, "motif": motif, "era": era,
            "light": light, "camera": camera, "framing": framing,
            "form": form, "effect": effect, "mood": mood or [], "model": model,
            "count": count, "is_keyframe": is_keyframe, "note": note}
    return await dc.request("POST", "api/scrapping/backgrounds/generate", json=body)


async def delete_background(bg_id: str) -> dict:
    """背景アーカイブから1件削除する(索引・実体ファイルとも)。取り消し不可。"""
    return await dc.request("DELETE", f"api/scrapping/backgrounds/{bg_id}")


# ── キャラ所有ライブラリ（Phase 3・Aロール演技スロットの作り置き） ───────────
#
# キャラ別に演技パターン(emotion/shot/angle)を作り置きし、台本行の画像生成時に無料で
# 引けるようにする(Aロールのuse_library既定true・aroll_manager._library_lookup)。
# 語彙は list_panel_presets と同一(emotion/shot/angle)。正本 Docs/AROLL_ASSET_PLAN.md。

async def list_panel_library(char_id: str, emotion: str = "", shot: str = "", angle: str = "") -> dict:
    """キャラのライブラリ索引を検索する(AND条件)。各entryに is_stale(外見更新後の世代違いか)が付く。

    2026-09-25〜: 世代は凍結済み(Docs/CHARACTER_CONSISTENCY_PLAN.md)。プロンプトや参照画像を
    直しても appearance_version は変わらないため、is_stale=true は通常出ない
    (出る場合は本物の世代違い=デザインを変えたキャラで、対処は rebless)。
    """
    params = {"emotion": emotion, "shot": shot, "angle": angle}
    return await dc.get(f"api/scrapping/characters/{char_id}/panel_library", params=params)


async def stock_health(char_id: str = "", project_id: str = "", episode_number: int = 0) -> dict:
    """在庫の健全性監査(READ・無料・何も変えない)。**話数を回す前に**、感情ごとに足りない在庫(薄いプール)を知る。

    - project_id+episode_number: その話の要求(aroll.json の行数)と比べる。thin[].message が
      「アオイ thoughtful（物思い） 13行／在庫10枚」の形＝この話では重複なしに賄えず、再使用か新規生成(課金)に落ちる。
    - char_id だけ: そのキャラ。要求は過去の全話の感情の割合×1話の行数の中央値(見込み)。
    - 何も渡さない: 在庫を持つ全キャラ(見込み)。thin_messages に薄い感情の一覧。
    families[] は感情(系統)ごとに primary(主タグの系統がその感情の絵)/by_tag(細かいタグの内訳)/verified/
    usable(上限・承認・世代・banned を除いて自動選択に出る枚数)/capped(使用上限到達)/sub_usable(副タグでだけ受けられる・
    薄いの判定には入れない)/pose_rate/demand/peak(過去の1話の最多)/thin/thin_at_peak。
    totals は no_emotion(感情なし＝死蔵)/face_hidden/pending/stale/unverified 等、orphan_overrides は消えた絵を指す設定。
    足りない時の補充(在庫の生成)は課金なのでユーザーに確認する(補充の計画は別)。
    """
    if project_id:
        return await dc.get(f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/stock-health")
    if char_id:
        return await dc.get(f"api/scrapping/characters/{char_id}/panel_library/health")
    return await dc.get("api/scrapping/panel-library/health")


async def generate_panel_library_entry(char_id: str, emotion: str, shot: str, angle: str,
                                        pose: str = "", facing: str = "",
                                        style: str = "kamishibai",
                                        model: str = "", replace_stale: bool = False) -> dict:
    """キャラのライブラリに1スロット生成・登録する(NanoBanana・外部API課金)。

    emotion/shot/angle/facing は list_panel_presets の id から選ぶ。facing省略時は"front"
    (2026-09-23新設。キャラの向き専用軸。詳細 Docs/FACING_AXIS_PLAN.md ── pose には
    向きを混ぜない。旧 pose="facing_left"/"profile_left" 等はこのバージョンでは語彙に無い)。
    model省略時は既定(NANOBANANA_MODEL)。廉価版で量産したい時は
    "gemini-3.1-flash-lite-image" を明示指定する。
    replace_stale=false(★2026-09-25既定変更): 世代を凍結したため、同スロットの旧世代entryを
    実体ごと削除するこの経路は通常不要(使用中の在庫まで消しうる不具合もあった)。
    trueは世代混在を意図的に掃除したい特殊な時だけ使う。
    COST分類＝確認ゲート対象。
    """
    body = {"emotion": emotion, "shot": shot, "angle": angle, "pose": pose, "facing": facing,
            "style": style, "model": model, "replace_stale": replace_stale}
    return await dc.request("POST", f"api/scrapping/characters/{char_id}/panel_library/generate", json=body)


async def delete_panel_library_entry(char_id: str, slot_id: str) -> dict:
    """キャラのライブラリから1件削除する(索引・実体ファイルとも)。取り消し不可。pending中の却下にも使う。"""
    return await dc.request("DELETE", f"api/scrapping/characters/{char_id}/panel_library/{slot_id}")


async def generate_panel_library_variants(char_id: str, emotion: str, shot: str, angle: str,
                                           poses: list[str], facings: list[str] | None = None,
                                           style: str = "kamishibai",
                                           model: str = "") -> dict:
    """同じ(emotion,shot,angle)で pose × facing を変えた複数バリアントを一括生成する(NanoBanana・外部API課金)。

    matching key(emotion/shot/angle)は変えない(組み合わせ爆発回避)。poseは既存語彙
    (list_panel_presetsのpose)から選ぶ。facings省略時は["front"]
    (2026-09-23新設。list_panel_presetsのfacing。詳細 Docs/FACING_AXIS_PLAN.md)。
    ⚠️ **生成件数は poses × facings の直積**（例: pose3つ×facing2つ＝6枚課金）。
    生成物は全てreview_status="pending"で登録され、
    approve_panel_library_entryで承認するまでAロール生成からは引かれない(色ブレ等の個体差が
    無審査で本番に流れるのを防ぐ設計。実測で瞳の色が違う個体が出た実例あり)。COST分類。
    """
    body = {"emotion": emotion, "shot": shot, "angle": angle, "poses": poses,
            "facings": facings or [], "style": style, "model": model}
    return await dc.request("POST", f"api/scrapping/characters/{char_id}/panel_library/generate_variants", json=body)


async def approve_panel_library_entry(char_id: str, slot_id: str) -> dict:
    """pending状態のライブラリentryを承認する(以後Aロール生成から引かれるようになる)。"""
    return await dc.request("POST", f"api/scrapping/characters/{char_id}/panel_library/{slot_id}/approve")


# ── リサーチ（research / 別件1: 探索→蒸留→ラフ台本） ─────────────────
#
# research-agent(:8001) は当初 MCP から外す方針だったが、頭脳とMCPが分離している以上、
# 頭脳に「リサーチ→重要トピック把握→再リサーチ→ラフ台本」と司令できる＝MCP化が筋。
# grounded_search(Geminiグラウンディング)が既に実装済み（探索脳の基礎）。出力 rough_script.txt は
# scripting が無改修で取り込む（[[research-agent-revived-digest]]）。director /api/research プロキシ経由。

async def research_list_sources(project_id: str) -> dict:
    """リサーチプロジェクトの収集済みソース一覧（本文除くプレビュー）を返す。"""
    return await dc.get(f"api/research/projects/{project_id}/sources")


async def research_search(project_id: str, query: str, max_results: int = 6) -> dict:
    """Web をグラウンディング検索し、出典群をソースとして保存する（外部API＝探索脳）。

    要 Gemini APIキー。COST 分類＝確認ゲート対象。重要トピック把握→再検索の反復に使う。
    """
    return await dc.request("POST", f"api/research/projects/{project_id}/sources/search",
                            json={"query": query, "max_results": max_results})


async def research_add_source(project_id: str, title: Optional[str] = None,
                              text: Optional[str] = None, url: Optional[str] = None) -> dict:
    """テキスト貼付 or URL取得でソースを1件追加する（text か url のいずれか必須）。"""
    body: dict = {}
    if title is not None:
        body["title"] = title
    if text is not None:
        body["text"] = text
    if url is not None:
        body["url"] = url
    return await dc.request("POST", f"api/research/projects/{project_id}/sources/text", json=body)


async def research_digest(project_id: str, target_duration_sec: int = 300,
                          extra_instruction: Optional[str] = None,
                          model: Optional[str] = None) -> dict:
    """収集ソースを蒸留してラフ台本(rough_script.txt)を作る（執筆脳・LLM）。scripting が取り込む。"""
    body = {"target_duration_sec": target_duration_sec}
    if extra_instruction is not None:
        body["extra_instruction"] = extra_instruction
    if model is not None:
        body["model"] = model
    return await dc.request("POST", f"api/research/projects/{project_id}/digest", json=body)


async def research_get_digest(project_id: str) -> dict:
    """蒸留結果（research メタ＋ rough_script）を読み取る。"""
    return await dc.get(f"api/research/projects/{project_id}/digest")


# ── YouTube SEO オプティマイザ（市場データ収穫→公開メタデータ生成） ─────
#
# ラフ台本からジャンル/シードキーワードを推定し、YouTube Data API で市場データ（タグ・競合・コメント）
# を収穫して seo_pack.json を生成。生成後は generate_script / generate_series_script の
# extra_instruction に自動注入されて台本品質向上（視聴者ニーズ反映）に繋がる。
# 最終出力 publish_pack.json は確定台本＋SEOパックから YouTube 公開用メタデータ（タイトル案3/概要欄/
# ハッシュタグ/タグ）を生成する。

async def seo_optimize(project_id: str, force: bool = False,
                       rough_script: Optional[str] = None) -> dict:
    """ラフ台本からLLMでジャンル/シードキーワードを推定し、YouTube Data APIで市場データを収穫してseo_pack.jsonを生成。

    台本生成プロンプトに自動注入される。rough_script を渡すと保存してから分析。
    force=True でも既に seo_pack.json が存在すれば確認なく上書き。
    """
    body: dict = {"force": force}
    if rough_script is not None:
        body["rough_script"] = rough_script
    return await dc.request("POST", f"api/research/projects/{project_id}/seo/optimize", json=body)


async def get_seo_pack(project_id: str) -> dict:
    """プロジェクトの seo_pack.json（ジャンル/シードキーワード/市場データ）を読み取る。"""
    return await dc.get(f"api/research/projects/{project_id}/seo")


async def generate_publish_pack(project_id: str, episode_number: int) -> dict:
    """確定台本とSEOパックからYouTube公開用メタデータ（タイトル案3/概要欄/ハッシュタグ/タグ）を生成。

    publish_pack.json に保存される。前提: 台本承認済み(approve_script) + seo_optimize 実行済み。
    """
    return await dc.request(
        "POST", f"api/research/projects/{project_id}/episodes/{episode_number}/publish-pack", json={})


async def get_publish_pack(project_id: str, episode_number: int) -> dict:
    """確定話の publish_pack.json（YouTube公開メタデータ）を読み取る。"""
    return await dc.get(f"api/research/projects/{project_id}/episodes/{episode_number}/publish-pack")


async def curate_seo_brief(project_id: str, model: Optional[str] = None) -> dict:
    """既存のseo_packから、台本本文に自然に馴染む厳選キーワード(script_brief)だけをAIで再選定する。

    市場データの再収穫はせずYouTube APIクォータを消費しない。台本生成時はこの厳選キーワードだけが軽く反映される。
    """
    body: dict = {}
    if model is not None:
        body["model"] = model
    return await dc.request("POST", f"api/research/projects/{project_id}/seo/curate", json=body)


async def set_seo_brief(project_id: str, keywords: list[str]) -> dict:
    """台本本文に反映するキーワード(script_brief)を明示的に指定する。

    AIまたは人が「この語だけ台本に効かせる」と決める用途。重複・空は自動除去、最大10語。
    """
    return await dc.request("PATCH", f"api/research/projects/{project_id}/seo/brief",
                            json={"keywords": keywords})


# ── Aロール（マンガ形式パネル / 台本セリフ行→キャラ画像の一括生成） ──────
#
# Aロール＝素材取得ではなく「セリフ1行＝マンガ1コマ」のキャラ画像（2026-07方針転換）。
# 流れ: generate_aroll_prompts(LLM・無料枠) → aroll_status で確認 →
#       run_aroll_batch(NanoBanana実課金 ≈$0.04/枚) → aroll_status でポーリング。
# 正本: episodes/epNN/a_roll/aroll.json（吹き出しはユーザーが編集時に手作業で載せる）。

async def generate_aroll_prompts(project_id: str, episode_number: int,
                                 extra_prompt: Optional[str] = None,
                                 overwrite: bool = False,
                                 aspect: str = "16:9", style: str = "kamishibai",
                                 model: Optional[str] = None,
                                 sections: Optional[list[str]] = None) -> dict:
    """承認済み台本の全セリフ行にマンガ1コマ分の画像生成プロンプトをLLMで用意する。

    章単位でLLM(既定Gemini無料枠→OpenRouter無料)を呼び、aroll.jsonに保存する（課金なし）。
    登場キャラ(1〜2人)もLLMが判定する。overwrite=Trueでも手編集済み(prompt_source=user)は保持。
    前提: 台本承認済み(approve_script)＋配役割当済み(assign_cast)。未割当話者はwarningsに出る。

    sections: 章idの配列。指定した章だけ作る（例: ["ki"]）。**省略で全章**。
    ⚠️ **分割・挿入で足した行のコマを埋めるだけなら、これではなく `aroll_prepare_lines` を使う**
    （LLMを呼ばない・拒否されない・無料）。英語の演出プロンプトは絵を新規生成する時に自動で作られる。
    安全フィルタの拒否が出る章は model="anthropic/claude-sonnet-5" を指定すると通る。
    """
    body: dict = {"overwrite": overwrite, "aspect": aspect, "style": style}
    if sections is not None:
        body["sections"] = sections
    if extra_prompt is not None:
        body["extra_prompt"] = extra_prompt
    if model is not None:
        body["model"] = model
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/prompts",
        json=body)


async def run_aroll_batch(project_id: str, episode_number: int,
                          only_missing: bool = True,
                          line_ids: Optional[list] = None,
                          allow_paid_fallback: bool = False) -> dict:
    """Aロールのパネル画像をバッチ生成する(NanoBanana・外部API課金 ≈$0.04/枚)。

    ⚠️ 課金は行数ではなく**カット数**（同じ画像を共有する連続行の単位）で決まる。対象が
    複数行にまたがるカットなら、先頭行だけ課金され残りの行は自動で同じ絵を共有する
    （実際に生成される枚数は行数より少なくなりうる）。
    バックグラウンド実行＝この呼び出しは即返る。進捗は aroll_status でポーリングする。
    only_missing=True(既定)は生成済みをスキップ＝中断後の再開・失敗行の再試行を兼ねる。
    allow_paid_fallback は既定False＝Gemini失敗時もOpenRouter(Free表示でも課金)へ退避しない。
    実行前に aroll_status で対象枚数を確認し、概算コストをユーザーに提示してから呼ぶこと。

    ⚠️ **課金の前に必ず aroll_cutout_plan → aroll_apply_cutout_plan を試すこと。**
    既存の切り抜き在庫で賄える行は無料で埋まる（実データでは196行中172行が在庫で賄えた
    ＝それを飛ばすと最大7倍の課金になりうる）。在庫適用後に残った行だけをこれで生成する。
    """
    body: dict = {"only_missing": only_missing, "allow_paid_fallback": allow_paid_fallback}
    if line_ids is not None:
        body["line_ids"] = line_ids
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/generate",
        json=body)


async def aroll_status(project_id: str, episode_number: int) -> dict:
    """Aロールの進捗(counts: total/done/failed/pending/no_prompt ＋ 実行中ジョブ)を返す。

    running=Trueの間はバッチ実行中。counts.no_prompt>0 なら先に generate_aroll_prompts が必要。
    sync(ok/stale/missing/orphan/unknown)と in_sync も含む。in_sync=False なら台本とズレている
    ＝行ごとの内訳は aroll_sync で見る。
    """
    return await dc.get(f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/status")


async def aroll_sync(project_id: str, episode_number: int) -> dict:
    """確定台本とAロールの差分を行ごとに返す（検査のみ・生成も課金もしない）。

    台本を後から追加/削除/推敲した後は必ずこれで確認する。items[].sync の意味:
    - stale   … セリフが変わったのに絵が生成時のまま → run_aroll_batch(line_ids=[…]) で描き直す
    - missing … 画像が無い行。status=no_panel（コマ/プロンプトが無い）なら先に aroll_prepare_lines(line_ids=[…])（LLMなし・無料）で
                実体化してから aroll_fill_missing(line_ids=[…]) を呼ぶと無料の手段（カットの
                引き継ぎ→在庫）で埋まる分だけ埋まり、残りは need_generation として返る（2026-09-24）
    - orphan  … 台本から消えた行のPNGが残っているだけ（編集には使われない）
    - unknown … この機能以前に生成された資産（生成時テキストの記録なし）
    stale/unknown は絵を作り直さなくても aroll_approve_images(line_ids=[…]) を呼べば、
    承認と同時に生成時テキストの記録も今の台本へ更新され sync が ok に戻る（2026-09-24統合）。
    ⚠️ line_ids を省略した全行承認では unknown しか直らない（stale はユーザーが絵を見て
    行を指定した時だけ解消する＝人が見ていない stale を黙って一致にしない）。
    """
    return await dc.get(f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/sync")


# ── Aロールの仕上げ工程（在庫からの割当・承認→在庫化・背景一括割当・行編集） ────
#
# generate_aroll_prompts/run_aroll_batch だけでは話数が完走しない
# （Docs/MCP_PARITY_PLAN.md）。ここから先が UI の🖼️Aロールタブが持つ仕上げの口。

async def aroll_cutout_plan(project_id: str, episode_number: int) -> dict:
    """切り抜き在庫で全行を賄えるかを試算する（検査のみ・生成も保存もしない・課金なし）。

    各行が in-stock かどうかと候補 slot_id を返す（items[].slot_id が null なら在庫では
    賄えない＝新規生成が要る行）。**run_aroll_batch で課金する前に必ずこれを呼ぶこと。**
    在庫で埋まる行を先に aroll_apply_cutout_plan で確定してから、残りだけ生成する。
    件数（from_stock / need_generation）は絵がまだ決まっていないカットだけを数える
    （lines[].decided=true の行は数えない）。
    stock_warnings[] はこの話で在庫が薄い感情（例「アオイ thoughtful（物思い） 13行／在庫10枚」）＝
    足りない分は再使用か新規生成になる。詳細は stock_health。
    """
    return await dc.get(f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/cutout-plan")


async def aroll_apply_cutout_plan(project_id: str, episode_number: int,
                                  line_ids: Optional[list[str]] = None) -> dict:
    """aroll_cutout_plan の試算結果を実際に書き込む（在庫で賄える行だけ・無料・画像生成なし）。

    在庫で賄えない行は触らない（そこは run_aroll_batch の担当）。
    line_ids は **省略で在庫が効く全行が対象・空リスト[]で対象ゼロ**。
    省略時は絵が既に決まっているカットを飛ばす（kept_decided に件数）。line_ids を明示すると
    決まっているカットでも選び直す（カット全体が替わり確定も外れる）ので注意。
    """
    body: dict = {}
    if line_ids is not None:
        body["line_ids"] = line_ids
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/cutout-plan/apply",
        json=body)


async def aroll_prepare_lines(project_id: str, episode_number: int,
                              line_ids: list[str]) -> dict:
    """指名した行のコマを**LLMなしで**下ごしらえする（可逆WRITE・無料・画像は生成しない）。

    プロンプトの無いコマ（分割・サブ行追加・挿入でできた行、確定の運用が始まる前の古い行）を埋める:
    サブ行（parent_line_id を持つ行）は親のコマからプロンプト・キャラ・slot を引き継ぎ、独立した行は
    台本の感情からルール（11語）で slot を決める。足りないコマは作る。絵・手直し済みの slot・
    在庫割当済みの行は触らない。冪等。
    `aroll_sync` が missing の行、`get_line_states` で確定済みなのに絵が決まらない行に使う。
    **この後 `aroll_fill_missing`（在庫で埋める）→足りなければ `run_aroll_batch`（課金）**。
    背景は `aroll_assign_backgrounds(only_missing=True)` で割り当てる。
    ⚠️ line_ids は必須（空リストは何もしない）。応答の filled に {inherited, rule}（行idの配列）。
    """
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/lines/sync-structure",
        json={"fill_line_ids": list(line_ids)})


async def aroll_fill_missing(project_id: str, episode_number: int,
                             line_ids: list[str]) -> dict:
    """選択行のうち絵が無いものを、無料の手段だけで埋める(可逆WRITE・課金なし)。

    順序: ①同じカットに既に画像を持つ行があればそれを無料でコピー ②在庫
    (aroll_apply_cutout_plan と同じ経路)。それでも埋まらない行は need_generation
    (カットの先頭行のみ・1カット1枚の原則)として返す。**ここでは課金しない**——
    need_generation を課金生成したい場合は、確認の上で run_aroll_batch(line_ids=...) を
    別途呼ぶこと。埋めた行は image_approved_at を立てない(承認は別途 aroll_approve_images)。

    ⚠️ line_ids は必須(省略不可)。空リストなら何もしない。
    """
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/fill-missing",
        json={"line_ids": line_ids})


async def aroll_approve_images(project_id: str, episode_number: int,
                               line_ids: Optional[list[str]] = None,
                               register: bool = True) -> dict:
    """Aロール画像を承認し、その時点でキャラ所有ライブラリ(在庫)へ取り込む(可逆WRITE・課金なし)。

    生成の瞬間ではなく承認の瞬間に在庫化する(作り直して捨てた絵を在庫に入れないため)。
    在庫に積めない行(キャラ未確定・slot不足・切り抜き失敗等)は skipped に理由が入るだけで
    承認自体は通る(その行の絵はそのまま使われる)。register=False で承認だけして在庫化は
    見送る。

    ⚠️ **line_ids は省略で全行・空リスト[]で対象ゼロ**（falsy判定で同一視しないこと。
    `CHARACTER_CUTOUT_PLAN.md` §13-4）。1行だけの再承認にも同じ口を使う。
    """
    body: dict = {"register": register}
    if line_ids is not None:
        body["line_ids"] = line_ids
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/approve-images",
        json=body)


async def aroll_picture_groups(project_id: str, episode_number: int) -> dict:
    """この話数の「同じ絵を使う行のまとまり」を返す(検査のみ・何も変えない・READ)。

    既定は**1行につき1枚の絵**（サブ行を含む。行という単位を守る）。まとまりができるのは、
    `aroll_share_picture(mode="same_as_previous")` で「前の行と同じ絵を使う」と明示した行だけ
    （吹き出しだけ変わる区間）。話者・セクションが変わる所では必ず別の絵になる。
    旧称は「カット」（Docs/LINE_WORKBENCH_PLAN.md D4 で廃止した語彙）。

    返り値の cuts[] は {cut_id, line_ids, role, duration_sec, speaker_id}（キー名は互換のため旧称のまま）。
    line_ids が2行以上のものが「同じ絵」のまとまり。duration_source が "estimated" ならTTS前の推定尺。
    """
    return await dc.get(
        f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/cuts")


async def aroll_share_picture(project_id: str, episode_number: int, line_id: str,
                              mode: str = "same_as_previous") -> dict:
    """「前の行と同じ絵を使う」かどうかを行ごとに決める(可逆WRITE)。台本の確定状態には影響しない。

    mode: "same_as_previous"=この行は前の行と同じ絵を使う（同じ話者の時だけ有効。話者をまたぐ指定は無視）/
    "own"=この行は自分の絵を使う（別の絵にする）/ "auto"=手直しを消して自動の判定へ戻す。
    手直しは `aroll.json` に保存され、再計算しても壊れない。
    """
    body = {"same_as_previous": {"boundary": "join"}, "own": {"boundary": "start"}, "auto": {"reset": True}}.get(mode)
    if body is None:
        raise ValueError('mode は "same_as_previous" / "own" / "auto" のいずれかです')
    return await dc.request(
        "PUT",
        f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/cuts/{line_id}",
        json=body)


async def aroll_duplicates(project_id: str, episode_number: int, window: int = 30) -> dict:
    """同じ絵・よく似た絵の繰り返しを検査する(READ・無料・何も変えない)。範囲は**この話数の中だけ**。

    items[] が「直す対象の行」: kind=exact（同じ絵が別の行にも）/ near（window 行以内に向きが同じで
    よく似た絵）。with[] が重なっている相手の行。最初に出た側は残し、後の行を直す対象にする。
    除外（items に出ない）: 人が選んだ絵・Photoshop で手直し済み(✋)・実画像を持つ行・ナレーション/2ショット・
    同じ絵を使うと明示した行のまとまりの中の共有。守られた行どうしの重なりは conflicts に出るだけ（直さない）。
    summary は {items, exact, near, lines, conflicts, protected_cuts}。
    直す時は選び直し（無料）→ 在庫が無い行だけ生成（課金・既存の見積もり確認）の順（`aroll_fix_duplicates`）。
    """
    return await dc.get(f"projects/{project_id}/episodes/{episode_number}/aroll-duplicates",
                        params={"window": window}, timeout=120)


async def aroll_fix_duplicates(project_id: str, episode_number: int, mode: str = "reselect",
                               apply: bool = False, line_ids: Optional[list[str]] = None,
                               window: int = 30) -> dict:
    """`aroll_duplicates` の指摘を直す(WRITE・無料・**画像は生成しない**)。**既定は案を返すだけ(apply=False)**。

    手順: ①apply=False で plan を見る → ②ユーザーに見せて了承 → ③apply=True で書く。
    mode="reselect": この話数で**使っていない**在庫の絵へ選び直す。plan[].action は reselect（替えの絵がある）/
      generate（在庫に替えが無い＝生成が要る・この mode では触らない）/ skip（感情未指定など自動では選べない）。
    mode="unassign": 替えが無い行(generate)の絵を外して未決定にする。そのあと**応答の changed の行だけ**を
      `run_aroll_batch(line_ids=changed, only_missing=True)`（**課金**・先に見積もりをユーザーへ）で生成する。
      ⚠️ **外した行に `aroll_fill_missing`（在庫で埋める）を使わない**: 在庫が尽きた行へ同じ絵が再使用で当て直され、
      重複が戻る（実測）。この道具自身は生成も課金もしない。
    人が選んだ絵・Photoshop で手直し済み(✋)・実画像を持つ行には触らない。選び直した行は絵の「確定」が外れ、
    仕上がりは要・再合成になる（合成の前に直すと安い）。応答の fix_id で `aroll_undo_duplicate_fix` できる。
    line_ids: 指摘のうちこの行だけを直す。省略は指摘すべて。
    """
    body: dict = {"mode": mode, "apply": apply, "window": window}
    if line_ids is not None:
        body["line_ids"] = line_ids
    return await dc.request("POST", f"projects/{project_id}/episodes/{episode_number}/aroll-duplicates/fix",
                            json=body)


async def aroll_undo_duplicate_fix(project_id: str, episode_number: int, fix_id: Optional[str] = None) -> dict:
    """直前（または fix_id）の `aroll_fix_duplicates` を戻す(可逆WRITE)。絵の割当と確定を元に戻す。
    その後に別の絵へ変わった行は戻さない（応答の skipped に理由）。生成した絵は戻せない・戻す対象でもない。"""
    return await dc.request("POST", f"projects/{project_id}/episodes/{episode_number}/aroll-duplicates/undo",
                            json={"fix_id": fix_id} if fix_id else {})


async def aroll_cuts(project_id: str, episode_number: int) -> dict:
    """【旧名・非推奨】`aroll_picture_groups` の別名（「カット」の語彙は廃止した）。同じ結果を返す。"""
    return await aroll_picture_groups(project_id, episode_number)


async def aroll_set_cut(project_id: str, episode_number: int, line_id: str,
                        boundary: Optional[str] = None,
                        reset: bool = False) -> dict:
    """【旧名・非推奨】`aroll_share_picture` の別名。boundary="join"→same_as_previous /
    "start"→own / reset=True→auto。新しく書くなら `aroll_share_picture` を使う。"""
    if reset:
        return await aroll_share_picture(project_id, episode_number, line_id, "auto")
    return await aroll_share_picture(
        project_id, episode_number, line_id,
        {"join": "same_as_previous", "start": "own"}.get(boundary or "", ""))


async def aroll_assign_backgrounds(project_id: str, episode_number: int,
                                   only_missing: bool = True,
                                   line_ids: Optional[list[str]] = None) -> dict:
    """全行の背景を背景アーカイブから自動割当する(無料・画像は一切生成しない・可逆WRITE)。

    行の(shot→framing, emotion→mood)から既存アーカイブより1件選んで割り当てる。
    approve_script(=approve-and-prepare)がonly_missing=True固定で自動的にも呼ぶので、
    通常は明示的に呼ぶ必要はない。手で選び直した行をまとめて割当し直したい時や、
    aroll_sync で missing が出た時にこれで使う。

    only_missing=True(既定): 既にbackground_idを持つ行はスキップ(手動選択を保護)。
    False: 全行を割当し直す(既存の手動選択も上書きする)。
    ⚠️ **line_ids は省略で全行・空リスト[]で対象ゼロ**（`CHARACTER_CUTOUT_PLAN.md` §13-4）。
    """
    body: dict = {"only_missing": only_missing}
    if line_ids is not None:
        body["line_ids"] = line_ids
    return await dc.request(
        "POST",
        f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/backgrounds/auto_assign",
        json=body)


async def aroll_update_line(project_id: str, episode_number: int, line_id: str,
                            prompt: Optional[str] = None,
                            slot: Optional[dict] = None,
                            characters: Optional[list[str]] = None,
                            background_id: Optional[str] = None,
                            bubble_key: Optional[str] = None) -> dict:
    """Aロール1行のプロンプト/演技スロット/登場キャラ/背景/吹き出しの形を手直しする(可逆WRITE・課金なし)。

    prompt を書くと prompt_source="user" になる(以後の一括上書きから保護される)。
    slot={emotion,shot,angle,pose?} を渡すと slot_source="user" になり、演技スロットに
    紐づく在庫/背景の照合キーだけを画像生成LLMの散文と独立に差し替えられる。
    background_id は空文字""で未割当に戻す(Noneは「変更しない」の意味＝他の引数と同じ)。
    bubble_key は吹き出しの形の上書き(rect_a/rect_b/round_a/cloud_a/cloud_b/spike_a/spike_b の横7種。
    縦型は不可・未知のキーはエラー)。空文字""で自動(話者の既定＋「！」でトゲ・「？」で雲)へ戻す。
    変えると、その行は「要合成」になる(組版プランは合成のたびに作り直される＝次の合成で反映。
    合成は psassist_run(kind="resync") か director の仕上がりタブ)。
    line_id は get_script/aroll_sync の行id。この編集だけでは画像は再生成されない
    (絵を作り直したい時は run_aroll_batch や1行再生成の別口を使う)。
    """
    body: dict = {}
    if prompt is not None:
        body["prompt"] = prompt
    if slot is not None:
        body["slot"] = slot
    if characters is not None:
        body["characters"] = characters
    if background_id is not None:
        body["background_id"] = background_id
    if bubble_key is not None:
        body["bubble_key"] = bubble_key
    return await dc.request(
        "PUT", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/lines/{line_id}",
        json=body)


async def aroll_export(project_id: str, episode_number: int) -> dict:
    """Photoshop等の手作業向けに a_roll/export/ へ行番号付きコピー＋script_lines.txtを書き出す。

    ⚠️ **旧・手作業用の書き出し**（現行の psassist ホスト工程は a_roll/ を直接読むため
    通常は不要）。Photoshopでの組版・検査・納品PNGは psassist_run(kind="build_plan"/
    "cutout"/"build_panel"/"qa_check"/"export_png") を使うこと。

    正本(a_roll/*.png)はリネームしない。export/フォルダは毎回全消去して作り直すため、
    台本を編集した後はもう一度呼ぶだけで良い。stale(台本とズレ)行と未生成行はコピーされず
    export/_README.txtに欠番として列挙される。課金なし・破壊的操作でもない（export/以外は触らない）。
    """
    return await dc.request(
        "POST", f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/export")


# ── ホスト工程（psassist・Photoshop） ────────────────────────────────
#
# psassist/ はコンテナではない（Photoshop / win32com のホスト常駐工程）。director は
# queue/ にジョブを書き state/ を読むだけで、host_worker.py とはHTTPで繋がらない
# （psassist/README.md）。組む/検査/納品PNGまでの全工程がここで揃う。

async def psassist_worker_status() -> dict:
    """host_worker.py（ホスト常駐・Photoshop）の生死をハートビートで返す（プロジェクト非依存）。

    alive=False またはこの呼び出し自体が404なら、psassist_run でジョブを積んでも
    キューに溜まるだけで何も起きない。**psassist_run の前に必ずこれを確認すること。**
    """
    return await dc.get("psassist/worker")


async def psassist_qa(project_id: str, episode_number: int) -> dict:
    """Photoshop合成結果の検査レポート（psassist/scripts/qa_check.py が書いたもの）を読む。

    psassist_run(kind="qa_check") の実行後に確認する。まだ検査していなければ404。
    """
    return await dc.get(f"projects/{project_id}/episodes/{episode_number}/psassist/qa")


async def psassist_jobs(project_id: str, episode_number: int) -> dict:
    """その話数のホスト工程ジョブを新しい順に返す（上限50件・status/progress/logを含む）。

    psassist_run の後はこれをポーリングして進捗を追う。
    """
    return await dc.get(f"projects/{project_id}/episodes/{episode_number}/psassist/jobs")


async def psassist_run(project_id: str, episode_number: int, kind: str,
                       lines: Optional[list[str]] = None, force: bool = False,
                       include_edited: bool = False) -> dict:
    """host_worker.py（ホスト常駐のPhotoshop工程）へジョブを1件キューに積む。

    ⚠️ **`cutout` / `build_panel` / `export_png` / `resync` は Photoshop を占有する。** 他の用途で
    Photoshopを使っていると衝突する。**実行前に必ずユーザーへ確認を取ってから呼ぶこと。**

    kind: "build_plan"（配置計画）| "cutout"（キャラ切り抜き・Photoshop占有）|
    "build_panel"（コマ合成・Photoshop占有）| "qa_check"（検査・結果はpsassist_qaで読む）|
    "export_png"（納品PNG書き出し・Photoshop占有）|
    "resync"（T3: 在庫を選び直した行を①build_plan→③build_panel→④qa_check→⑤export_pngで
    1ジョブに連鎖して組み直す・Photoshop占有・要組み直しの行だけが対象）。
    （ほかにワークベンチ専用の "open_psd"＝PSDをPhotoshopで開く・"library_cutout"＝在庫のPS切り抜きがある。）
    ⚠️ ホスト工程（host_worker）が止まっているとジョブは積まれるだけで進まない。先に psassist_worker_status で確認。

    lines: 対象行のline_id配列。build_plan/cutout/build_panel/qa_checkは省略で「全件」。
    ⚠️ **export_png と resync だけは lines 省略不可**（空/省略はエラーになる）。
    export_pngの理由: 進捗の `--resume` はファイルの更新日時を見ないため、全件指定だと
    直した行だけ描き出すつもりが古い版のまま飛ばされる。直した行のline_idを明示すること。
    resyncの理由: 「要組み直し」の対象を明示させる設計（全件を毎回殴らない）。

    force: build_panel/resync/export_pngで、対象行にPS切り抜き未処理（rembgの仮絵のまま）の
    ものがあると既定では409で止まる（`Docs/CUTOUT_PS_PRIMARY_PLAN.md` P3）。409が返ったら
    detailの`ps_pending_lines`（対象行）・`worker_alive`（host_workerの生死）をユーザーに見せて
    「待つ（何もしない）」か「このまま進める」か確認し、進める場合だけforce=Trueで呼び直すこと。
    PS環境を使っていない環境（CUTOUT_PS=off・worker.json無し）ではそもそも409にならない。

    **再合成**（旧称「組版し直す」）＝Photoshop で1行分の合成を最初から作り直すこと。`resync`（または lines を
    指定した `build_panel`）がそれ。確定の運用が始まっている話数では、再合成は**確定済みの行だけ**が対象
    （未確定の行は飛ばす。対象が空なら409）。

    ⚠️ **手直しの保護**: Photoshop で手直しした行（画面で「✋ 手直し済み」）は、`build_panel`/`resync` が
    **既定で飛ばす**（手直しを上書きで消さないため。ジョブ結果の `skipped_edited` に行が出る）。
    上書きするなら include_edited=True（**必ずユーザーに確認してから**。元のPSDを `psd_final/_backup/` に
    退避してから上書きする＝結果の `backed_up`）。

    ⚠️ **先に psassist_worker_status() で alive を確認すること。** worker が動いていないと
    ジョブはキューに積まれるだけで何も実行されない（無言で放置される）。
    進捗は psassist_jobs をポーリングして status/log を見る。
    """
    body: dict = {"kind": kind}
    if lines is not None:
        body["lines"] = lines
    if force:
        body["force"] = True
    if include_edited:
        body["args"] = {"include_edited": True}
    return await dc.request(
        "POST", f"projects/{project_id}/episodes/{episode_number}/psassist/jobs", json=body)


# ── オープニング（モーショングラフィック・motion-agent）─────────────────
#
# 設計: Docs/OPENING_MOTION_PLAN.md。director の汎用プロキシ /api/motion/* 経由で motion-agent(:8007)を叩く。
# 型（テンプレート）の枠を埋めた「描画入力」を検査し、静止画で確かめ、本描画（音なしmp4）する。
# 画像を返すツールは戻り値の "_images"（[{"name", "png": bytes, "format": "png"|"jpeg"}]）を server.py が画像として返す
# （tools.py を FastMCP に依存させない）。

def _motion_body(template_id: str, version: int, sample: str, render_input: Optional[dict],
                 frames: Optional[list[dict]] = None) -> dict:
    if not sample and render_input is None:
        raise ValueError("sample（型に同梱の見本の名前）か render_input（描画入力）のどちらかを指定してください")
    body: dict = {"template_id": template_id, "version": version}
    if sample:
        body["sample"] = sample
    else:
        body["input"] = render_input
    if frames:
        body["frames"] = frames
    return body


async def opening_templates() -> dict:
    """オープニングの型（テンプレート）の一覧。各型の template_id・version・要約・見本の名前を返す。

    次に opening_template_meta で枠（個数・字数・秒・選べるつまみ）を読み、描画入力を作る。
    """
    return await dc.get("api/motion/templates")


async def opening_template_meta(template_id: str = "kinetic_teaser", version: int = 1,
                                sample: str = "") -> dict:
    """型の meta.json（枠の制約の本籍）。sample を指定するとその見本の描画入力も返す（書き方の例）。

    meta: beats（①〜⑤の個数・字数・秒の範囲）・max_total_sec（20秒）・variants（選べるつまみ）。
    描画入力はこの制約を満たして書く（満たさないと opening_validate が日本語で指摘する）。
    """
    out = await dc.get(f"api/motion/templates/{template_id}/v{version}")
    if sample:
        out["sample_input"] = await dc.get(f"api/motion/templates/{template_id}/v{version}/samples/{sample}")
    return out


async def opening_fonts() -> dict:
    """書体の束の一覧（id・family・太さ・用途 role・用途タグ tags・日本語の有無 ja・ユーザーの書体か user）。
    数百ある時は見本帳 opening_font_specimen（画像）で選ぶ。

    演出プランや描画入力では書体を id で指定する。ユーザーの書体（購入したものなど）は
    shared/motion/fonts/ に置くと自動で加わる（id は user_<ファイル名>）。
    """
    return await dc.get("api/motion/fonts")


async def opening_validate(template_id: str = "kinetic_teaser", version: int = 1, sample: str = "",
                           render_input: Optional[dict] = None) -> dict:
    """描画入力を検査し、timing（ビートの秒・②の切り替え・③のカット・効果音の打点）を返す。描画はしない。

    ok が false なら errors（field と日本語の message）を直してから opening_stills へ。
    合計20秒を超える入力もここで指摘される。
    """
    return await dc.request("POST", "api/motion/validate",
                            json=_motion_body(template_id, version, sample, render_input))


async def opening_stills(template_id: str = "kinetic_teaser", version: int = 1, sample: str = "",
                         render_input: Optional[dict] = None, frames: Optional[list[dict]] = None,
                         focus: Optional[list[str]] = None, wait: int = 120) -> dict:
    """確認用の静止画を描き、**画像として**返す（見本シート1枚＋focus で指定した静止画）。

    既定はビートごとの見せ場9枚を3列の見本シートにしたもの（order の順に左上から）。
    frames: [{"name": "...", "frame": 整数}] で見たいフレームを指定できる（名前は英数字・_・-）。
    focus: 個別に大きく見たい静止画の名前（最大3つ。order の名前）。
    描画はローカルCPUで数秒（課金なし）。型の見た目を直す時の確認に使う。
    """
    body = _motion_body(template_id, version, sample, render_input, frames)
    job = await dc.request("POST", f"api/motion/stills?wait={max(0, min(int(wait), 300))}", json=body)
    return await _stills_result(job, focus)


async def _stills_result(job: dict, focus: Optional[list[str]]) -> dict:
    """静止画ジョブの結果を、見本シート（＋focus）の画像つきの戻り値にする。"""
    out = {"job_id": job["job_id"], "status": job["status"], "elapsed_sec": job.get("elapsed_sec"),
           "error": job.get("error")}
    if job["status"] != "done":
        out["note"] = "まだ終わっていません。opening_status(job_id) で確かめてください" if not job.get("error") else "描画に失敗しました"
        return out
    res = job["result"]
    images = []
    sheet = res.get("sheet")
    if sheet:
        images.append({"name": "sheet", "format": "jpeg", "png": await dc.get_bytes("api/motion" + sheet["url"])})
        out["order"], out["columns"] = sheet["order"], sheet["columns"]
    by_name = {s["name"]: s for s in res["stills"]}
    out["frames"] = {s["name"]: s["frame"] for s in res["stills"]}
    for name in (focus or [])[:3]:
        if name not in by_name:
            out.setdefault("warnings", []).append(f"focus の {name} という静止画はありません")
            continue
        images.append({"name": name, "png": await dc.get_bytes("api/motion" + by_name[name]["url"])})
    out["_images"] = images
    return out


async def opening_render(template_id: str = "kinetic_teaser", version: int = 1, sample: str = "",
                         render_input: Optional[dict] = None) -> dict:
    """本描画（1080p30・音なしの mp4）のジョブを投入する。進捗は opening_status(job_id)。

    ローカルCPUで約1.5〜2分（課金なし）。ジョブは1本ずつ順に処理される。
    注意: 現段階（M1）は作業用の置き場 shared/motion/_work/{job_id}/out.mp4 に出る。
    話ごとの納品フォルダ（delivery/opening/）への書き出しは M2 から。
    """
    return await dc.request("POST", "api/motion/render",
                            json=_motion_body(template_id, version, sample, render_input))


async def opening_status(job_id: str) -> dict:
    """描画・静止画ジョブの状態（queued/bundling/running/done/error・progress・result・error）。"""
    return await dc.get(f"api/motion/jobs/{job_id}")


# ── オープニングの演出プラン（型 kinetic_teaser v2・設計: Docs/OPENING_MOTION_PLAN.md §18） ──
#
# 演出プラン＝型 v1 の①〜⑤を土台に、ビートごとにギミック（ID で呼ぶ）へ差し替えるショットの並び。
# 話ごとに保存される（episodes/epNN/opening/plan.json・履歴つき）。検査（尺・安全域・出典・書体・明滅）は
# 保存と描画の前に必ず走る。手順は Skill yt-motion-design。

async def opening_gimmicks() -> dict:
    """ギミックの台帳: ID（カタログ ID＋名前）・効き方・秒の範囲・params の書き方・繋ぎ・重ねもの・別名。
    ビートごとに使えるギミック（beats.*.allowed）と、K3 のカットの既定も返す。
    演出プランを書く前に必ず読む（何が実装済みかの本籍はここ）。"""
    return await dc.get("api/motion/gimmicks")


def _plan_path(project_id: str, episode_number: int, tail: str = "") -> str:
    return f"api/motion/plans/{project_id}/{int(episode_number)}{tail}"


async def opening_plan_get(project_id: str, episode_number: int) -> dict:
    """話の演出プランと検査の要約を返す。まだ無ければ exists: false（opening_plan_save で作る）。

    analysis: errors（field と日本語の message）・warnings・shots（ショットごとの秒と「何小節目の何拍目」）・
    flash_estimate（明滅の概算）・resolved_fonts（role: 指定で選ばれた書体）。"""
    try:
        return {"exists": True, **await dc.get(_plan_path(project_id, episode_number))}
    except dc.DirectorError as e:
        if "404" in str(e) and "演出プランがありません" in str(e):
            return {"exists": False, "note": "まだ保存されていません。opening_plan_save で作ります（型 v1 のブリーフだけの plan でも描ける）"}
        raise


async def opening_plan_save(project_id: str, episode_number: int, plan: dict) -> dict:
    """演出プランを保存して検査する（検査に落ちても JSON として読めれば保存される＝途中の状態を失わない。
    直前の版は履歴に残る）。返り値の analysis.errors が空になるまで直してから stills・render へ。

    plan: {"brief": {variant, b1_first, b2_flow, b3_montage（素材ごとに source＝出典）, b4_out, b5_title},
           "direction": {"bpm": 120, "overlay": {hud, texture},
                         "beats": {"b1_first": [{"id","gimmick","bars","params","enter"}], ...}},
           "notes": "演出の意図"}
    - direction に書かなかったビートは型 v1 の既定（K1〜K5）で描く。params を省くと brief から埋まる。
    - 長さ bars は小節（0.25 の倍数）。繋ぎ enter: {"type": A1_portal|A3_light|A4_iris|morph, "bars", "x", "y"}。
    - ギミックの ID と書き方は opening_gimmicks。書体は id か "role:<用途タグ>"（opening_fonts・opening_font_specimen）。
    課金なし。"""
    return await dc.request("PUT", _plan_path(project_id, episode_number), json={"plan": plan})


async def opening_plan_history(project_id: str, episode_number: int, name: str = "") -> dict:
    """保存の履歴。name を省くと名前の一覧（新しい順・最大20）、指定するとその版のプラン（戻したい時は opening_plan_save へ）。"""
    if name:
        return await dc.get(_plan_path(project_id, episode_number, f"/history/{name}"))
    return await dc.get(_plan_path(project_id, episode_number, "/history"))


async def opening_plan_stills(project_id: str, episode_number: int, frames: Optional[list[dict]] = None,
                              focus: Optional[list[str]] = None, wait: int = 120) -> dict:
    """保存済みのプランの確認用の静止画を**画像として**返す（ショットごとに1枚＋繋ぎの途中・最大12枚を4列の見本シート）。
    order の順に左上から。frames: [{"name","frame"}] で見たいフレームを指定、focus: 大きく見たい静止画の名前（最大3）。
    検査に誤りがあると 422。約6〜10秒・課金なし。"""
    body = {"frames": frames} if frames else None
    job = await dc.request("POST", _plan_path(project_id, episode_number, f"/stills?wait={max(0, min(int(wait), 300))}"),
                           json=body)
    return await _stills_result(job, focus)


async def opening_plan_render(project_id: str, episode_number: int) -> dict:
    """保存済みのプランを本描画（1080p30・音なしの mp4・約1〜2分・課金なし）。進捗と結果は opening_status(job_id)。
    終わると result.flash に**明滅の実測**（ok・max_per_sec・worst_at_sec。画面の25%以上・毎秒3回まで）が入る。
    ok が false なら、該当の秒付近を直して（K3 の min_sec を伸ばす・語の切り替えを遅くする・明るさの近い素材）描き直す。
    検査に誤りがあると 422。"""
    return await dc.request("POST", _plan_path(project_id, episode_number, "/render"))


async def opening_font_specimen(text: str = "洗脳と記録 MK-ULTRA 2025", ids: Optional[list[str]] = None, tag: str = "",
                                ja_only: bool = True, user_only: bool = False, query: str = "",
                                limit: int = 24, offset: int = 0) -> dict:
    """書体の見本帳（1行1書体の画像）を**画像として**返す。書体は数百あるので、名前でなく見て選ぶ。
    絞り込み: ids（id か role:<タグ>）／tag（強調・不穏・機械・手書き・章題・毛筆・遊び・標準）／user_only（購入した書体だけ）／
    query（id か家族名に含まれる語）。offset で続きを見る（total が全体の数）。
    気に入った書体に用途タグを付けるには shared/motion/fonts/fonts.json に {"fonts": {"<id>": {"tags": ["不穏"]}}} と書く。"""
    body = {"text": text, "ja_only": ja_only, "user_only": user_only, "limit": limit, "offset": offset}
    for k, v in (("ids", ids), ("tag", tag), ("query", query)):
        if v:
            body[k] = v
    res = await dc.request("POST", "api/motion/fonts/specimen", json=body)
    res["_images"] = [{"name": "specimen", "format": "jpeg", "png": await dc.get_bytes("api/motion" + res.pop("url"))}]
    return res


# ── レジストリ（server.py / 後継ループ が参照する単一の出所） ──────────

S = SideEffect
TOOLS = [
    {"fn": check_openrouter_credits, "side_effects": [S.READ]},
    {"fn": check_vecteezy_quota, "side_effects": [S.READ]},
    {"fn": check_youtube_quota,  "side_effects": [S.READ]},
    {"fn": list_projects,        "side_effects": [S.READ]},
    {"fn": project_status,       "side_effects": [S.READ]},
    {"fn": list_styles,          "side_effects": [S.READ]},
    {"fn": set_project_style,    "side_effects": [S.WRITE]},
    {"fn": create_style,         "side_effects": [S.WRITE]},
    {"fn": update_style,         "side_effects": [S.WRITE]},
    {"fn": delete_style,         "side_effects": [S.WRITE]},
    {"fn": create_project,       "side_effects": [S.WRITE]},
    {"fn": generate_script,      "side_effects": [S.WRITE]},
    {"fn": generate_series_script, "side_effects": [S.WRITE]},
    {"fn": approve_script,       "side_effects": [S.WRITE]},
    # 台本の行の操作（director の窓口経由・W5）。音声・コマ・確定が追随する
    {"fn": update_script_line,   "side_effects": [S.WRITE]},
    {"fn": insert_script_line,   "side_effects": [S.WRITE]},
    {"fn": move_script_line,     "side_effects": [S.WRITE]},
    {"fn": delete_script_line,   "side_effects": [S.WRITE]},
    {"fn": get_llm_proposal,     "side_effects": [S.READ]},
    {"fn": adopt_llm_proposal,   "side_effects": [S.WRITE]},
    {"fn": audit_episode,        "side_effects": [S.READ]},
    {"fn": get_line_states,      "side_effects": [S.READ]},
    {"fn": start_confirmations,  "side_effects": [S.WRITE]},
    {"fn": confirm_lines,        "side_effects": [S.WRITE, S.GPU, S.ASYNC]},
    {"fn": undo_line_op,         "side_effects": [S.WRITE]},
    {"fn": get_line_history,     "side_effects": [S.READ]},
    {"fn": regenerate_lines,     "side_effects": [S.WRITE]},
    {"fn": import_script,        "side_effects": [S.WRITE]},
    {"fn": get_script,           "side_effects": [S.READ]},
    {"fn": split_line,           "side_effects": [S.WRITE]},
    {"fn": merge_line_with_next, "side_effects": [S.WRITE]},
    {"fn": add_subline,          "side_effects": [S.WRITE]},
    {"fn": get_split_proposal,   "side_effects": [S.READ]},
    {"fn": apply_split_proposal, "side_effects": [S.WRITE]},
    {"fn": apply_split_proposal_all, "side_effects": [S.WRITE]},
    {"fn": generate_queries,     "side_effects": [S.WRITE]},
    {"fn": search_footage,       "side_effects": [S.WRITE, S.COST]},
    {"fn": auto_select_footage,  "side_effects": [S.WRITE]},
    {"fn": select_footage,       "side_effects": [S.WRITE, S.COST]},
    {"fn": run_tts,              "side_effects": [S.GPU, S.ASYNC]},
    {"fn": build_timeline,       "side_effects": [S.WRITE]},
    # キャラ台帳・配役（登録＝台本参加の入口／可逆WRITE）
    {"fn": list_characters,      "side_effects": [S.READ]},
    {"fn": get_character,        "side_effects": [S.READ]},
    {"fn": create_character,     "side_effects": [S.WRITE]},
    {"fn": update_character,     "side_effects": [S.WRITE]},
    {"fn": list_voices,          "side_effects": [S.READ]},
    {"fn": assign_cast,          "side_effects": [S.WRITE]},
    # 紙芝居パネル
    {"fn": list_panel_presets,   "side_effects": [S.READ]},
    {"fn": generate_character_panel, "side_effects": [S.COST]},
    # Aロール（セリフ行→マンガ形式パネル）
    {"fn": generate_aroll_prompts, "side_effects": [S.WRITE]},
    {"fn": run_aroll_batch,      "side_effects": [S.COST, S.ASYNC]},
    {"fn": aroll_status,         "side_effects": [S.READ]},
    {"fn": aroll_sync,           "side_effects": [S.READ]},
    {"fn": aroll_export,         "side_effects": [S.WRITE]},
    {"fn": aroll_cutout_plan,    "side_effects": [S.READ]},
    {"fn": aroll_apply_cutout_plan, "side_effects": [S.WRITE]},
    {"fn": aroll_prepare_lines,  "side_effects": [S.WRITE]},
    {"fn": aroll_fill_missing,   "side_effects": [S.WRITE]},
    {"fn": aroll_approve_images, "side_effects": [S.WRITE]},
    {"fn": aroll_assign_backgrounds, "side_effects": [S.WRITE]},
    {"fn": aroll_picture_groups, "side_effects": [S.READ]},
    {"fn": aroll_share_picture,  "side_effects": [S.WRITE]},
    {"fn": aroll_duplicates,     "side_effects": [S.READ]},
    {"fn": aroll_fix_duplicates, "side_effects": [S.WRITE]},
    {"fn": aroll_undo_duplicate_fix, "side_effects": [S.WRITE]},
    {"fn": aroll_cuts,          "side_effects": [S.READ]},        # 旧名（別名）
    {"fn": aroll_set_cut,        "side_effects": [S.WRITE]},       # 旧名（別名）
    {"fn": aroll_update_line,    "side_effects": [S.WRITE]},
    # ホスト工程（psassist・Photoshop）
    {"fn": psassist_worker_status, "side_effects": [S.READ]},
    {"fn": psassist_qa,          "side_effects": [S.READ]},
    {"fn": psassist_jobs,        "side_effects": [S.READ]},
    {"fn": psassist_run,         "side_effects": [S.WRITE, S.ASYNC]},
    # 背景アーカイブ（台本非依存の再利用可能な背景ライブラリ）
    {"fn": list_background_presets, "side_effects": [S.READ]},
    {"fn": list_backgrounds,     "side_effects": [S.READ]},
    {"fn": generate_background,  "side_effects": [S.COST]},
    {"fn": delete_background,    "side_effects": [S.WRITE]},
    # キャラ所有ライブラリ（Phase 3・Aロール演技スロットの作り置き）
    {"fn": list_panel_library,   "side_effects": [S.READ]},
    {"fn": stock_health,         "side_effects": [S.READ]},
    {"fn": generate_panel_library_entry, "side_effects": [S.COST]},
    {"fn": generate_panel_library_variants, "side_effects": [S.COST]},
    {"fn": approve_panel_library_entry,  "side_effects": [S.WRITE]},
    {"fn": delete_panel_library_entry,   "side_effects": [S.WRITE]},
    # 自由生成（台本非依存）
    {"fn": list_imagegen_styles, "side_effects": [S.READ]},
    {"fn": free_generate,        "side_effects": [S.COST, S.GPU]},
    {"fn": free_audio,           "side_effects": [S.COST]},
    {"fn": free_tts,             "side_effects": [S.GPU]},
    {"fn": free_save,            "side_effects": [S.WRITE]},
    {"fn": free_video,           "side_effects": [S.COST, S.ASYNC]},
    {"fn": free_video_status,    "side_effects": [S.WRITE]},
    # リサーチ（探索→蒸留→ラフ台本）
    {"fn": research_list_sources, "side_effects": [S.READ]},
    {"fn": research_search,      "side_effects": [S.COST]},
    {"fn": research_add_source,  "side_effects": [S.WRITE]},
    {"fn": research_digest,      "side_effects": [S.WRITE]},
    {"fn": research_get_digest,  "side_effects": [S.READ]},
    # YouTube SEO オプティマイザ（市場データ収穫→公開メタデータ生成）
    {"fn": seo_optimize,         "side_effects": [S.WRITE, S.COST]},
    {"fn": get_seo_pack,         "side_effects": [S.READ]},
    {"fn": generate_publish_pack, "side_effects": [S.WRITE]},
    {"fn": get_publish_pack,     "side_effects": [S.READ]},
    # SEO キュレーション（厳選キーワード・台本反映用）
    {"fn": curate_seo_brief,     "side_effects": [S.WRITE]},
    {"fn": set_seo_brief,        "side_effects": [S.WRITE]},
    # オープニング（モーショングラフィック・motion-agent。課金なし・CPU描画）
    {"fn": opening_templates,    "side_effects": [S.READ]},
    {"fn": opening_template_meta, "side_effects": [S.READ]},
    {"fn": opening_fonts,        "side_effects": [S.READ]},
    {"fn": opening_validate,     "side_effects": [S.READ]},
    {"fn": opening_stills,       "side_effects": [S.WRITE, S.ASYNC]},
    {"fn": opening_render,       "side_effects": [S.WRITE, S.ASYNC]},
    {"fn": opening_status,       "side_effects": [S.READ]},
    {"fn": opening_gimmicks,     "side_effects": [S.READ]},
    {"fn": opening_plan_get,     "side_effects": [S.READ]},
    {"fn": opening_plan_save,    "side_effects": [S.WRITE]},
    {"fn": opening_plan_history, "side_effects": [S.READ]},
    {"fn": opening_plan_stills,  "side_effects": [S.WRITE, S.ASYNC]},
    {"fn": opening_plan_render,  "side_effects": [S.WRITE, S.ASYNC]},
    {"fn": opening_font_specimen, "side_effects": [S.READ]},
]
