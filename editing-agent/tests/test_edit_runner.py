"""edit_runner.run_edit の subtitle_format 分岐を、project_manager をfixtureで差し替えて検証。"""
import json
from pathlib import Path

import pytest

from app.core import edit_runner, project_manager

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def patched(monkeypatch, tmp_path):
    tts = json.loads((FIXTURES / "tts.json").read_text(encoding="utf-8"))
    footage = json.loads((FIXTURES / "footage.json").read_text(encoding="utf-8"))
    captured = {}

    monkeypatch.setattr(project_manager, "find_project_dir", lambda pid: tmp_path)
    monkeypatch.setattr(project_manager, "episode_dir", lambda pid, n: tmp_path)
    monkeypatch.setattr(project_manager, "get_episode_tts", lambda pid, n: tts)
    monkeypatch.setattr(project_manager, "get_episode_footage", lambda pid, n: footage)
    monkeypatch.setattr(project_manager, "get_subtitle_style", lambda pid: {"per_speaker": {"speaker_a": {"color": [1, 0.9, 0.2, 1]}}})
    monkeypatch.setattr(project_manager, "get_episode_status", lambda pid, n: {})
    monkeypatch.setattr(project_manager, "update_episode_status", lambda *a, **k: None)
    monkeypatch.setattr(project_manager, "append_error", lambda *a, **k: None)

    def fake_write(pid, n, otio_text, srt_text, edit_json, fcpxml_text=None):
        captured["otio"] = otio_text
        captured["srt"] = srt_text
        captured["fcpxml"] = fcpxml_text
        captured["edit_json"] = edit_json
        return tmp_path

    monkeypatch.setattr(project_manager, "write_edit_outputs", fake_write)
    return captured


def test_both_produces_fcpxml(patched):
    edit_json = edit_runner.run_edit("p", 1, subtitle_format="both")
    assert patched["fcpxml"] is not None
    assert "<title" in patched["fcpxml"]
    assert edit_json["files"]["fcpxml"] == "edit/subtitles.fcpxml"
    # 話者別色がconfigから反映されている
    assert "1 0.9 0.2 1" in patched["fcpxml"]


def test_srt_only_omits_fcpxml(patched):
    edit_json = edit_runner.run_edit("p", 1, subtitle_format="srt")
    assert patched["fcpxml"] is None
    assert "fcpxml" not in edit_json["files"]
    assert edit_json["files"]["srt"] == "edit/subtitles.srt"


def test_fcpxml_only(patched):
    edit_json = edit_runner.run_edit("p", 1, subtitle_format="fcpxml")
    assert patched["fcpxml"] is not None
    assert edit_json["files"]["fcpxml"] == "edit/subtitles.fcpxml"
