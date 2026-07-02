import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()

from app.api.routes import router

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="research-agent",
    version="1.0.0",
    description="持ち込み素材（ドキュメント/テキスト/URL）→ラフ台本ダイジェスト（Gemini主・Cloudflare協調）",
)

app.include_router(router)

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
