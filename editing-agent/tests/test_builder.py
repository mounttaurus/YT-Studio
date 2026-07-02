import json
from pathlib import Path

import opentimelineio as otio
import pytest

from app.core import path_mapper
from app.core.timeline_builder import _sec_to_frame, build_timeline, timeline_stats

FIXTURES = Path(__file__).parent / "fixtures"
FPS = 30

MISSING_AUDIO_PATH = "episodes/ep01/audio/line_015.wav"
MISSING_FOOTAGE_PATH = "footage/clip_010.mp4"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _build_layout(tmp_path: Path, monkeypatch, *, omit_audio: str | None = None, omit_footage: str | None = None):
    # path_mapper.to_host_path() は container_path が SHARED_DIR 配下であることを前提とするため、
    # テストではSHARED_DIRをtmp_pathに差し替える
    monkeypatch.setattr(path_mapper, "SHARED_DIR", tmp_path)
    monkeypatch.setattr(path_mapper, "HOST_SHARED_DIR", "D:\\FakeShared")

    tts = _load("tts.json")
    footage = _load("footage.json")

    project_dir = tmp_path / "project"
    episode_dir = project_dir / "episodes" / "ep01"
    episode_dir.mkdir(parents=True)

    for entry in tts["timeline"]:
        if entry["file_path"] == omit_audio:
            continue
        p = project_dir / entry["file_path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()

    for clip in footage["clips"]:
        if clip["file_path"] == omit_footage:
            continue
        p = episode_dir / clip["file_path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()

    return tts, footage, project_dir, episode_dir


def _audio_tracks(timeline):
    return [t for t in timeline.tracks if t.kind == otio.schema.TrackKind.Audio]


def _audio_clip_starts(audio_track) -> dict:
    cursor = 0
    starts = {}
    for item in audio_track:
        if isinstance(item, otio.schema.Clip):
            starts[item.name] = cursor
        cursor += item.duration().value
    return starts


def test_audio_split_into_per_speaker_tracks(tmp_path, monkeypatch):
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)

    timeline, warnings = build_timeline("ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS)

    audio_files_by_line = {a["line_id"]: a for a in tts["audio_files"]}
    speaker_ids = {a["speaker_id"] for a in tts["audio_files"]}

    audio_tracks = _audio_tracks(timeline)
    assert len(audio_tracks) == len(speaker_ids)

    # 全話者トラックで合計31クリップ、各トラックは単一話者のクリップのみを持つ（混在しない）
    total_clips = 0
    seen_speaker_ids = set()
    for track in audio_tracks:
        clip_names = [item.name for item in track if isinstance(item, otio.schema.Clip)]
        total_clips += len(clip_names)
        sids_on_track = {audio_files_by_line[name]["speaker_id"] for name in clip_names}
        assert len(sids_on_track) == 1
        sid = next(iter(sids_on_track))
        assert sid in track.name  # トラック名にも話者idが含まれる
        seen_speaker_ids.add(sid)

        starts = _audio_clip_starts(track)
        for name, start in starts.items():
            entry = next(e for e in tts["timeline"] if e["line_id"] == name)
            assert start == _sec_to_frame(entry["start_sec"], FPS)

        # 自分の話者の行がない区間はGapで埋まり、トラック全長が他トラックと揃う（同期）
        assert track.duration().value == audio_tracks[0].duration().value

    assert seen_speaker_ids == speaker_ids
    assert total_clips == 31
    assert not any(w["code"] == "MEDIA_NOT_FOUND" for w in warnings)


def test_missing_audio_creates_gap_and_warning(tmp_path, monkeypatch):
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch, omit_audio=MISSING_AUDIO_PATH)

    timeline, warnings = build_timeline("ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS)

    names = []
    for track in _audio_tracks(timeline):
        names += [item.name for item in track if isinstance(item, otio.schema.Clip)]
    assert "line_015" not in names
    assert len(names) == 30

    assert any(w["code"] == "MEDIA_NOT_FOUND" and "line_015" in w["message"] for w in warnings)


def test_missing_footage_creates_gap_and_warning(tmp_path, monkeypatch):
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch, omit_footage=MISSING_FOOTAGE_PATH)

    timeline, warnings = build_timeline("ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS)

    video_track = timeline.tracks[0]
    names = [item.name for item in video_track if isinstance(item, otio.schema.Clip)]
    assert "clip_010" not in names

    assert any(w["code"] == "MEDIA_NOT_FOUND" and "clip_010" in w["message"] for w in warnings)


def test_photo_placement_uses_full_allotted_duration(tmp_path, monkeypatch):
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)

    timeline, _ = build_timeline("ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS)

    video_track = timeline.tracks[0]
    items = list(video_track)

    idx = next(i for i, item in enumerate(items) if isinstance(item, otio.schema.Clip) and item.name == "clip_004")
    clip = items[idx]
    assert clip.metadata["youtube_auto"]["media_type"] == "photo"

    # 写真は割当尺をフルに使うため、直後にGapは入らない
    if idx + 1 < len(items):
        next_item = items[idx + 1]
        assert not isinstance(next_item, otio.schema.Gap)


def test_short_video_clip_gets_trailing_gap(tmp_path, monkeypatch):
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)

    timeline, _ = build_timeline("ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS)

    video_track = timeline.tracks[0]
    items = list(video_track)

    idx = next(i for i, item in enumerate(items) if isinstance(item, otio.schema.Clip) and item.name == "clip_013")
    clip = items[idx]
    # clip_013 の duration_sec=14.0 -> 420フレームのまま配置される
    assert clip.source_range.duration.value == 420

    # discussion セクションは5クリップで割当尺が420フレームより大きいため、後ろにGapが入る
    assert idx + 1 < len(items)
    assert isinstance(items[idx + 1], otio.schema.Gap)


def test_section_markers_count(tmp_path, monkeypatch):
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)

    timeline, _ = build_timeline("ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS)

    video_track = timeline.tracks[0]
    marker_names = []
    for item in video_track:
        for m in item.markers:
            marker_names.append(m.name)

    # footage.json 内のsection: intro, main_topic, discussion, summary, outro
    assert set(marker_names) == {"intro", "main_topic", "discussion", "summary", "outro"}
    assert len(marker_names) == 5


def test_otio_roundtrip_and_stats(tmp_path, monkeypatch):
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)

    timeline, warnings = build_timeline("ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS)

    otio_text = otio.adapters.write_to_string(timeline, adapter_name="otio_json")
    reloaded = otio.adapters.read_from_string(otio_text, adapter_name="otio_json")
    assert reloaded.name == timeline.name

    stats = timeline_stats(timeline)
    assert stats["audio_clip_count"] == 31
    assert stats["video_clip_count"] == 19
    assert stats["marker_count"] == 5
    assert stats["duration_sec"] > 0
