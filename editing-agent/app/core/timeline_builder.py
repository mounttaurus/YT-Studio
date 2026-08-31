"""
script.json / tts.json / footage.json / aroll.json から OTIO Timeline を構築する。
DATA_SCHEMA.md 6c / Docs/06_editing.md セクション5 のアルゴリズムを実装。

- A1, A2, ... (Audio): tts.json の timeline[] を実測の絶対秒で配置（フレーム変換は丸めのみ、
  累積しない）。話者(speaker_id)ごとに別トラックへ分割する。Resolve側でトラック単位の
  Pan/Volumeを設定しやすくするため（OTIOにpan情報を埋め込んでもResolveのネイティブ.otio
  インポータは解釈しない＝実機確認済。トラック分割が唯一の実用解）
- V1 (Video/Bロール): footage.json の clips[] をsectionでグループ化し、tts.jsonから算出した
  セクション区間を等分割して配置
- V2 (Video/Aロール・任意): aroll.json がある場合のみ追加。tts.json の timeline[] に合わせ
  セリフ1行=1コマをA1と同じ絶対秒で配置（未生成/欠損行はGap）。V1より上に重なるため、
  Resolve上ではAロールが無い区間・行だけV1のBロールが透けて見える＝台本追加でAロールが
  一部欠けても編集データ生成自体は破綻しない（欠損はwarningsに列挙されるのみ）
- 各セクション先頭のクリップ/ギャップに Marker（色=GREEN, name=section）を付与
"""
from datetime import datetime, timezone
from pathlib import Path

import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange

from app.core import path_mapper

MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
LINE_NOT_IN_TIMELINE = "LINE_NOT_IN_TIMELINE"
AROLL_MISSING = "AROLL_MISSING"
FOOTAGE_ABSENT = "FOOTAGE_ABSENT"
EXPORT_STALE = "EXPORT_STALE"


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


def _resolve_aroll_media(
    line_id: str,
    panel: dict | None,
    project_dir: Path,
    episode_dir: Path,
    export_ok_by_line: dict[str, bool],
) -> tuple[Path | None, str]:
    """納品PNG → 生成画像 → 無し の順で解決する（Docs/EDITING_AROLL_PARITY_PLAN.md 2章）。

    戻り値: (実ファイルの絶対パス|None, "psassist"|"raw"|"missing")
    export_ok_by_line に line_id が無ければ export_log.json 自体が無い（旧プロジェクト）
    とみなし、存在確認だけで通す。ok:false の行だけ1を飛ばして2へ落とす。
    """
    if export_ok_by_line.get(line_id, True):
        resolved = path_mapper.resolve_media_path(
            f"psassist/export/panel_{line_id}.png", project_dir, episode_dir,
        )
        if resolved is not None:
            return resolved, "psassist"

    if panel and panel.get("image"):
        resolved = path_mapper.resolve_media_path(f"a_roll/{panel['image']}", project_dir, episode_dir)
        if resolved is not None:
            return resolved, "raw"

    return None, "missing"


