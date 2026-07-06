"""
YouTube SEOオプティマイザのコア。

ラフ台本からジャンル/シードキーワードをLLMで推定し、YouTube Data API v3 で
市場データ（タグ・競合・下克上動画・視聴者コメント）を収穫、台本の手持ちキーワードとの
差分を分析して `seo_pack.json` を作る。さらに確定台本から概要欄/タイトル/タグの
`publish_pack.json` を作る。

収穫はクォータ予算に守られており、途中で尽きても部分結果（partial=true）を返す。
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core import llm_client, project_manager, youtube_client
from app.core.youtube_client import QuotaBudgetExceeded

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"

GENRE_SYSTEM = (
    "あなたはYouTube動画のSEOリサーチャーです。与えられたラフ台本を読み、"
    "この動画がどのジャンル・視聴者層に向けたものかを分析し、YouTube検索で"
    "市場調査するための検索クエリと、台本が既に持っているキーワードを抽出します。"
    "出力は指定のJSONのみ、前置き・コメント禁止。"
)

INSIGHTS_SYSTEM = (
    "あなたはYouTubeコメント欄の分析官です。与えられたコメント群から、"
    "視聴者の反応を『称賛』『不満』『疑問』に分類して要約します。"
    "出力は指定のJSONのみ、前置き・コメント禁止。"
)

GAP_SYSTEM = (
    "あなたはYouTube動画のSEO戦略家です。台本が既に持つキーワードと、市場で実際に"
    "使われているタグ・伸びている動画のタイトルパターンを比較し、台本に足りていない"
    "要素と、台本執筆に注入すべき指示文を作ります。出力は指定のJSONのみ、前置き・コメント禁止。"
)

PUBLISH_SYSTEM = (
    "あなたはYouTube動画の公開メタデータ担当です。確定台本とSEO調査結果から、"
    "クリックされやすくSEOに強いタイトル案・概要欄・ハッシュタグ・タグを作ります。"
    "出力は指定のJSONのみ、前置き・コメント禁止。"
)

CURATE_SYSTEM = (
    "あなたはSEOと物語構成の両方を理解する編集者です。検索性より台本の面白さを優先し、"
    "この台本に無理なく馴染むキーワードだけを厳選します。詰め込みすぎて話が不自然になることを"
    "最も嫌います。出力は指定のJSONのみ、前置き・コメント禁止。"
)


# ─── JSON抽出（digest_builder と同じ頑健パース） ──────────────────────────

def _strip_json(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a != -1 and b != -1 and b > a:
        return raw[a:b + 1]
    return raw


def _parse_json(raw, fallback: dict) -> dict:
    if isinstance(raw, dict):  # 上流が既にパース済みJSONを返した場合
        return raw
    try:
        return json.loads(_strip_json(raw))
    except json.JSONDecodeError:
        logger.warning("LLM応答のJSON解析に失敗。フォールバック値を使用: %s", raw[:200])
        return fallback


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── 集計（純関数：テスト可能） ────────────────────────────────────────

def aggregate_tags(video_items: list[dict]) -> list[dict]:
    """動画群のsnippet.tagsを正規化して頻度集計。上位40件。
    正規化: 前後空白除去、英字のみ小文字化（日本語はそのまま）。
    """
    counts: dict[str, int] = {}
    for item in video_items:
        tags = (item.get("snippet") or {}).get("tags") or []
        for tag in tags:
            norm = tag.strip()
            if not norm:
                continue
            if norm.isascii():
                norm = norm.lower()
            counts[norm] = counts.get(norm, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [{"tag": tag, "count": count} for tag, count in ranked[:40]]


def rank_channels(video_items: list[dict], channel_items: list[dict]) -> list[dict]:
    """複数クエリに跨がって出現するチャンネル順（真の競合）。上位15件。"""
    channel_meta = {c["id"]: c for c in channel_items}
    appearances: dict[str, int] = {}
    for item in video_items:
        cid = (item.get("snippet") or {}).get("channelId")
        if not cid:
            continue
        appearances[cid] = appearances.get(cid, 0) + 1

    ranked = sorted(appearances.items(), key=lambda kv: kv[1], reverse=True)
    out = []
    for cid, count in ranked[:15]:
        meta = channel_meta.get(cid, {})
        snippet = meta.get("snippet") or {}
        stats = meta.get("statistics") or {}
        out.append({
            "channel_id": cid,
            "title": snippet.get("title", ""),
            "subscribers": int(stats.get("subscriberCount", 0)) if not stats.get("hiddenSubscriberCount") else None,
            "appearances": count,
        })
    return out


def find_upsets(video_items: list[dict], channel_items: list[dict]) -> list[dict]:
    """打率(views ÷ max(subscribers,1))が2.0以上の下克上動画。上位10件。"""
    channel_meta = {c["id"]: c for c in channel_items}
    upsets = []
    for item in video_items:
        vid = item.get("id")
        snippet = item.get("snippet") or {}
        stats = item.get("statistics") or {}
        views = int(stats.get("viewCount", 0))
        cid = snippet.get("channelId")
        meta = channel_meta.get(cid, {})
        cstats = meta.get("statistics") or {}
        subs = int(cstats.get("subscriberCount", 0)) if cstats.get("subscriberCount") is not None else 0
        ratio = views / max(subs, 1)
        if ratio >= 2.0:
            upsets.append({
                "video_id": vid,
                "title": snippet.get("title", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "views": views,
                "subscribers": subs,
                "ratio": round(ratio, 2),
            })
    upsets.sort(key=lambda u: u["ratio"], reverse=True)
    return upsets[:10]


# ─── ジャンル推定（LLM） ───────────────────────────────────────────────

async def _estimate_genre(rough_script: str, research: Optional[dict], model: Optional[str]) -> tuple[dict, bool]:
    context = ""
    if research and research.get("overview"):
        context = f"\n\n## 補助文脈（リサーチ概要）\n{research['overview']}"

    prompt = f"""以下のラフ台本を分析し、YouTube市場調査のための情報を抽出してください。

