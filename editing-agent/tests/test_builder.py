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


def _video_tracks(timeline):
    return [t for t in timeline.tracks if t.kind == otio.schema.TrackKind.Video]


def _add_aroll(episode_dir: Path, *, omit_image: str | None = None) -> dict:
    """aroll.json フィクスチャを読み込み、パネル画像ファイルを episode_dir/a_roll/ に作る。

    line_020 は panels に存在するがomit_imageで画像ファイルだけ欠損させられる
    （バッチ生成が未完了/失敗のケース）。line_010等は元々panelsに無い
    （台本追加でAロール未生成のまま増えた行のケース）。
    """
    aroll = _load("aroll.json")
    aroll_dir = episode_dir / "a_roll"
    aroll_dir.mkdir(parents=True, exist_ok=True)
    for panel in aroll["panels"]:
        if panel["image"] == omit_image:
            continue
        (aroll_dir / panel["image"]).touch()
    return aroll


def _add_psassist_export(episode_dir: Path, line_ids: list[str]) -> None:
    """episode_dir/psassist/export/panel_{line_id}.png を作る（psassist組版済みの納品PNGの体）。"""
    export_dir = episode_dir / "psassist" / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    for lid in line_ids:
        (export_dir / f"panel_{lid}.png").touch()


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


def test_no_aroll_track_when_aroll_omitted(tmp_path, monkeypatch):
    """aroll未指定(既定None)なら従来通りV1のみ＝Aロール未導入プロジェクトへの後方互換。"""
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)

    timeline, _ = build_timeline("ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS)

    assert [t.name for t in _video_tracks(timeline)] == ["V1_Footage"]


def test_aroll_track_placed_on_tts_timeline(tmp_path, monkeypatch):
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)
    aroll = _add_aroll(episode_dir)

    timeline, warnings = build_timeline(
        "ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS, aroll=aroll,
    )

    video_tracks = _video_tracks(timeline)
    assert [t.name for t in video_tracks] == ["V1_Footage", "V2_Aroll"]

    aroll_track = video_tracks[1]
    clip_names = [item.name for item in aroll_track if isinstance(item, otio.schema.Clip)]
    assert clip_names == ["line_001", "line_002", "line_003", "line_004", "line_005", "line_020"]

    # A1音声トラックと同じ絶対位置にクリップが乗ることを確認
    audio_starts = _audio_clip_starts(_audio_tracks(timeline)[0])
    aroll_starts = _audio_clip_starts(aroll_track)
    for name in clip_names:
        if name in audio_starts:
            assert aroll_starts[name] == audio_starts[name]

    stats = timeline_stats(timeline)
    assert stats["aroll_clip_count"] == 6
    assert stats["video_clip_count"] == 19 + 6


def test_aroll_missing_panel_or_image_falls_back_to_gap(tmp_path, monkeypatch):
    """台本追加でAロール未生成の行、および生成失敗/未完了の行はGapになりBロールへフォールバックする。"""
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)
    aroll = _add_aroll(episode_dir, omit_image="panel_020_line_020.png")

    timeline, warnings = build_timeline(
        "ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS, aroll=aroll,
    )

    aroll_track = _video_tracks(timeline)[1]
    clip_names = [item.name for item in aroll_track if isinstance(item, otio.schema.Clip)]
    assert "line_020" not in clip_names  # panelはあるが画像ファイルが無い
    assert "line_010" not in clip_names  # panels自体に無い(未生成)

    assert any(w["code"] == "AROLL_MISSING" and "line_020" in w["message"] for w in warnings)
    assert any(w["code"] == "AROLL_MISSING" and "line_010" in w["message"] for w in warnings)


def test_aroll_prefers_psassist_export_png(tmp_path, monkeypatch):
    """納品PNG(psassist/export/)があればそちらを使う。無い行は合成前の生成画像へ落ちる。"""
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)
    aroll = _add_aroll(episode_dir)
    _add_psassist_export(episode_dir, ["line_001", "line_002", "line_003", "line_004", "line_005"])

    timeline, _ = build_timeline(
        "ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS, aroll=aroll,
    )

    aroll_track = _video_tracks(timeline)[1]
    clips = {item.name: item for item in aroll_track if isinstance(item, otio.schema.Clip)}
    for lid in ["line_001", "line_002", "line_003", "line_004", "line_005"]:
        assert clips[lid].metadata["youtube_auto"]["aroll_source"] == "psassist"
    # line_020 は納品PNGが無いので合成前の生成画像(a_roll/)へフォールバック
    assert clips["line_020"].metadata["youtube_auto"]["aroll_source"] == "raw"

    stats = timeline_stats(timeline)
    assert stats["aroll_psassist_count"] == 5
    assert stats["aroll_raw_count"] == 1


