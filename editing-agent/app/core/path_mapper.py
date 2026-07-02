"""
コンテナ内パス（SHARED_DIR配下）とホスト側パス（DaVinci Resolveが解釈するパス）の変換、
および tts.json / footage.json の file_path 基準ディレクトリの差異を吸収するヘルパー。

DATA_SCHEMA.md 1章「file_path の基準ディレクトリの差異」参照:
- tts.json の file_path はプロジェクトルート相対
- footage.json の file_path はエピソードディレクトリ相対
両方を試して実在するパスを採用する。
"""
import os
from pathlib import Path, PureWindowsPath

HOST_SHARED_DIR = os.getenv("HOST_SHARED_DIR", "")
SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))


def resolve_media_path(raw: str, project_dir: Path, episode_dir: Path) -> Path | None:
    """file_pathを実ファイルが存在するコンテナ内絶対パスに解決する。

    1) episode_dir / raw が存在すればそれ（footage.json基準）
    2) project_dir / raw が存在すればそれ（tts.json基準）
    3) どちらも無ければ None
    """
    candidate = episode_dir / raw
    if candidate.exists():
        return candidate
    candidate = project_dir / raw
    if candidate.exists():
        return candidate
    return None


def to_host_path(container_path: Path) -> PureWindowsPath:
    """コンテナ内絶対パス（SHARED_DIR配下）をホスト側Windowsパスに変換する。

    例: /shared/projects/foo/episodes/ep01/audio/line_001.wav
        -> PureWindowsPath("D:/Docker/Youtube-Auto/shared/projects/foo/episodes/ep01/audio/line_001.wav")
    """
    rel = container_path.resolve().relative_to(SHARED_DIR.resolve())
    host_root = PureWindowsPath(HOST_SHARED_DIR)
    result = host_root
    for part in rel.parts:
        result = result / part
    return result


def to_target_url(container_path: Path, path_style: str = "file_uri") -> str:
    """OTIOのExternalReference.target_urlに書き込む文字列を生成する。

    - file_uri: file:///D:/... 形式（日本語はpercent-encode）
    - windows: 生のWindowsパス D:\\... をそのまま使う
      （file_uriが日本語パスでResolveにリンクされない場合のフォールバック）
    """
    host_path = to_host_path(container_path)
    if path_style == "windows":
        return str(host_path)
    return host_path.as_uri()
