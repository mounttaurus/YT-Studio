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

class RevalidatingStaticFiles(StaticFiles):
    """静的ファイルを毎回再検証させる（ETag で変わっていなければ 304＝軽い）。

    ⚠️ 付けないと、更新日時が古いままの ES モジュール（例: workbench/js/api.js）だけが
    ブラウザの推測キャッシュに残り、新しい app.js と食い違って画面が壊れる
    （2026-10-03: 新しい aroll_ui.js が古い api.js の `api.dup` を読んで失敗した）。
    """

    def file_response(self, *args, **kwargs):
        res = super().file_response(*args, **kwargs)
        res.headers["Cache-Control"] = "no-cache"
        return res


static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", RevalidatingStaticFiles(directory=str(static_dir), html=True), name="static")