## ラフ台本
{rough_script}
{context}

## 出力フォーマット（このJSONだけを出力）
{{
  "genre": "この動画のジャンル（例: 都市伝説, ガジェットレビュー, 歴史解説）",
  "audience": "想定視聴者層の簡潔な説明",
  "competitor_archetypes": ["競合しそうなチャンネルタイプを2〜4個"],
  "seed_queries": ["YouTube検索用の日本語クエリを3〜6個（実際に視聴者が打ちそうな語）"],
  "own_keywords": ["この台本が既に持っているキーワード・固有名詞を10個前後"]
}}
"""
    raw, fallback_used = await llm_client.chat(
        prompt, model=model or llm_client.FLASH_MODEL, system=GENRE_SYSTEM, temperature=0.3, max_tokens=2048,
    )
    parsed = _parse_json(raw, {
        "genre": "", "audience": "", "competitor_archetypes": [],
        "seed_queries": [], "own_keywords": [],
    })
    return parsed, fallback_used


# ─── 収穫 ──────────────────────────────────────────────────────────────

async def _harvest(seed_queries: list[str]) -> tuple[list[dict], list[dict], bool, Optional[str]]:
    """各シードクエリで検索→動画詳細→チャンネル詳細を集める。
    戻り: (video_items, channel_items, partial, partial_reason)
    """
    video_ids: list[str] = []
    seen_video_ids: set[str] = set()
    partial = False
    partial_reason = None

    for query in seed_queries:
        try:
            data = await youtube_client.search(query, max_results=25)
        except QuotaBudgetExceeded as e:
            partial = True
            partial_reason = str(e)
            break
        for item in data.get("items", []):
            vid = (item.get("id") or {}).get("videoId")
            if vid and vid not in seen_video_ids:
                seen_video_ids.add(vid)
                video_ids.append(vid)

    video_items: list[dict] = []
    if video_ids:
        try:
            video_items = await youtube_client.videos(video_ids)
        except QuotaBudgetExceeded as e:
            partial = True
            partial_reason = partial_reason or str(e)

    channel_ids = list({(v.get("snippet") or {}).get("channelId") for v in video_items if (v.get("snippet") or {}).get("channelId")})
    channel_items: list[dict] = []
    if channel_ids:
        try:
            channel_items = await youtube_client.channels(channel_ids)
        except QuotaBudgetExceeded as e:
            partial = True
            partial_reason = partial_reason or str(e)

    return video_items, channel_items, partial, partial_reason


async def _viewer_insights(upset_videos: list[dict], model: Optional[str]) -> tuple[dict, bool, bool, Optional[str]]:
    """打率上位3動画のコメントを要約。戻り: (insights, fallback_used, partial, partial_reason)"""
    partial = False
    partial_reason = None
    all_comments: list[str] = []
    for video in upset_videos[:3]:
        try:
            threads = await youtube_client.comment_threads(video["video_id"], max_results=100)
        except QuotaBudgetExceeded as e:
            partial = True
            partial_reason = str(e)
            break
        for t in threads:
            text = (
                ((t.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
            ).get("textDisplay", "")
            if text:
                all_comments.append(text)

    if not all_comments:
        return {"praise": [], "complaints": [], "questions": []}, False, partial, partial_reason

    joined = "\n".join(f"- {c}" for c in all_comments[:300])
    prompt = f"""以下は競合動画のコメント欄からの抜粋です。視聴者の反応を分類してください。

