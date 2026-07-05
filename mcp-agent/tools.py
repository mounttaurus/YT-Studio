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
from enum import Enum
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
    """指定話のドラフトを承認し script.json として確定する（次工程の前提）。"""
    return await dc.request(
        "POST", f"api/scripting/projects/{project_id}/approve",
        params={"episode_number": episode_number},
    )


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
                        aspect: str = "16:9") -> dict:
    """台本非依存でテキストから画像を count 枚生成し staging 候補にする（t2i のみ）。

    provider: nanobanana(外部API課金) | comfy(ローカルSD/GPU・無料)。参照画像i2iは非対応（テキストのみ）。
    確定保存は free_save。COST/GPU 分類＝確認ゲート対象。
    """
    form = {"provider": provider, "mode": "t2i", "prompt": prompt,
            "style": style, "count": count, "aspect": aspect}
    return await dc.request("POST", "api/scrapping/imagegen/free/generate", data=form)


async def free_audio(prompt: str) -> dict:
    """Lyriaで BGM/効果音を1本生成し staging 候補(MP3)にする（外部API課金）。確定は free_save。"""
    return await dc.request("POST", "api/scrapping/imagegen/free/audio/generate",
                            json={"prompt": prompt})


async def free_save(name: str, save_name: str = "") -> dict:
    """staging の候補(name)を direct_output/ に確定保存する。save_name は任意の確定名。"""
    return await dc.request("POST", "api/scrapping/imagegen/free/save",
                            json={"name": name, "save_name": save_name})


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
                                 model: Optional[str] = None) -> dict:
    """承認済み台本の全セリフ行にマンガ1コマ分の画像生成プロンプトをLLMで用意する。

    章単位でLLM(既定Gemini無料枠→OpenRouter無料)を呼び、aroll.jsonに保存する（課金なし）。
    登場キャラ(1〜2人)もLLMが判定する。overwrite=Trueでも手編集済み(prompt_source=user)は保持。
    前提: 台本承認済み(approve_script)＋配役割当済み(assign_cast)。未割当話者はwarningsに出る。
    """
    body: dict = {"overwrite": overwrite, "aspect": aspect, "style": style}
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
    """Aロールのパネル画像をバッチ生成する(NanoBanana・外部API課金 ≈$0.04/枚×対象行数)。

    バックグラウンド実行＝この呼び出しは即返る。進捗は aroll_status でポーリングする。
    only_missing=True(既定)は生成済みをスキップ＝中断後の再開・失敗行の再試行を兼ねる。
    allow_paid_fallback は既定False＝Gemini失敗時もOpenRouter(Free表示でも課金)へ退避しない。
    実行前に aroll_status で対象枚数を確認し、概算コストをユーザーに提示してから呼ぶこと。
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
    """
    return await dc.get(f"api/scrapping/projects/{project_id}/episodes/{episode_number}/aroll/status")


# ── レジストリ（server.py / 後継ループ が参照する単一の出所） ──────────

S = SideEffect
TOOLS = [
    {"fn": check_openrouter_credits, "side_effects": [S.READ]},
    {"fn": check_vecteezy_quota, "side_effects": [S.READ]},
    {"fn": list_projects,        "side_effects": [S.READ]},
    {"fn": project_status,       "side_effects": [S.READ]},
    {"fn": list_styles,          "side_effects": [S.READ]},
    {"fn": create_style,         "side_effects": [S.WRITE]},
    {"fn": update_style,         "side_effects": [S.WRITE]},
    {"fn": delete_style,         "side_effects": [S.WRITE]},
    {"fn": create_project,       "side_effects": [S.WRITE]},
    {"fn": generate_script,      "side_effects": [S.WRITE]},
    {"fn": generate_series_script, "side_effects": [S.WRITE]},
    {"fn": approve_script,       "side_effects": [S.WRITE]},
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
    # 自由生成（台本非依存）
    {"fn": list_imagegen_styles, "side_effects": [S.READ]},
    {"fn": free_generate,        "side_effects": [S.COST, S.GPU]},
    {"fn": free_audio,           "side_effects": [S.COST]},
    {"fn": free_save,            "side_effects": [S.WRITE]},
    # リサーチ（探索→蒸留→ラフ台本）
    {"fn": research_list_sources, "side_effects": [S.READ]},
    {"fn": research_search,      "side_effects": [S.COST]},
    {"fn": research_add_source,  "side_effects": [S.WRITE]},
    {"fn": research_digest,      "side_effects": [S.WRITE]},
    {"fn": research_get_digest,  "side_effects": [S.READ]},
]
