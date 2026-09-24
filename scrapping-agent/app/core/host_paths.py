"""コンテナ内パス（SHARED_DIR配下）をホスト側Windowsパスに変換する（表示専用）。

editing-agent/app/core/path_mapper.py の to_host_path() と同じ仕組み。
Docs/PANEL_LIBRARY_FILE_PATH_PLAN.md。

⚠️ 索引(library.json)には保存しない。ホストのshared位置は環境依存（YT-Studio開発機と
LUKAandAOI本番機でルートが違う）なので、書き込むと環境間で持ち運べなくなる。
表示のたびにこのモジュールで計算する（is_stale と同じ「読むたびに正規化する」方針）。
"""
import os
from pathlib import Path, PureWindowsPath

HOST_SHARED_DIR = os.getenv("HOST_SHARED_DIR", "")
SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))


def to_host_path(container_path: Path) -> str | None:
    """コンテナ内絶対パス(SHARED_DIR配下)をホスト側Windowsパス文字列に変換する。

    HOST_SHARED_DIR未設定、またはcontainer_pathがSHARED_DIR配下でない場合はNone
    （呼び出し側はUIで「未設定」と表示する。install.ps1を通さない環境でも他機能は動く）。
    """
    if not HOST_SHARED_DIR:
        return None
    try:
        rel = container_path.resolve().relative_to(SHARED_DIR.resolve())
    except ValueError:
        return None
    result = PureWindowsPath(HOST_SHARED_DIR)
    for part in rel.parts:
        result = result / part
    return str(result)