## コメント
{joined}

## 出力フォーマット（このJSONだけを出力）
{{
  "praise": ["称賛されている点"],
  "complaints": ["不満・批判されている点"],
  "questions": ["視聴者が疑問に思っている点"]
}}
"""
    raw, fallback_used = await llm_client.chat(
        prompt, model=model or llm_client.FLASH_MODEL, system=INSIGHTS_SYSTEM, temperature=0.3, max_tokens=2048,
    )
    parsed = _parse_json(raw, {"praise": [], "complaints": [], "questions": []})
    return parsed, fallback_used, partial, partial_reason


async def _gap_analysis(
    own_keywords: list[str], tags: list[dict], upset_videos: list[dict], model: Optional[str],
) -> tuple[dict, bool]:
    tag_list = ", ".join(t["tag"] for t in tags[:40])
    title_list = "\n".join(f"- {v['title']}" for v in upset_videos[:10])

    prompt = f"""台本の手持ちキーワードと市場データを比較し、差分を分析してください。

## 台本の手持ちキーワード
{", ".join(own_keywords)}

## 市場で使われているタグ（上位）
{tag_list or "（データなし）"}

## 下克上している動画のタイトル
{title_list or "（データなし）"}

## 出力フォーマット（このJSONだけを出力）
{{
  "missing_tags": ["台本に足りていない市場タグ"],
  "multiplier_ideas": ["視聴を伸ばすための企画・切り口のアイデア"],
  "title_patterns": ["伸びているタイトルの型・パターン"],
  "for_script": "台本生成プロンプトに注入する500〜800字のコンパクトな指示ブロック（自然文または箇条書き）"
}}
"""
    raw, fallback_used = await llm_client.chat(
        prompt, model=model or llm_client.FLASH_MODEL, system=GAP_SYSTEM, temperature=0.4, max_tokens=2048,
    )
    parsed = _parse_json(raw, {
        "missing_tags": [], "multiplier_ideas": [], "title_patterns": [], "for_script": "",
    })
    return parsed, fallback_used


# ─── SEOキュレーション層（seo_pack全体からscript_briefを厳選） ────────────

async def _curate_brief(source_text: str, pack: dict, model: Optional[str] = None) -> tuple[dict, bool]:
    """seo_pack（パレット＝全分析結果）から、この台本に自然に馴染むキーワードだけを
    3〜6個に厳選する。台本本文に直接注入するのはこの script_brief のみとし、
    パレット全体（harvest.tagsなど）は表示・参照用に留める。
    最優先は検索性ではなくシナリオの面白さ＝無理に馴染まない語は捨てる。
    """
    tags = (pack.get("harvest") or {}).get("tags") or []
    tag_list = ", ".join(t["tag"] for t in tags[:20])
    own_keywords = (pack.get("genre_frame") or {}).get("own_keywords") or []

    prompt = f"""以下の台本と、市場調査で得られたキーワード候補を見比べて、この台本に自然に馴染むキーワードだけを3〜6個選んでください。

