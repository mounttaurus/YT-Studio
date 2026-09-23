import asyncio
import traceback
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()

from app.api.routes import router
from app.core import character_manager, panel_library_manager

app = FastAPI(
    title="scrapping-agent",
    version="0.1.0",
    description="映像素材の収集・管理コンテナ（Pexels検索・footage.json生成）",
)

app.include_router(router)

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

PS_ADOPT_INTERVAL_SEC = 15  # Docs/CUTOUT_PS_PRIMARY_PLAN.md P2 §4


async def _ps_cutout_adopt_loop() -> None:
    """host_workerが cutouts_ps/ へ置いたPS切り抜きを、全キャラ分・定期的に取り込む。

    ⚠️ library.json を書けるのはこのコンテナだけ（§1-2）。host側はPNGを置くだけ。
    1キャラの失敗が他キャラを止めないよう、char_idごとに例外を握りつぶす。
    """
    while True:
        try:
            for c in character_manager.list_characters():
                char_id = c.get("char_id")
                if not char_id:
                    continue
                try:
                    result = panel_library_manager.adopt_ps_cutouts(char_id)
                    if result["adopted"] or result["errors"] or result["orphaned"]:
                        print("[ps-cutout] %s adopted=%d errors=%d orphaned=%d" % (
                            char_id, len(result["adopted"]), len(result["errors"]),
                            len(result["orphaned"])), flush=True)
                except Exception:
                    print("[ps-cutout] %s の取り込みに失敗" % char_id, flush=True)
                    traceback.print_exc()
        except Exception:
            print("[ps-cutout] キャラ一覧の取得に失敗", flush=True)
            traceback.print_exc()
        await asyncio.sleep(PS_ADOPT_INTERVAL_SEC)


@app.on_event("startup")
async def _start_ps_cutout_adopt_loop() -> None:
    asyncio.create_task(_ps_cutout_adopt_loop())
