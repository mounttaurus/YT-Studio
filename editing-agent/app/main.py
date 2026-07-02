from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()

from app.api.routes import router
from app.mcp.server import mcp_router

app = FastAPI(
    title="editing-agent",
    version="0.1.0",
    description="台本・TTS音声・素材を統合し、DaVinci Resolve用のラフ編集データ（OTIO+SRT）を生成するコンテナ",
)

app.include_router(router)
app.include_router(mcp_router, prefix="/mcp")

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
