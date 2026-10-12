"""話の文脈が要る検査（app/core/hf_project.py）と見た目の束（app/core/hf_look.py）。I1。CLI・ブラウザは呼ばない。"""
import json
from pathlib import Path

import pytest

from app.core import config, hf, hf_look, hf_project

HOST_NAME = ('<div id="n" data-gimmick="L1_name" data-variant="{variant}" data-composition-id="L1_name" '
             'data-composition-src="compositions/blocks/L1_name.html" data-variable-values=\'{{"name":"ジョン","variant":"{variant}"}}\' '
             'data-start="0" data-duration="3" data-track-index="1" data-width="1920" data-height="1080"></div>')
HOST_DOSSIER = ('<div id="d" data-gimmick="F4_dossier" data-composition-id="F4_dossier" '
                'data-composition-src="compositions/blocks/F4_dossier.html" data-variable-values=\'{{"photo":"{photo}"}}\' '
                'data-start="0" data-duration="3" data-track-index="2" data-width="1920" data-height="1080"></div>')


def _index(dur: float, *hosts: str) -> str:
    return (f'<!doctype html><html><head><script src="vendor/gsap.min.js"></script></head><body>'
            f'<div id="root" data-composition-id="main" data-start="0" data-width="1920" data-height="1080" data-duration="{dur}">'
            + "".join(hosts) + '</div></body></html>')


def _comp(shared: Path, pid: str, ep: int, comp: str, hosts=(), dur=3.0, motion=None) -> Path:
    d = shared / "projects" / pid / "episodes" / f"ep{ep:02d}" / "motion" / "hf" / comp
    d.mkdir(parents=True)
    (d / "index.html").write_text(_index(dur, *hosts), encoding="utf-8")
    (d / "motion.json").write_text(json.dumps(motion or {"profile": "insert", "fit": {"sec": dur}}), encoding="utf-8")
    return d


def _tts(shared: Path, pid: str, ep: int, lines: list[tuple[str, float, float]]) -> None:
    p = shared / "projects" / pid / "episodes" / f"ep{ep:02d}"
    p.mkdir(parents=True, exist_ok=True)
    (p / "tts.json").write_text(json.dumps({"timeline": [
        {"line_id": lid, "start_sec": s, "end_sec": e} for lid, s, e in lines]}), encoding="utf-8")


def _codes(issues):
    return sorted(i["code"] for i in issues)


# ── 文脈・行の長さ ──

def test_context_only_inside_an_episode(tmp_path):
    d = _comp(tmp_path, "demo", 3, "mi_0001")
    ctx = hf_project.context(d)
    assert ctx["project_id"] == "demo" and ctx["episode"] == 3 and ctx["comp_id"] == "mi_0001"
    sample = tmp_path / "samples" / "x"
    sample.mkdir(parents=True)
    assert hf_project.context(sample) is None


def test_line_seconds_reads_timeline_then_audio_files(tmp_path):
    d = _comp(tmp_path, "demo", 1, "mi_0001")
    _tts(tmp_path, "demo", 1, [("line_001", 0.0, 3.2), ("line_002", 3.7, 6.2)])
    ctx = hf_project.context(d)
    assert hf_project.line_seconds(ctx, "line_002") == 2.5
    assert hf_project.line_seconds(ctx, "line_999") is None
    (d.parents[2] / "tts.json").write_text(json.dumps({"audio_files": [{"line_id": "line_007", "duration_sec": 4.4}]}), encoding="utf-8")
    assert hf_project.line_seconds(ctx, "line_007") == 4.4


def test_fit_line_id_matches_or_drifts(tmp_path):
    motion = {"profile": "insert", "fit": {"line_id": "line_001"}}
    d = _comp(tmp_path, "demo", 1, "mi_0001", dur=3.2, motion=motion)
    _tts(tmp_path, "demo", 1, [("line_001", 0.0, 3.2)])
    assert hf_project.check_fit(d, motion, 3.2) == []
    assert hf_project.check_fit(d, motion, 3.2 + 1 / 31) == []                 # 1フレームまでは許す
    issues = hf_project.check_fit(d, motion, 4.0)
    assert [(i["code"], i["level"]) for i in issues] == [("length_mismatch", "error")]
    assert "3.2" in issues[0]["message"] and "+0.8" in issues[0]["message"]
    info = hf_project.fit_info(d, motion, 4.0)
    assert info["expected_sec"] == 3.2 and info["drift_sec"] == 0.8


