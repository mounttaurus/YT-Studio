"""motion-agent の固定値。コンテナ内のパス・URL は compose の environment 側で与える（.env に書かない）。"""
import os
from pathlib import Path

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
REMOTION_DIR = Path(os.getenv("REMOTION_DIR", "/app/remotion"))
TEMPLATES_DIR = REMOTION_DIR / "src" / "templates"
WORK_DIR = SHARED_DIR / "motion" / "_work"
# ユーザーの書体（購入したものなど）の置き場。fontconfig の検索先にも入れてある（Dockerfile）
USER_FONTS_DIR = SHARED_DIR / "motion" / "fonts"

# 描画中の Chrome が素材を取りに来る自分自身の URL（同じコンテナ内）
SELF_URL = os.getenv("MOTION_SELF_URL", "http://127.0.0.1:8007")

# M0 の実測で並列4が最速（Docs/OPENING_MOTION_PLAN.md §13）
RENDER_CONCURRENCY = int(os.getenv("MOTION_RENDER_CONCURRENCY", "4"))
RENDER_TIMEOUT_SEC = int(os.getenv("MOTION_RENDER_TIMEOUT_SEC", "900"))
STILL_SCALE = float(os.getenv("MOTION_STILL_SCALE", "0.5"))
NODE_BIN = os.getenv("NODE_BIN", "node")

# HyperFrames（新しい組み立て方・Docs/MOTION_UNIFIED_PLAN.md）。CLI はコンテナの中だけで動かす
HF_DIR = Path(os.getenv("HF_DIR", "/app/hyperframes"))
HF_SAMPLES_DIR = HF_DIR / "samples"
HF_BIN = HF_DIR / "node_modules" / ".bin" / "hyperframes"
HF_CHECK_TIMEOUT_SEC = int(os.getenv("MOTION_HF_CHECK_TIMEOUT_SEC", "180"))
HF_PROFILES_DIR = HF_DIR / "profiles"
HF_MEASURE_TIMEOUT_SEC = int(os.getenv("MOTION_HF_MEASURE_TIMEOUT_SEC", "120"))

# Bロールの箱（scrapping-agent）。描いたインサートの受け渡し先（Docs/BROLL_PLAN.md §8-2）。書き手は scrapping-agent だけ（M4）
SCRAPPING_AGENT_URL = os.getenv("SCRAPPING_AGENT_URL", "http://scrapping-agent:8003")
BROLL_REGISTER_TIMEOUT_SEC = int(os.getenv("MOTION_BROLL_REGISTER_TIMEOUT_SEC", "120"))
