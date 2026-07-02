"""
ソース取り込み：アップロードファイル（PDF/docx/txt/json/音声）と URL本文の抽出。
音声/動画は Cloudflare Whisper で書き起こす（Geminiの無料枠を消費しない）。
"""
import io
import json

import httpx
from bs4 import BeautifulSoup

from app.core import cloudflare_text

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm", ".mp4", ".mpga", ".mpeg")


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


def _extract_docx(data: bytes) -> str:
    import docx
    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs).strip()


def _extract_json(data: bytes) -> str:
    obj = json.loads(data.decode("utf-8", errors="replace"))
    return json.dumps(obj, ensure_ascii=False, indent=2)


async def extract_file(filename: str, data: bytes) -> tuple[str, str]:
    """(kind, text) を返す。音声系は Whisper で書き起こす。"""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return "document", _extract_pdf(data)
    if name.endswith(".docx"):
        return "document", _extract_docx(data)
    if name.endswith(".json"):
        return "document", _extract_json(data)
    if name.endswith(AUDIO_EXTS):
        return "transcript", await cloudflare_text.transcribe(data)
    # txt その他はそのままテキストとして扱う
    return "document", data.decode("utf-8", errors="replace").strip()


async def fetch_url(url: str) -> tuple[str, str]:
    """(title, text) を返す。HTMLは本文テキストを抽出。"""
    headers = {"User-Agent": "Mozilla/5.0 (research-agent)"}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=headers) as client:
        res = await client.get(url)
        res.raise_for_status()
        ctype = res.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            # PDF等は extract_file へ回す
            kind_text = await extract_file(url.split("?")[0], res.content)
            return url, kind_text[1]
        soup = BeautifulSoup(res.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        title = (soup.title.string.strip() if soup.title and soup.title.string else url)
        text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
        return title, text.strip()
