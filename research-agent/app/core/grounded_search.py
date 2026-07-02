"""
Gemini の Google検索グラウンディングで関連記事/言及を補助収集する。
引用URLを確実に取るため google-genai ネイティブSDKを使う（LiteLLM経由のグラウンディングは不安定）。
"""
import logging

from app.core.llm_client import gemini_api_key

logger = logging.getLogger(__name__)

GROUNDING_MODEL = "gemini-2.5-flash"  # 検索＝速い・安いflashで十分


async def search(query: str, max_results: int = 6) -> list[dict]:
    """
    (results, summary) を返す。results=[{title, url, snippet}]。
    鍵未設定や失敗時は ([], "")（補助機能なので落とさない）。
    """
    key = gemini_api_key()
    if not key:
        return [], ""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.warning("google-genai not installed; grounded search disabled")
        return [], ""

    client = genai.Client(api_key=key)
    prompt = (
        f"次のトピックについて、動画台本の参考になる信頼できる関連記事・一次情報をWeb検索で探してください。"
        f"単一の観点に絞らず、複数の異なる側面・論点をそれぞれ根拠つきで述べてください。日本語で。"
        f"\n\nトピック: {query}"
    )
    try:
        resp = await client.aio.models.generate_content(
            model=GROUNDING_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2,
            ),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("grounded search failed: %s", str(e)[:200])
        return [], ""

    summary = getattr(resp, "text", "") or ""

    # grounding_chunks は URL/タイトルのみで本文を持たない。grounding_supports
    # （回答内の各セグメントがどの chunk を根拠にしたか）と突き合わせて、
    # 出典ごとに「実際に引用された文」を snippet として復元する。
    chunk_urls: dict[int, tuple[str, str]] = {}
    snippets_by_url: dict[str, list[str]] = {}
    try:
        for cand in resp.candidates or []:
            meta = getattr(cand, "grounding_metadata", None)
            for i, chunk in enumerate(getattr(meta, "grounding_chunks", None) or []):
                web = getattr(chunk, "web", None)
                url = getattr(web, "uri", None) if web else None
                if url:
                    chunk_urls[i] = (getattr(web, "title", None) or url, url)

            for support in (getattr(meta, "grounding_supports", None) or []):
                seg_text = (getattr(getattr(support, "segment", None), "text", None) or "").strip()
                if not seg_text:
                    continue
                for idx in (getattr(support, "grounding_chunk_indices", None) or []):
                    entry = chunk_urls.get(idx)
                    if not entry:
                        continue
                    bucket = snippets_by_url.setdefault(entry[1], [])
                    if seg_text not in bucket:
                        bucket.append(seg_text)
    except Exception as e:  # noqa: BLE001
        logger.warning("grounding metadata parse failed: %s", str(e)[:200])

    results: list[dict] = []
    seen: set[str] = set()
    for title, url in chunk_urls.values():
        if url in seen:
            continue
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": " / ".join(snippets_by_url.get(url, []))})
        if len(results) >= max_results:
            break

    return results, summary
