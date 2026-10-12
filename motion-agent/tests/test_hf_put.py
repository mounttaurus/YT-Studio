"""話のコンポジションへ書く口（app/core/hf_put.py・PUT /hf/projects/{pid}/{ep}/{comp_id}/files）。I1・M4。"""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.core import config, hf, hf_put
from app.main import app

INDEX = ('<!doctype html><html><head><script src="vendor/gsap.min.js"></script></head><body>'
         '<div id="root" data-composition-id="main" data-start="0" data-width="1920" data-height="1080" data-duration="3"></div>'
         '<script>window.__timelines["main"] = gsap.timeline({paused: true});</script></body></html>')
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SHARED_DIR", tmp_path)
    (tmp_path / "projects" / "demo").mkdir(parents=True)
    monkeypatch.setattr(hf, "lint", lambda d: {"ok": True, "errors": [], "warnings": []})
    return tmp_path


def _comp_dir(shared, cid="mi_0001", ep=1):
    return shared / "projects" / "demo" / "episodes" / f"ep{ep:02d}" / "motion" / "hf" / cid


def test_new_comp_ids_count_up_from_mi_0001(shared):
    a = hf_put.put("demo", 1, None, {"index.html": INDEX})
    b = hf_put.put("demo", 1, None, {"index.html": INDEX})
    assert (a["comp_id"], a["created"]) == ("mi_0001", True) and b["comp_id"] == "mi_0002"
    assert hf_put.put("demo", 2, None, {"index.html": INDEX})["comp_id"] == "mi_0001"      # 話ごと
    assert a["target"] == "projects/demo/1/mi_0001"
    assert (_comp_dir(shared) / "index.html").read_text(encoding="utf-8") == INDEX


def test_history_keeps_the_previous_version(shared):
    hf_put.put("demo", 1, None, {"index.html": INDEX, "motion.json": {"profile": "insert", "fit": {"sec": 3}}})
    r = hf_put.put("demo", 1, "mi_0001", {"index.html": INDEX.replace('"3"', '"4"'), "motion.json": '{"profile": "free"}'})
    assert r["created"] is False and r["history"]
    h = _comp_dir(shared) / "history" / r["history"]
    assert json.loads((h / "motion.json").read_text(encoding="utf-8"))["profile"] == "insert"
    assert (h / "index.html").read_text(encoding="utf-8") == INDEX
    # 新しい版が書かれている
    assert json.loads((_comp_dir(shared) / "motion.json").read_text(encoding="utf-8"))["profile"] == "free"


def test_dict_motion_json_and_base64_image(shared):
    r = hf_put.put("demo", 1, None, {"index.html": INDEX, "motion.json": {"profile": "insert", "notes": "日本語"},
                                     "assets/photo.png": {"base64": PNG}, "compositions/custom/mine.html": "<template></template>"})
    d = _comp_dir(shared)
    assert sorted(r["written"]) == ["assets/photo.png", "compositions/custom/mine.html", "index.html", "motion.json"]
    assert json.loads((d / "motion.json").read_text(encoding="utf-8"))["notes"] == "日本語"
    assert (d / "assets/photo.png").read_bytes().startswith(b"\x89PNG")
    assert not list(d.rglob("*.tmp"))


@pytest.mark.parametrize("files", [
    {"../evil.html": "x"},
    {"compositions/blocks/L1_name.html": "x"},          # 辞書から prepare が写す
    {"vendor/gsap.min.js": "x"},
    {"assets/../../x.png": {"base64": PNG}},
    {"assets/p.exe": "x"},
    {"index.htm": "x"},
    {"assets/photo.png": "文字列では画像を書けない"},
    {"index.html": {"base64": PNG}},
    {"assets/photo.png": {"base64": "***not base64***"}},
    {"motion.json": "{ not json"},
    {"motion.json": "[1, 2]"},
    {},
])
def test_bad_files_are_rejected(shared, files):
    with pytest.raises(hf_put.PutError):
        hf_put.put("demo", 1, None, files)
    assert not (shared / "projects" / "demo" / "episodes").exists()                 # 何も作らない


def test_missing_project_and_missing_comp(shared):
    with pytest.raises(FileNotFoundError):
        hf_put.put("nope", 1, None, {"index.html": INDEX})
    with pytest.raises(FileNotFoundError):
        hf_put.put("demo", 1, "mi_0009", {"index.html": INDEX})
    with pytest.raises(hf.TargetError):
        hf_put.put("demo", 1, "../x", {"index.html": INDEX})


def test_history_is_trimmed(shared, monkeypatch):
    hf_put.put("demo", 1, None, {"index.html": INDEX})
    monkeypatch.setattr(hf_put, "HISTORY_KEEP", 3)
    d = _comp_dir(shared)
    for i in range(6):
        (d / "history").mkdir(exist_ok=True)
        (d / "history" / f"20260101-00000{i}").mkdir()
    hf_put.put("demo", 1, "mi_0001", {"index.html": INDEX})
    assert len([x for x in (d / "history").iterdir() if x.is_dir()]) == 3


# ── API ──

def test_put_api_creates_and_checks(shared):
    c = TestClient(app)
    r = c.put("/hf/projects/demo/1/new/files", json={"files": {"index.html": INDEX, "motion.json": {"profile": "insert", "fit": {"sec": 3.0}}}})
    assert r.status_code == 200
    body = r.json()
    assert body["comp_id"] == "mi_0001" and body["created"] is True
    assert body["check"]["ok"] is True and body["check"]["duration"] == 3.0
    # 長さが合わなければ、書いたあとの検査が教える（書けはする）
    r2 = c.put("/hf/projects/demo/1/mi_0001/files", json={"files": {"motion.json": {"profile": "insert", "fit": {"sec": 5.0}}}})
    assert r2.json()["check"]["ok"] is False and r2.json()["check"]["errors"][0]["code"] == "length_mismatch"
    assert c.put("/hf/projects/demo/1/new/files", json={"files": {"../x": "y"}}).status_code == 422
    assert c.put("/hf/projects/nope/1/new/files", json={"files": {"index.html": INDEX}}).status_code == 404
    assert c.put("/hf/projects/demo/1/..%2Fx/files", json={"files": {"index.html": INDEX}}).status_code in (400, 404)
    assert c.put("/hf/projects/demo/1/new/files", json={"files": {"index.html": INDEX}, "check": False}).json().get("check") is None


def test_engine_version_mismatch_is_a_warning(shared, monkeypatch):
    monkeypatch.setattr(hf, "version", lambda: "0.9.0")
    r = hf_put.put("demo", 1, None, {"index.html": INDEX, "motion.json": {"profile": "insert", "fit": {"sec": 3.0},
                                                                          "engine": {"name": "hyperframes", "version": "0.8.140"}}})
    res = hf.check(r["target"])
    assert res["ok"] is True
    assert [w["code"] for w in res["warnings"]] == ["engine_version_differs"]
