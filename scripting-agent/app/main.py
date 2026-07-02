import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()

from app.api.routes import router
from app.mcp.server import mcp_router
from app.core.project_manager import migrate_all_projects

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時: 旧構造プロジェクトを episodes/ 構造へ自動マイグレーション
    try:
        result = migrate_all_projects()
        logger.info(
            "Migration complete: total=%d migrated=%d already_migrated=%d skipped=%d",
            result["total"], result["migrated"], result["already_migrated"], result["skipped"],
        )
    except Exception as e:
        logger.error("Migration failed: %s", e, exc_info=True)
    yield


app = FastAPI(
    title="scripting-agent",
    version="2.0.0",
    description="台本生成・リライトエージェント（episodes/構造対応）",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(mcp_router, prefix="/mcp")

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
