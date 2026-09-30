from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()

from app.api.lines import router as lines_router
from app.api.routes import router
from app.api.workbench import router as workbench_router

app = FastAPI(
    title="director-agent",
    version="0.1.0",
    description="各エージェントへの命令を司るディレクター・コンテナ",
)

app.include_router(router)
app.include_router(lines_router)   # 行の操作の窓口（Docs/LINE_WORKBENCH_PLAN.md §3）
app.include_router(workbench_router)   # ワークベンチの状態の一括取得（§5・W3）

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
