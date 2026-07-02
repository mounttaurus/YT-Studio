"""
script.json / tts.json / footage.json から OTIO Timeline を構築する。
DATA_SCHEMA.md 6c / Docs/06_editing.md セクション5 のアルゴリズムを実装。

- A1, A2, ... (Audio): tts.json の timeline[] を実測の絶対秒で配置（フレーム変換は丸めのみ、
  累積しない）。話者(speaker_id)ごとに別トラックへ分割する。Resolve側でトラック単位の
  Pan/Volumeを設定しやすくするため（OTIOにpan情報を埋め込んでもResolveのネイティブ.otio
  インポータは解釈しない＝実機確認済。トラック分割が唯一の実用解）
- V1 (Video): footage.json の clips[] をsectionでグループ化し、tts.jsonから算出した
  セクション区間を等分割して配置
- 各セクション先頭のクリップ/ギャップに Marker（色=GREEN, name=section）を付与
"""
from datetime import datetime, timezone
from pathlib import Path

import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange

from app.core import path_mapper

MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
LINE_NOT_IN_TIMELINE = "LINE_NOT_IN_TIMELINE"


def _sec_to_frame(sec: float, fps: int) -> int:
    return round(sec * fps)


def _frame_range(start_frame: int, duration_frames: int, fps: int) -> TimeRange:
    return TimeRange(
        start_time=RationalTime(start_frame, fps),
        duration=RationalTime(duration_frames, fps),
    )


def _build_audio_tracks(
    tts: dict,
    project_dir: Path,
    episode_dir: Path,
    fps: int,
    path_style: str,
    warnings: list[dict],
) -> list["otio.schema.Track"]:
    """話者(speaker_id)ごとに別トラックを作る。

    全トラックを同じ(Gap, dur)パターンで並行して進めることで、どのトラックも
    同じ絶対時刻に同期したまま、自分の話者の行だけがClipになる（他話者の区間はGap）。
    これによりResolve上でトラック単位の選択・Pan/Volume設定ができる
    （OTIOのEffect/metadata経由のパンはResolveのネイティブ.otioインポータが解釈しないことを
    実機確認済。トラック分割が唯一の実用解）。
    """
    audio_files_by_line = {a["line_id"]: a for a in tts.get("audio_files", [])}

    speaker_order: list[str] = []
    speaker_names: dict[str, str] = {}
    for entry in tts.get("timeline", []):
        meta = audio_files_by_line.get(entry["line_id"], {})
        sid = meta.get("speaker_id", "") or "unknown"
        if sid not in speaker_order:
            speaker_order.append(sid)
            speaker_names.setdefault(sid, meta.get("speaker_name", ""))

    tracks: dict[str, "otio.schema.Track"] = {}
    for i, sid in enumerate(speaker_order):
        name = speaker_names.get(sid, "")
        track_name = f"A{i + 1}_{sid}_{name}" if name else f"A{i + 1}_{sid}"
        tracks[sid] = otio.schema.Track(name=track_name, kind=otio.schema.TrackKind.Audio)

    prev_end_frame = 0
    for entry in tts.get("timeline", []):
        line_id = entry["line_id"]
        start_f = _sec_to_frame(entry["start_sec"], fps)
        end_f = _sec_to_frame(entry["end_sec"], fps)
        audio_meta = audio_files_by_line.get(line_id, {})
        sid = audio_meta.get("speaker_id", "") or "unknown"

        if start_f > prev_end_frame:
            gap_range = _frame_range(0, start_f - prev_end_frame, fps)
            for t in tracks.values():
                t.append(otio.schema.Gap(source_range=gap_range))

        dur = end_f - start_f
        if dur <= 0:
            prev_end_frame = max(prev_end_frame, end_f)
            continue

        resolved = path_mapper.resolve_media_path(entry["file_path"], project_dir, episode_dir)
        tr = _frame_range(0, dur, fps)
        if resolved is None:
            warnings.append({
                "code": MEDIA_NOT_FOUND,
                "message": f"{entry['file_path']} が見つかりません（Gapで代替）",
            })
            for t in tracks.values():
                t.append(otio.schema.Gap(source_range=tr))
        else:
            for t_sid, t in tracks.items():
                if t_sid != sid:
                    t.append(otio.schema.Gap(source_range=tr))
                    continue
                ref = otio.schema.ExternalReference(
                    target_url=path_mapper.to_target_url(resolved, path_style),
                    available_range=tr,
                )
                clip = otio.schema.Clip(name=line_id, media_reference=ref, source_range=tr)
                clip.metadata["youtube_auto"] = {
                    "line_id": line_id,
                    "speaker_name": audio_meta.get("speaker_name", ""),
                    "text": audio_meta.get("text", ""),
                }
                t.append(clip)

        prev_end_frame = end_f

    return list(tracks.values())