def _build_aroll_track(
    aroll: dict,
    tts: dict,
    project_dir: Path,
    episode_dir: Path,
    fps: int,
    path_style: str,
    warnings: list[dict],
    psassist_export_log: list[dict] | None = None,
) -> "otio.schema.Track":
    """Aロール（セリフ1行=1コマ）をA1音声トラックと同じ絶対秒でV2に配置する。

    優先順位は 納品PNG(psassist/export/) → 合成前の生成画像(a_roll/) → Gap。
    未生成/画像欠損の行はGapにする。V2はV1(Bロール)より上に重なる前提のため、
    Gap区間はResolve上でV1がそのまま透けて見える＝欠損の自然なフォールバックになる。
    """
    track = otio.schema.Track(name="V2_Aroll", kind=otio.schema.TrackKind.Video)
    panels_by_line = {p.get("line_id"): p for p in aroll.get("panels", [])}
    export_ok_by_line = {r["line_id"]: r.get("ok", True) for r in (psassist_export_log or [])}

    prev_end_frame = 0
    for entry in tts.get("timeline", []):
        line_id = entry["line_id"]
        start_f = _sec_to_frame(entry["start_sec"], fps)
        end_f = _sec_to_frame(entry["end_sec"], fps)

        if start_f > prev_end_frame:
            gap_range = _frame_range(0, start_f - prev_end_frame, fps)
            track.append(otio.schema.Gap(source_range=gap_range))

        dur = end_f - start_f
        if dur <= 0:
            prev_end_frame = max(prev_end_frame, end_f)
            continue

        tr = _frame_range(0, dur, fps)
        panel = panels_by_line.get(line_id)
        resolved, source = _resolve_aroll_media(line_id, panel, project_dir, episode_dir, export_ok_by_line)

        if resolved is None:
            if panel is None:
                reason = "aroll.jsonに存在しません（台本追加後の未生成）"
            elif panel.get("status") != "done":
                reason = f"画像が未生成です（status: {panel.get('status') or 'unknown'}）"
            else:
                reason = "画像ファイルが見つかりません（生成済みのはずが欠損）"
            warnings.append({
                "code": AROLL_MISSING,
                "message": f"{line_id} のAロールが{reason}（Bロールへフォールバック）",
            })
            track.append(otio.schema.Gap(source_range=tr))
        else:
            ref = otio.schema.ExternalReference(
                target_url=path_mapper.to_target_url(resolved, path_style),
                available_range=tr,
            )
            clip = otio.schema.Clip(name=line_id, media_reference=ref, source_range=tr)
            clip.metadata["youtube_auto"] = {
                "line_id": line_id,
                "speaker_name": panel.get("speaker_name", "") if panel else "",
                "text": panel.get("text", "") if panel else "",
                "aroll_source": source,
            }
            track.append(clip)

            if source == "psassist":
                psd_path = path_mapper.resolve_media_path(
                    f"psassist/psd_final/panel_{line_id}.psd", project_dir, episode_dir,
                )
                if psd_path is not None:
                    try:
                        if psd_path.stat().st_mtime > resolved.stat().st_mtime + 1:
                            warnings.append({
                                "code": EXPORT_STALE,
                                "message": f"{line_id} の納品PNGが古い可能性があります（PSDの方が新しい）",
                            })
                    except OSError:
                        pass

        prev_end_frame = end_f

    return track


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

    clips_list = footage.get("clips", [])
    if not clips_list:
        # footage.json が無い/空（Aロールのみの現運用）でもV1を省略しない。
        # 省略するとResolve上でV2が最下層に落ち、将来Bロールを足す時にトラック番号が動く
        # （Docs/EDITING_AROLL_PARITY_PLAN.md P1）。全編Gapで他トラックと尺を揃える。
        total_f = max((_sec_to_frame(e["end_sec"], fps) for e in tts.get("timeline", [])), default=0)
        if total_f > 0:
            track.append(otio.schema.Gap(source_range=_frame_range(0, total_f, fps)))
        return track

    tts_by_line = {e["line_id"]: e for e in tts.get("timeline", [])}

    # sectionでグループ化（出現順保持）
    groups: dict[str, list[dict]] = {}
    for clip in clips_list:
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
    footage: dict | None,
    project_dir: Path,
    episode_dir: Path,
    fps: int = 30,
    path_style: str = "file_uri",
    aroll: dict | None = None,
    psassist_export_log: list[dict] | None = None,
) -> tuple["otio.schema.Timeline", list[dict]]:
    warnings: list[dict] = []

    if footage is None:
        warnings.append({
            "code": FOOTAGE_ABSENT,
            "message": "Bロール素材が無いためV1は空です（Aロールのみで構成）",
        })
        footage = {"clips": []}

    video_track = _build_video_track(footage, tts, project_dir, episode_dir, fps, path_style, warnings)
    audio_tracks = _build_audio_tracks(tts, project_dir, episode_dir, fps, path_style, warnings)

    timeline = otio.schema.Timeline(name=f"{project_id}_ep{episode_number:02d}")
    timeline.tracks.append(video_track)
    if aroll is not None:
        timeline.tracks.append(_build_aroll_track(
            aroll, tts, project_dir, episode_dir, fps, path_style, warnings,
            psassist_export_log=psassist_export_log,
        ))
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
    """edit.jsonのtimeline統計を計算する。

    video_clip_count はV1(Bロール)+V2(Aロール、有れば)の合算（後方互換のため既存キーは維持）。
    aroll_clip_count はV2単独の内訳（Aロール未導入プロジェクトでは0）。
    aroll_psassist_count / aroll_raw_count はV2内訳のさらに内訳（納品PNG/合成前の生成画像）。
    「196枚あるのに全部rawだった」に数字で気づけるようにする（Docs/EDITING_AROLL_PARITY_PLAN.md 2章）。
    """
    durations_sec = []
    video_clip_count = 0
    aroll_clip_count = 0
    aroll_psassist_count = 0
    aroll_raw_count = 0
    audio_clip_count = 0
    marker_count = 0

    for track in timeline.tracks:
        durations_sec.append(track.duration().to_seconds())
        for item in track:
            if isinstance(item, otio.schema.Clip):
                if track.kind == otio.schema.TrackKind.Video:
                    video_clip_count += 1
                    if track.name == "V2_Aroll":
                        aroll_clip_count += 1
                        source = item.metadata.get("youtube_auto", {}).get("aroll_source")
                        if source == "psassist":
                            aroll_psassist_count += 1
                        elif source == "raw":
                            aroll_raw_count += 1
                else:
                    audio_clip_count += 1
            marker_count += len(item.markers)

    return {
        "duration_sec": max(durations_sec) if durations_sec else 0.0,
        "video_clip_count": video_clip_count,
        "aroll_clip_count": aroll_clip_count,
        "aroll_psassist_count": aroll_psassist_count,
        "aroll_raw_count": aroll_raw_count,
        "audio_clip_count": audio_clip_count,
        "marker_count": marker_count,
    }