def test_aroll_falls_back_to_raw_without_psassist_export(tmp_path, monkeypatch):
    """psassist/export/自体が無い（旧プロジェクト）なら全行が合成前の生成画像を使う。"""
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)
    aroll = _add_aroll(episode_dir)

    timeline, _ = build_timeline(
        "ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS, aroll=aroll,
    )

    aroll_track = _video_tracks(timeline)[1]
    clips = [item for item in aroll_track if isinstance(item, otio.schema.Clip)]
    assert clips
    assert all(c.metadata["youtube_auto"]["aroll_source"] == "raw" for c in clips)

    stats = timeline_stats(timeline)
    assert stats["aroll_psassist_count"] == 0
    assert stats["aroll_raw_count"] == len(clips)


def test_aroll_export_log_ok_false_falls_back_to_raw(tmp_path, monkeypatch):
    """export_log.jsonでok:falseの行は納品PNGが存在しても使わず、rawへ落ちる。"""
    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)
    aroll = _add_aroll(episode_dir)
    _add_psassist_export(episode_dir, ["line_001"])
    export_log = [{"line_id": "line_001", "ok": False, "src_size": "1376x768", "out_size": "0x0"}]

    timeline, _ = build_timeline(
        "ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS, aroll=aroll,
        psassist_export_log=export_log,
    )

    aroll_track = _video_tracks(timeline)[1]
    clips = {item.name: item for item in aroll_track if isinstance(item, otio.schema.Clip)}
    assert clips["line_001"].metadata["youtube_auto"]["aroll_source"] == "raw"


def test_export_stale_warns_when_psd_newer_than_export(tmp_path, monkeypatch):
    """psd_final/のPSDが納品PNGより新しければEXPORT_STALEを警告する(止めない)。"""
    import os
    import time

    tts, footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)
    aroll = _add_aroll(episode_dir)
    _add_psassist_export(episode_dir, ["line_001"])

    psd_dir = episode_dir / "psassist" / "psd_final"
    psd_dir.mkdir(parents=True, exist_ok=True)
    psd_path = psd_dir / "panel_line_001.psd"
    psd_path.touch()

    export_png = episode_dir / "psassist" / "export" / "panel_line_001.png"
    now = time.time()
    os.utime(export_png, (now - 100, now - 100))
    os.utime(psd_path, (now, now))

    timeline, warnings = build_timeline(
        "ラリーの秘密", 1, tts, footage, project_dir, episode_dir, fps=FPS, aroll=aroll,
    )

    assert any(w["code"] == "EXPORT_STALE" and "line_001" in w["message"] for w in warnings)
    # 止めない: line_001はGapにならずクリップとして配置されたまま
    aroll_track = _video_tracks(timeline)[1]
    clip_names = [item.name for item in aroll_track if isinstance(item, otio.schema.Clip)]
    assert "line_001" in clip_names


def test_footage_absent_produces_empty_v1_and_warning(tmp_path, monkeypatch):
    """footage.jsonが無くても例外にならず、空のV1トラック(全編Gap)が出る。"""
    tts, _footage, project_dir, episode_dir = _build_layout(tmp_path, monkeypatch)

    timeline, warnings = build_timeline(
        "ラリーの秘密", 1, tts, None, project_dir, episode_dir, fps=FPS,
    )

    video_tracks = _video_tracks(timeline)
    assert [t.name for t in video_tracks] == ["V1_Footage"]
    v1 = video_tracks[0]
    assert not any(isinstance(item, otio.schema.Clip) for item in v1)
    assert any(isinstance(item, otio.schema.Gap) for item in v1)
    # 他トラック(音声)と尺が揃う
    assert v1.duration().value == _audio_tracks(timeline)[0].duration().value

    assert any(w["code"] == "FOOTAGE_ABSENT" for w in warnings)

    stats = timeline_stats(timeline)
    assert stats["video_clip_count"] == 0


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