def test_fit_line_id_unresolved_is_a_warning(tmp_path):
    motion = {"profile": "insert", "fit": {"line_id": "line_404"}}
    d = _comp(tmp_path, "demo", 1, "mi_0001", motion=motion)
    assert [(i["code"], i["level"]) for i in hf_project.check_fit(d, motion, 3.0)] == [("fit_line_unresolved", "warning")]
    # 話の外の見本でも落とさない
    sample = tmp_path / "s"
    sample.mkdir()
    (sample / "index.html").write_text(_index(3), encoding="utf-8")
    assert [i["code"] for i in hf_project.check_fit(sample, motion, 3.0)] == ["fit_line_unresolved"]
    assert hf_project.check_fit(d, {"fit": {"sec": 3}}, 3.0) == []


# ── 出典 ──

@pytest.mark.parametrize("mats,code,level", [
    ({"p": {"path": "assets/p.png", "source": {"kind": "self"}}}, None, None),
    ({"p": {"path": "assets/p.png"}}, "source_missing", "warning"),
    ({"p": {"path": "assets/p.png", "source": {"kind": "unknown"}}}, "source_unknown", "warning"),
    ({"p": {"path": "assets/p.png", "source": {"kind": "cc_by", "author": "A"}}}, "source_incomplete", "error"),
    ({"p": {"path": "assets/p.png", "source": {"kind": "bogus"}}}, "source_kind", "error"),
    ({"p": {"path": "assets/p.png", "source": "自作"}}, "source_legacy", "warning"),
    ({"p": {"path": "assets/p.png", "source": {"kind": "cc_by", "author": "A", "url": "https://x/y", "license": "CC BY 4.0"}}}, None, None),
])
def test_sources(tmp_path, mats, code, level):
    d = _comp(tmp_path, "demo", 1, "mi_0001")
    issues = hf_project.check_sources(d, {"materials": mats})
    if code is None:
        assert issues == []
    else:
        assert [(i["code"], i["level"]) for i in issues] == [(code, level)]


def test_block_material_must_be_declared(tmp_path):
    d = _comp(tmp_path, "demo", 1, "mi_0001", hosts=[HOST_DOSSIER.format(photo="assets/p.png")])
    issues = hf_project.check_sources(d, {"materials": {}})
    assert [(i["code"], i["level"]) for i in issues] == [("material_undeclared", "warning")]
    ok = {"materials": {"p": {"path": "assets/p.png", "source": {"kind": "self"}}}}
    assert hf_project.check_sources(d, ok) == []


# ── 繰り返し ──

def test_repeat_in_episode_adjacent_and_recent(tmp_path):
    fp = ["L1_name:decode"]
    _tts(tmp_path, "demo", 3, [("line_001", 0, 3), ("line_002", 3, 6), ("line_003", 6, 9), ("line_004", 9, 12)])
    mk = lambda comp, lid, variant="decode", ep=3: _comp(  # noqa: E731
        tmp_path, "demo", ep, comp, hosts=[HOST_NAME.format(variant=variant)],
        motion={"profile": "insert", "fit": {"line_id": lid}})
    a = mk("mi_0001", "line_001")
    mk("mi_0002", "line_002")
    d3 = mk("mi_0003", "line_004")
    # 他の2本が同じ → この3本目で「3回目」
    assert "repeat_in_episode" in _codes(hf_project.check_repeat(d3, fp))
    # 台本の順で隣り合う（line_001 と line_002）
    assert "repeat_adjacent" in _codes(hf_project.check_repeat(a, fp))
    assert all(i["level"] == "warning" for i in hf_project.check_repeat(a, fp))
    # 離れていて種類も違えば何も言わない
    z = mk("mi_0004", "line_003", variant="type")
    assert hf_project.check_repeat(z, ["L1_name:type"]) == []
    # 直近2話と重なる
    prev = _comp(tmp_path, "demo", 2, "mi_0001", hosts=[HOST_NAME.format(variant="type")])
    assert prev.is_dir()
    assert "repeat_recent" in _codes(hf_project.check_repeat(z, ["L1_name:type"]))