def _place_footage_clip(
    clip_data: dict,
    allotted_frames: int,
    project_dir: Path,
    episode_dir: Path,
    fps: int,
    path_style: str,
    warnings: list[dict],
    track: "otio.schema.Track",
):
    """セクション区間の割当尺(allotted_frames)内にクリップを配置し、配置したitemを返す。

    映像で実尺が割当より短い場合は残りをGapで埋める。
    メディアが見つからない場合はallotted_frames全体をGapにする。
    allotted_frames<=0の場合は何も配置せずNoneを返す。
    """
    if allotted_frames <= 0:
        return None

    media_type = clip_data.get("media_type")
    duration_sec = clip_data.get("duration_sec") or 0

    resolved = path_mapper.resolve_media_path(clip_data["file_path"], project_dir, episode_dir)
    if resolved is None:
        warnings.append({
            "code": MEDIA_NOT_FOUND,
            "message": f"{clip_data['file_path']} が見つかりません（Gapで代替）",
        })
        gap = otio.schema.Gap(source_range=_frame_range(0, allotted_frames, fps))
        track.append(gap)
        return gap

    if media_type == "video" and duration_sec > 0:
        clip_frames = min(_sec_to_frame(duration_sec, fps), allotted_frames)
        if clip_frames <= 0:
            clip_frames = allotted_frames
    else:
        # 写真、またはduration_sec<=0（vecteezy動画等）は割当尺フルで配置
        clip_frames = allotted_frames

    tr = _frame_range(0, clip_frames, fps)
    ref = otio.schema.ExternalReference(
        target_url=path_mapper.to_target_url(resolved, path_style),
        available_range=tr,
    )
    clip = otio.schema.Clip(name=clip_data["id"], media_reference=ref, source_range=tr)
    clip.metadata["youtube_auto"] = {
        "clip_id": clip_data["id"],
        "section": clip_data.get("section", ""),
        "media_type": media_type,
        "source": clip_data.get("source", ""),
    }
    track.append(clip)

    if clip_frames < allotted_frames:
        track.append(otio.schema.Gap(source_range=_frame_range(0, allotted_frames - clip_frames, fps)))

    return clip


def _build_video_track(
    footage: dict,
    tts: dict,
    project_dir: Path,
    episode_dir: Path,
    fps: int,
    path_style: str,
    warnings: list[dict],
) -> "otio.schema.Track":
    track = otio.schema.Track(name="V1_Footage", kind=otio.schema.TrackKind.Video)

    tts_by_line = {e["line_id"]: e for e in tts.get("timeline", [])}

    # sectionでグループ化（出現順保持）
    groups: dict[str, list[dict]] = {}
    for clip in footage.get("clips", []):
        groups.setdefault(clip.get("section", ""), []).append(clip)

    blocks = []
    for section, clips in groups.items():
        line_ids: set[str] = set()
        for c in clips:
            line_ids.update(c.get("line_ids", []))

        starts = []
        ends = []
        for lid in line_ids:
            e = tts_by_line.get(lid)
            if e:
                starts.append(e["start_sec"])
                ends.append(e["end_sec"] + e.get("pause_after_sec", 0))

        if not starts:
            warnings.append({
                "code": LINE_NOT_IN_TIMELINE,
                "message": f"section '{section}' のline_idsがtts.jsonのtimelineに見つかりません（スキップ）",
            })
            continue

        start_f = _sec_to_frame(min(starts), fps)
        end_f = _sec_to_frame(max(ends), fps)
        if end_f <= start_f:
            continue

        blocks.append({"section": section, "clips": clips, "start_f": start_f, "end_f": end_f})

    blocks.sort(key=lambda b: b["start_f"])

    cursor = 0
    for block in blocks:
        if block["start_f"] > cursor:
            track.append(otio.schema.Gap(source_range=_frame_range(0, block["start_f"] - cursor, fps)))

        n = len(block["clips"])
        total = block["end_f"] - block["start_f"]
        base = total // n
        remainder = total % n

        first_item = None
        for i, clip_data in enumerate(block["clips"]):
            allotted = base + (1 if i < remainder else 0)
            item = _place_footage_clip(clip_data, allotted, project_dir, episode_dir, fps, path_style, warnings, track)
            if first_item is None and item is not None:
                first_item = item

        if first_item is not None:
            marker = otio.schema.Marker(
                name=block["section"],
                marked_range=_frame_range(0, 1, fps),
                color=otio.schema.MarkerColor.GREEN,
            )
            first_item.markers.append(marker)

        cursor = block["end_f"]

    return track


def build_timeline(
    project_id: str,
    episode_number: int,
    tts: dict,
    footage: dict,
    project_dir: Path,
    episode_dir: Path,
    fps: int = 30,
    path_style: str = "file_uri",
) -> tuple["otio.schema.Timeline", list[dict]]:
    warnings: list[dict] = []

    video_track = _build_video_track(footage, tts, project_dir, episode_dir, fps, path_style, warnings)
    audio_tracks = _build_audio_tracks(tts, project_dir, episode_dir, fps, path_style, warnings)

    timeline = otio.schema.Timeline(name=f"{project_id}_ep{episode_number:02d}")
    timeline.tracks.append(video_track)
    for t in audio_tracks:
        timeline.tracks.append(t)

    timeline.metadata["youtube_auto"] = {
        "project_id": project_id,
        "episode": episode_number,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fps": fps,
    }

    return timeline, warnings


def timeline_stats(timeline: "otio.schema.Timeline") -> dict:
    """edit.jsonのtimeline統計を計算する。"""
    durations_sec = []
    video_clip_count = 0
    audio_clip_count = 0
    marker_count = 0

    for track in timeline.tracks:
        durations_sec.append(track.duration().to_seconds())
        for item in track:
            if isinstance(item, otio.schema.Clip):
                if track.kind == otio.schema.TrackKind.Video:
                    video_clip_count += 1
                else:
                    audio_clip_count += 1
            marker_count += len(item.markers)

    return {
        "duration_sec": max(durations_sec) if durations_sec else 0.0,
        "video_clip_count": video_clip_count,
        "audio_clip_count": audio_clip_count,
        "marker_count": marker_count,
    }
