from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()

from app.api.routes import router

app = FastAPI(
    title="scrapping-agent",
    version="0.1.0",
    description="映像素材の収集・管理コンテナ（Pexels検索・footage.json生成）",
)

app.include_router(router)

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