def test_repeat_is_silent_outside_an_episode_or_without_blocks(tmp_path):
    sample = tmp_path / "s"
    sample.mkdir()
    (sample / "index.html").write_text(_index(3), encoding="utf-8")
    assert hf_project.check_repeat(sample, ["L1_name:decode"]) == []
    d = _comp(tmp_path, "demo", 1, "mi_0001")
    assert hf_project.check_repeat(d, []) == []


def test_list_project_shows_drift_and_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SHARED_DIR", tmp_path)
    motion = {"profile": "insert", "fit": {"line_id": "line_001"}}
    _comp(tmp_path, "demo", 1, "mi_0001", hosts=[HOST_NAME.format(variant="decode")], dur=4.0, motion=motion)
    _tts(tmp_path, "demo", 1, [("line_001", 0.0, 3.0)])
    rows = hf.list_project("demo", 1)
    assert rows[0]["fingerprint"] == ["L1_name:decode"] and rows[0]["duration"] == 4.0
    assert rows[0]["fit_info"]["drift_sec"] == 1.0


# ── 見た目の束 ──

def test_look_css_is_deterministic_and_links_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SHARED_DIR", tmp_path / "shared")
    look = hf_look.resolve("dossier_crimson")
    assert look and hf_look.validate(look) == []
    css = hf_look.css(look)
    assert "--look-accent: #c8282e;" in css and '--look-font-name: "Noto Serif CJK JP";' in css
    assert css == hf_look.css(look)
    d = tmp_path / "c"
    d.mkdir()
    (d / "index.html").write_text(_index(3), encoding="utf-8")
    motion = {"look": "dossier_crimson"}
    assert hf_look.sync(d, motion) == ["look.css"]
    assert hf_look.sync(d, motion) == []                                   # 同じなら書かない
    assert [(i["code"], i["level"]) for i in hf_look.check(d, motion)] == [("look_not_linked", "warning")]
    (d / "index.html").write_text(_index(3).replace("<head>", '<head><link rel="stylesheet" href="look.css" />'), encoding="utf-8")
    assert hf_look.check(d, motion) == []
    assert [i["code"] for i in hf_look.check(d, {"look": "no_such_look"})] == ["look_unknown"]
    assert hf_look.check(d, {}) == [] and hf_look.sync(d, {}) == []


def test_channel_look_overrides_the_dictionary(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    (shared / "motion" / "looks").mkdir(parents=True)
    monkeypatch.setattr(config, "SHARED_DIR", shared)
    mine = {"id": "dossier_crimson", "label": "うちの束",
            "colors": {"desk": "#000000", "paper": "#ffffff", "ink": "#eeeeee", "ink_dim": "#999999", "accent": "#ff0000"},
            "fonts": {"name": "A", "sub": "B", "mono": "C", "hand": "D"}}
    (shared / "motion" / "looks" / "dossier_crimson.json").write_text(json.dumps(mine), encoding="utf-8")
    assert hf_look.resolve("dossier_crimson")["label"] == "うちの束"
    row = next(l for l in hf_look.list_looks() if l["id"] == "dossier_crimson")
    assert row["source"] == "channel"
    assert {l["id"] for l in hf_look.list_looks()} >= {"ledger_amber", "cold_file"}


def test_invalid_look_is_reported(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    (shared / "motion" / "looks").mkdir(parents=True)
    monkeypatch.setattr(config, "SHARED_DIR", shared)
    bad = {"id": "bad", "colors": {"desk": "red", "paper": "#ffffff", "ink": "#eeeeee", "ink_dim": "#999999", "accent": "#ff0000"},
           "fonts": {"name": 'A"; x', "sub": "B", "mono": "C", "hand": "D"}}
    (shared / "motion" / "looks" / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    d = tmp_path / "c"
    d.mkdir()
    (d / "index.html").write_text(_index(3), encoding="utf-8")
    issues = hf_look.check(d, {"look": "bad"})
    assert [i["code"] for i in issues[:2]] == ["look_invalid", "look_invalid"]
    assert hf_look.sync(d, {"look": "bad"}) == []                          # 壊れた束では look.css を作らない