## 台本（抜粋）
{source_text[:3000]}

## 市場タグ（上位、参考）
{tag_list or "（データなし）"}

## 台本の手持ちキーワード（参考）
{", ".join(own_keywords)}

## 選定方針
- 最優先はシナリオの面白さ。無理に入れると話が不自然になる語は捨てること。
- この台本の内容・トーンに合わないキーワードは選ばないこと。
- 3〜6個に絞ること（多すぎても不自然になる）。

## 出力フォーマット（このJSONだけを出力）
{{
  "keywords": ["この台本に自然に馴染むキーワードを3〜6個"]
}}
"""
    raw, fallback_used = await llm_client.chat(
        prompt, model=model or llm_client.FLASH_MODEL, system=CURATE_SYSTEM, temperature=0.3, max_tokens=1024,
    )
    parsed = _parse_json(raw, {"keywords": []})

    # 空文字/重複を除去し最大6個に切り詰め
    seen: set[str] = set()
    keywords: list[str] = []
    for kw in parsed.get("keywords") or []:
        kw = str(kw).strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        keywords.append(kw)
        if len(keywords) >= 6:
            break

    brief = {"keywords": keywords, "curated_by": "ai", "curated_at": _now()}
    return brief, fallback_used


# ─── 既存台本フォールバック（ラフ台本が無いプロジェクト向け） ──────────────

def _derive_source_from_scripts(project_id: str) -> Optional[str]:
    """ラフ台本が無い場合の代替ソース。episodes/ep{NN}/ 配下の確定台本
    （script.json、無ければscript_draft.json）を番号順に読み、本文を連結して返す。
    1件も無ければNone。
    """
    episodes_dir = project_manager.get_project_dir(project_id) / "episodes"
    if not episodes_dir.exists():
        return None

    summaries: list[str] = []
    for ep_dir in sorted(episodes_dir.glob("ep*")):
        if not ep_dir.is_dir():
            continue
        script = None
        for name in ("script.json", "script_draft.json"):
            f = ep_dir / name
            if f.exists():
                try:
                    script = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                break
        if script is not None:
            summary = _script_text_summary(script)
            if summary:
                summaries.append(summary)

    if not summaries:
        return None
    return "\n\n".join(summaries)[:8000]


# ─── メインパイプライン ────────────────────────────────────────────────

async def optimize(project_id: str, force: bool = False, model: Optional[str] = None) -> dict:
    """ラフ台本→SEOパック生成のメインパイプライン。
    既存パックがあり source_hash が一致すれば（force=False時）再生成せず既存を返す。
    """
    # ラフ台本または既存台本本文（rough_scriptが無ければ確定台本から代替ソースを組み立てる）
    rough_script = project_manager.read_rough_script(project_id)
    if rough_script is None:
        rough_script = _derive_source_from_scripts(project_id)
    if rough_script is None:
        raise ValueError("ラフ台本も台本もありません。先にリサーチ/台本生成を行ってください。")
    research = project_manager.read_research(project_id)

    source_hash = _sha256(rough_script)

    existing = read_seo_pack(project_id)
    if not force and existing and existing.get("source_hash") == source_hash:
        return {**existing, "cached": True}

    fallback_used = False
    partial = False
    partial_reason = None

    genre_frame, fb = await _estimate_genre(rough_script, research, model)
    fallback_used = fallback_used or fb

    seed_queries = genre_frame.get("seed_queries") or []
    video_items, channel_items, harvest_partial, harvest_reason = await _harvest(seed_queries)
    partial = partial or harvest_partial
    partial_reason = partial_reason or harvest_reason

    tags = aggregate_tags(video_items)
    channels_ranked = rank_channels(video_items, channel_items)
    upset_videos = find_upsets(video_items, channel_items)

    insights, fb, insights_partial, insights_reason = await _viewer_insights(upset_videos, model)
    fallback_used = fallback_used or fb
    partial = partial or insights_partial
    partial_reason = partial_reason or insights_reason

    gap, fb = await _gap_analysis(genre_frame.get("own_keywords") or [], tags, upset_videos, model)
    fallback_used = fallback_used or fb

    pack = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "generated_at": _now(),
        "source_hash": source_hash,
        "engine": {"model": model or llm_client.FLASH_MODEL, "fallback_used": fallback_used},
        "genre_frame": genre_frame,
        "harvest": {
            "tags": tags,
            "channels": channels_ranked,
            "upset_videos": upset_videos,
            "videos_analyzed": len(video_items),
        },
        "viewer_insights": insights,
        "gap_analysis": {
            "missing_tags": gap.get("missing_tags", []),
            "multiplier_ideas": gap.get("multiplier_ideas", []),
            "title_patterns": gap.get("title_patterns", []),
        },
        "for_script": gap.get("for_script", ""),
        "partial": partial,
        "partial_reason": partial_reason,
    }

    # seo_pack（パレット）から台本注入用の厳選キーワード（script_brief）を自動選抜
    script_brief, fb = await _curate_brief(rough_script, pack, model)
    fallback_used = fallback_used or fb
    pack["engine"]["fallback_used"] = fallback_used
    pack["script_brief"] = script_brief

    _save_seo_pack(project_id, pack)
    return pack


def _seo_pack_path(project_id: str) -> Path:
    return project_manager.get_project_dir(project_id) / "seo_pack.json"


def _save_seo_pack(project_id: str, pack: dict) -> None:
    f = _seo_pack_path(project_id)
    f.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")


def read_seo_pack(project_id: str) -> Optional[dict]:
    f = _seo_pack_path(project_id)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


# ─── キュレーション層のAPI向けヘルパー（routes.pyから薄く呼ばれる） ────────

async def recurate(project_id: str, model: Optional[str] = None) -> dict:
    """既存seo_packを読み、AIによるscript_brief再選定のみをやり直す
    （harvest等の市場データ収穫はやり直さない＝クォータ消費なし）。
    """
    pack = read_seo_pack(project_id)
    if pack is None:
        raise ValueError("seo_pack.json がまだありません。先にSEO分析を実行してください。")

    # ラフ台本または既存台本本文（optimize()と同じ解決順）
    source_text = project_manager.read_rough_script(project_id)
    if source_text is None:
        source_text = _derive_source_from_scripts(project_id)
    if source_text is None:
        raise ValueError("ラフ台本も台本もありません。先にリサーチ/台本生成を行ってください。")

    script_brief, fb = await _curate_brief(source_text, pack, model)
    pack["engine"]["fallback_used"] = pack.get("engine", {}).get("fallback_used", False) or fb
    pack["script_brief"] = script_brief

    _save_seo_pack(project_id, pack)
    return script_brief


def set_brief(project_id: str, keywords: list[str]) -> dict:
    """ユーザーによる手動編集でscript_briefを置き換える（最大10個・空/重複除去）。"""
    pack = read_seo_pack(project_id)
    if pack is None:
        raise ValueError("seo_pack.json がまだありません。先にSEO分析を実行してください。")

    seen: set[str] = set()
    cleaned: list[str] = []
    for kw in keywords:
        kw = str(kw).strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        cleaned.append(kw)
        if len(cleaned) >= 10:
            break

    script_brief = {"keywords": cleaned, "curated_by": "user", "curated_at": _now()}
    pack["script_brief"] = script_brief

    _save_seo_pack(project_id, pack)
    return script_brief


# ─── 公開パック（タイトル/概要欄/タグ） ────────────────────────────────

def _episode_dir(project_id: str, episode_number: int) -> Path:
    return project_manager.get_project_dir(project_id) / "episodes" / f"ep{episode_number:02d}"


def _read_script(project_id: str, episode_number: int) -> dict:
    ep_dir = _episode_dir(project_id, episode_number)
    for name in ("script.json", "script_draft.json"):
        f = ep_dir / name
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    raise ValueError(f"台本が見つかりません（{ep_dir} に script.json / script_draft.json なし）")


def _publish_pack_path(project_id: str, episode_number: int) -> Path:
    return _episode_dir(project_id, episode_number) / "publish_pack.json"


def read_publish_pack(project_id: str, episode_number: int) -> Optional[dict]:
    f = _publish_pack_path(project_id, episode_number)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _script_text_summary(script: dict) -> str:
    """script.json/script_draft.jsonからLLMに渡す簡潔な本文抜粋を作る。"""
    lines = script.get("lines") or script.get("script_lines") or []
    parts = []
    for line in lines:
        if isinstance(line, dict):
            text = line.get("text") or line.get("line") or ""
        else:
            text = str(line)
        if text:
            parts.append(text)
    return "\n".join(parts)[:6000]


async def build_publish_pack(
    project_id: str, episode_number: int, model: Optional[str] = None,
) -> dict:
    """確定台本＋seo_packから概要欄/タイトル/ハッシュタグ/タグを生成する。"""
    script = _read_script(project_id, episode_number)
    seo_pack = read_seo_pack(project_id)

    script_summary = _script_text_summary(script)
    seo_context = ""
    if seo_pack:
        seo_context = f"""

