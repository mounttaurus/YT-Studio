"""HyperFrames の口（app/core/hf.py・hf_checks.py）。CLI を呼ばない部分だけ（CLI の通しは手で確かめる＝§16）。"""
import json
from pathlib import Path

import pytest

from app.core import config, hf, hf_checks

INDEX_OK = """<!doctype html><html><head><script src="vendor/gsap.min.js"></script>
<style>.a{background:url(#grain)} .b{background:url("assets/x.png")}</style></head>
<body><div id="root" data-composition-id="main" data-start="0" data-width="1920" data-height="1080" data-duration="5">
<svg xmlns="http://www.w3.org/2000/svg"><rect filter="url(#grain-f)"/></svg>
</div><script>window.__timelines["main"] = gsap.timeline({paused: true});</script></body></html>
"""


def _write(d: Path, index: str = INDEX_OK, motion: dict | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(index, encoding="utf-8")
    if motion is not None:
        (d / "motion.json").write_text(json.dumps(motion), encoding="utf-8")
    return d


# ── target の解決 ──

def test_resolve_sample_and_project():
    assert hf.resolve("samples/person_intro") == config.HF_SAMPLES_DIR / "person_intro"
    assert hf.resolve("projects/demo/3/mi_0001") == (
        config.SHARED_DIR / "projects" / "demo" / "episodes" / "ep03" / "motion" / "hf" / "mi_0001")


@pytest.mark.parametrize("bad", ["", "samples", "samples/../x", "samples/a b", "projects/p/0/mi", "projects/p/x/mi",
                                 "projects/../../etc/1/mi", "other/x", "projects/p/1/mi/extra"])
def test_resolve_rejects_bad_targets(bad):
    with pytest.raises(hf.TargetError):
        hf.resolve(bad)


# ── ネットワーク禁止（M3） ──

def test_clean_composition_has_no_network_issues(tmp_path):
    assert hf_checks.check_network(_write(tmp_path)) == []


@pytest.mark.parametrize("snippet,code", [
    ('<script src="https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js"></script>', "network_url"),
    ('<img src="//example.com/a.png">', "network_url"),
    ('<style>.x{background:url( "https://example.com/a.png")}</style>', "network_url"),
    ('<style>@import url("https://fonts.googleapis.com/css2?family=Inter");</style>', "network_url"),
    ('<script>fetch("data.json")</script>', "network_api"),
    ('<script>new WebSocket("ws://x")</script>', "network_api"),
    ('<script>import("https://esm.sh/x")</script>', "network_api"),
])
def test_network_uses_are_errors_with_line(tmp_path, snippet, code):
    d = _write(tmp_path, INDEX_OK.replace("</body>", snippet + "\n</body>"))
    issues = hf_checks.check_network(d)
    assert issues and issues[0]["code"] == code and issues[0]["level"] == "error"
    assert issues[0]["file"] == "index.html" and issues[0]["line"] >= 1


def test_vendor_and_cli_outputs_are_not_scanned(tmp_path):
    d = _write(tmp_path)
    for sub in ("vendor", "snapshots", "renders"):
        (d / sub).mkdir()
        (d / sub / "x.js").write_text('fetch("https://example.com")', encoding="utf-8")
    (d / "compositions").mkdir()
    (d / "compositions" / "part.html").write_text('<img src="https://example.com/a.png">', encoding="utf-8")
    issues = hf_checks.check_network(d)
    assert [i["file"] for i in issues] == ["compositions/part.html"]


# ── 長さ ──

def test_root_info_reads_id_and_duration():
    assert hf_checks.root_info(INDEX_OK) == {"id": "main", "duration": 5.0}
    assert hf_checks.root_info("<div></div>") == {"id": None, "duration": None}


def test_length_matches_fit_sec(tmp_path):
    issues, dur = hf_checks.check_length(_write(tmp_path, motion={"fit": {"sec": 5.0}}), {"fit": {"sec": 5.0}})
    assert issues == [] and dur == 5.0


def test_length_mismatch_is_an_error(tmp_path):
    issues, _ = hf_checks.check_length(_write(tmp_path), {"fit": {"sec": 4.0}})
    assert [i["code"] for i in issues] == ["length_mismatch"]


def test_one_frame_tolerance(tmp_path):
    issues, _ = hf_checks.check_length(_write(tmp_path), {"fit": {"sec": 5.0 + 1 / 31}})
    assert issues == []


def test_missing_duration_and_motion(tmp_path):
    d = _write(tmp_path, INDEX_OK.replace(' data-duration="5"', ""))
    issues, dur = hf_checks.check_length(d, None)
    assert dur is None and {i["code"]: i["level"] for i in issues} == {
        "missing_duration": "error", "missing_motion_json": "warning"}


def test_fit_line_id_is_a_warning_until_i1(tmp_path):
    issues, _ = hf_checks.check_length(_write(tmp_path), {"fit": {"line_id": "line_001"}})
    assert [(i["code"], i["level"]) for i in issues] == [("fit_line_unsupported", "warning")]


# ── 準備（vendor・見本の素材） ──

def test_prepare_copies_vendor_and_runs_only_allowed_generators(tmp_path, monkeypatch):
    hf_root = tmp_path / "hf"
    (hf_root / "node_modules/gsap/dist").mkdir(parents=True)
    (hf_root / "node_modules/gsap/dist/gsap.min.js").write_text("/*gsap*/", encoding="utf-8")
    monkeypatch.setattr(config, "HF_DIR", hf_root)
    called = []
    monkeypatch.setitem(hf.GENERATORS, "portrait", lambda out, spec: (called.append(out.name), out.write_bytes(b"png")))
    d = _write(tmp_path / "comp", motion={"generated": [
        {"tool": "portrait", "out": "assets/_gen/p.png"},
        {"tool": "portrait", "out": "../escape.png"},     # プロジェクトの外は作らない
        {"tool": "rm -rf", "out": "assets/x.png"},        # 許可していない生成器は走らせない
    ]})
    made = hf.prepare(d)
    assert made == ["vendor/gsap.min.js", "assets/_gen/p.png"] and called == ["p.png"]
    assert not (tmp_path / "escape.png").exists()
    assert hf.prepare(d) == []   # 2回目は何もしない


# ── ジョブの指定 ──

def test_job_spec_clamps_frames_and_checks_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HF_SAMPLES_DIR", tmp_path)
    _write(tmp_path / "s1")
    spec = hf.job_spec("stills", "samples/s1", tmp_path / "job", frames=99, at=[0.5, 1.25])
    assert spec["engine"] == "hyperframes" and spec["mode"] == "stills" and spec["frames"] == 12 and spec["at"] == [0.5, 1.25]
    assert hf._stills_cmd(spec)[-2:] == ["--at", "0.5,1.25"]
    assert hf.job_spec("render", "samples/s1", tmp_path / "job")["quality"] == "standard"
    with pytest.raises(hf.TargetError):
        hf.job_spec("render", "samples/s1", tmp_path / "job", quality="ultra")
    with pytest.raises(FileNotFoundError):
        hf.job_spec("render", "samples/nope", tmp_path / "job")


# ── 見本 ──

def test_sample_person_intro_passes_our_checks():
    d = config.HF_SAMPLES_DIR / "person_intro"
    if not d.is_dir():
        pytest.skip("見本が無い環境")
    motion = hf.read_motion(d)
    assert motion["profile"] == "insert"
    assert hf_checks.check_network(d) == []
    issues, dur = hf_checks.check_length(d, motion)
    assert issues == [] and dur == 5.0
