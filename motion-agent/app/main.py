"""motion-agent — オープニング（モーショングラフィック）の制作。設計: Docs/OPENING_MOTION_PLAN.md"""
from fastapi import FastAPI

from app.api.routes import router

app = FastAPI(title="motion-agent", version="0.1.0")
app.include_router(router)