## SEO調査結果
- 台本執筆向け指示: {seo_pack.get('for_script', '')}
- 市場タグ上位: {", ".join(t['tag'] for t in (seo_pack.get('harvest', {}).get('tags') or [])[:20])}
- 伸びているタイトルパターン: {", ".join(seo_pack.get('gap_analysis', {}).get('title_patterns', []))}
"""

    prompt = f"""以下の確定台本から、YouTube公開用のメタデータを作ってください。

## 台本本文（抜粋）
{script_summary}
{seo_context}

## 出力フォーマット（このJSONだけを出力）
{{
  "titles": ["タイトル案を3個"],
  "description": "概要欄全文（冒頭2行に主要キーワードを含め、末尾にハッシュタグを3個含める）",
  "hashtags": ["#付きハッシュタグを10〜15個"],
  "tags": ["検索タグを列挙（結合して500字以内に収まる想定で多めに）"]
}}
"""
    raw, fallback_used = await llm_client.chat(
        prompt, model=model or llm_client.DEFAULT_MODEL, system=PUBLISH_SYSTEM, temperature=0.5, max_tokens=4096,
    )
    parsed = _parse_json(raw, {"titles": [], "description": "", "hashtags": [], "tags": []})

    tags = parsed.get("tags") or []
    trimmed_tags: list[str] = []
    total_len = 0
    for tag in tags:
        added_len = len(tag) + (1 if trimmed_tags else 0)  # 結合区切り分を概算
        if total_len + added_len > 500:
            break
        trimmed_tags.append(tag)
        total_len += added_len

    pack = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "episode_number": episode_number,
        "generated_at": _now(),
        "titles": parsed.get("titles", []),
        "description": parsed.get("description", ""),
        "hashtags": parsed.get("hashtags", []),
        "tags": trimmed_tags,
    }

    f = _publish_pack_path(project_id, episode_number)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")

    return pack
