"""
editing-agent のメイン処理オーケストレーション。
tts.json / footage.json を読み、timeline.otio / subtitles.srt / edit.json を書き出し、
project.json の editing / video_edit ステータスを更新する。
Docs/06_editing.md セクション3・DATA_SCHEMA.md 6c参照。
"""
import os
from datetime import datetime, timezone

import opentimelineio as otio

from app.core import fcpxml_subtitle_writer, project_manager, srt_writer, timeline_builder

SCHEMA_VERSION = "1.0.0"


def run_edit(
    project_id: str,
    episode_number: int,
    fps: int = 30,
    path_style: str = "file_uri",
    speaker_prefix: bool = False,
    subtitle_format: str = "both",
    lang: str | None = None,
) -> dict:
    """script/tts/footageを統合し編集情報を生成する。

    lang指定時は原語ではなく locales/{lang}/tts.json（翻訳音声）を使い、
    出力も locales/{lang}/edit/ へ書く（Docs/08_i18n.md §6）。footage.json は
    言語に依らず常に原語のもの（原語と翻訳語でAロール/Bロールを共有するため）。
    """
    project_dir = project_manager.find_project_dir(project_id)
    episode_dir = project_manager.episode_dir(project_id, episode_number)
    if project_dir is None or episode_dir is None:
        raise FileNotFoundError(f"episode directory not found: {project_id} ep{episode_number}")

    tts = project_manager.get_episode_tts(project_id, episode_number, lang=lang)
    footage = project_manager.get_episode_footage(project_id, episode_number)
    if tts is None or footage is None:
        raise FileNotFoundError(f"{'locales/' + lang + '/' if lang else ''}tts.json or footage.json not found")

    project_manager.update_episode_status(project_id, episode_number, lang=lang, editing="running")

    try:
        timeline, warnings = timeline_builder.build_timeline(
            project_id, episode_number, tts, footage, project_dir, episode_dir, fps=fps, path_style=path_style,
        )
        otio_text = otio.adapters.write_to_string(timeline, adapter_name="otio_json")
        srt_text = srt_writer.build_srt(tts, speaker_prefix=speaker_prefix)

        fcpxml_text = None
        if subtitle_format in ("fcpxml", "both"):
            style = project_manager.get_subtitle_style(project_id)
            fcpxml_text = fcpxml_subtitle_writer.build_fcpxml(tts, style=style, fps=fps)

        stats = timeline_builder.timeline_stats(timeline)
        stats["subtitle_count"] = srt_writer.count_subtitles(tts)

        edit_dir_rel = f"episodes/ep{episode_number:02d}/locales/{lang}/edit" if lang \
            else f"episodes/ep{episode_number:02d}/edit"
        files = {
            "otio": f"{edit_dir_rel}/timeline.otio",
            "srt": f"{edit_dir_rel}/subtitles.srt",
        }
        if fcpxml_text is not None:
            files["fcpxml"] = f"{edit_dir_rel}/subtitles.fcpxml"

        edit_json = {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "episode": episode_number,
            "lang": lang,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "fps": fps,
            "path_style": path_style,
            "host_media_root": os.getenv("HOST_SHARED_DIR", ""),
            "files": files,
            "timeline": stats,
            "warnings": warnings,
        }

        project_manager.write_edit_outputs(project_id, episode_number, otio_text, srt_text, edit_json,
                                           fcpxml_text=fcpxml_text, lang=lang)

    except Exception as e:
        project_manager.append_error(project_id, "editing", str(e))
        project_manager.update_episode_status(project_id, episode_number, lang=lang, editing="error")
        raise

    status_updates = {"editing": "done"}
    current_status = project_manager.get_episode_status(project_id, episode_number, lang=lang)
    if current_status.get("video_edit", "not_started") == "not_started":
        status_updates["video_edit"] = "pending"
    project_manager.update_episode_status(project_id, episode_number, lang=lang, **status_updates)

    return edit_json
