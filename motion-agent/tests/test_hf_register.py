"""描いたインサートを Bロールの箱へ入れる口（app/core/hf_register.py・POST /hf/register）。I1・BROLL_PLAN §8-2。
scrapping-agent へは繋がず、送る本文と受け取り方だけを見る（post を差し込む）。"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import config, hf, hf_put, hf_register, jobs
from app.main import app

INDEX = ('<!doctype html><html><head><script src="vendor/gsap.min.js"></script></head><body>'
         '<div id="root" data-composition-id="main" data-start="0" data-width="1920" data-height="1080" data-duration="3.2"></div>'
         '<script>window.__timelines["main"] = gsap.timeline({paused: true});</script></body></html>')
MOTION = {"profile": "insert", "fit": {"line_id": "line_034"}, "slug": "ruby", "notes": "ジャック・ルビーの紹介",
          "materials": {"photo": {"path": "assets/ruby.jpg", "asset_id": "b_3fa9c2d1_ruby_img",
                                  "source": {"kind": "public_domain"}}}}
JOB_A = "20261012-100000-aaaaaa"
JOB_B = "20261012-110000-bbbbbb"
TARGET = "projects/demo/1/mi_0001"


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SHARED_DIR", tmp_path)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "motion" / "_work")
    d = tmp_path / "projects" / "demo" / "episodes" / "ep01" / "motion" / "hf" / "mi_0001"
    d.mkdir(parents=True)
    (d / "index.html").write_text(INDEX, encoding="utf-8")
    (d / "motion.json").write_text(json.dumps(MOTION, ensure_ascii=False), encoding="utf-8")
    (d / "assets").mkdir()
    (d / "assets" / "ruby.jpg").write_bytes(b"jpegbytes")
    return d


def _job(job_id=JOB_A, target=TARGET, d=None, flash_ok=True, audio=None, status="done", kind="render", sha="auto", engine="hyperframes"):
    out = config.WORK_DIR / job_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "out.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    result = {"file": f"motion/_work/{job_id}/out.mp4", "flash": {"ok": flash_ok}}
    if audio is not None:
        result["audio"] = audio
    if sha == "auto" and d is not None:
        hf.prepare(d)
        result["source_sha"] = hf.source_sha(d)
    elif sha not in ("auto", None):
        result["source_sha"] = sha
    job = {"job_id": job_id, "kind": kind, "engine": engine, "composition": target, "status": status, "result": result}
    (out / "job.json").write_text(json.dumps(job), encoding="utf-8")
    return job


class Post:
    def __init__(self, asset_id="b_9d8c7b6a_ruby_mot", duplicate=False, assigned=True, fail=None):
        self.calls, self.asset_id, self.duplicate, self.assigned, self.fail = [], asset_id, duplicate, assigned, fail

    def __call__(self, url, body):
        self.calls.append({"url": url, "body": json.loads(json.dumps(body))})
        if self.fail:
            f, self.fail = self.fail, None
            raise f
        return {"asset": {"asset_id": self.asset_id}, "duplicate": self.duplicate,
                "assigned": {"line_id": body.get("line_id"), "asset_id": self.asset_id, "index": 0} if self.assigned and body.get("line_id") else None}


def _last(d):
    return json.loads((d / "motion.json").read_text(encoding="utf-8")).get("last_registered")


# ── 正常系 ──

def test_register_sends_the_whole_story_and_remembers_it(box):
    _job(d=box)
    post = Post()
    res = hf_register.register("demo", 1, "mi_0001", post=post)
    call = post.calls[0]
    assert call["url"] == f"{config.SCRAPPING_AGENT_URL}/projects/demo/episodes/1/broll/register"
    b = call["body"]
    assert b["path"] == f"motion/_work/{JOB_A}/out.mp4" and b["license"] == "own" and b["source"] == {"kind": "motion"}
    assert b["line_id"] == "line_034" and b["slug"] == "ruby" and b["added_by"] == "motion"
    assert b["derived_from"] == ["b_3fa9c2d1_ruby_img"] and "replaces" not in b
    assert b["motion"]["plan_dir"] == "projects/demo/episodes/ep01/motion/hf/mi_0001"
    assert b["motion"]["engine"] == "hyperframes" and b["motion"]["fit"] == {"line_id": "line_034"} and b["motion"]["duration"] == 3.2
    assert res["asset_id"] == "b_9d8c7b6a_ruby_mot" and res["replaces"] is None and res["line_id"] == "line_034"
    last = _last(box)
    assert last["asset_id"] == "b_9d8c7b6a_ruby_mot" and last["rendered_sec"] == 3.2 and last["job_id"] == JOB_A
    # 台帳の項目だけが増えて、書いた中身は壊れていない
    assert json.loads((box / "motion.json").read_text(encoding="utf-8"))["fit"] == {"line_id": "line_034"}


def test_second_register_replaces_the_previous_asset(box):
    _job(JOB_A, d=box)
    hf_register.register("demo", 1, "mi_0001", post=Post(asset_id="b_11111111_ruby_mot"))
    (box / "index.html").write_text(INDEX.replace("3.2", "3.4"), encoding="utf-8")   # 直して描き直した
    _job(JOB_B, d=box)
    post = Post(asset_id="b_22222222_ruby_mot")
    res = hf_register.register("demo", 1, "mi_0001", post=post)
    assert post.calls[0]["body"]["replaces"] == "b_11111111_ruby_mot"
    assert res["replaces"] == "b_11111111_ruby_mot"
    assert post.calls[0]["body"]["path"] == f"motion/_work/{JOB_B}/out.mp4"      # 一番新しい描画
    assert _last(box)["asset_id"] == "b_22222222_ruby_mot"


def test_assign_false_and_explicit_line_override(box):
    _job(d=box)
    p1 = Post()
    hf_register.register("demo", 1, "mi_0001", assign=False, post=p1)
    assert "line_id" not in p1.calls[0]["body"]
    p2 = Post()
    hf_register.register("demo", 1, "mi_0001", line_id="line_040", post=p2)
    assert p2.calls[0]["body"]["line_id"] == "line_040"


def test_duplicate_is_reported_and_not_a_replacement(box):
    _job(d=box)
    hf_register.register("demo", 1, "mi_0001", post=Post(asset_id="b_11111111_ruby_mot"))
    res = hf_register.register("demo", 1, "mi_0001", post=Post(asset_id="b_11111111_ruby_mot", duplicate=True))
    assert res["duplicate"] is True and res["replaces"] is None


def test_a_trashed_previous_asset_is_dropped_not_fatal(box):
    _job(d=box)
    hf_register.register("demo", 1, "mi_0001", post=Post(asset_id="b_11111111_ruby_mot"))
    gone = hf_register.RegisterError("scrapping_rejected", "拒否", 502,
                                     detail={"code": "unknown_asset", "message": "replaces の素材が箱に無い: b_11111111_ruby_mot"})
    post = Post(asset_id="b_33333333_ruby_mot", fail=gone)
    res = hf_register.register("demo", 1, "mi_0001", post=post)
    assert len(post.calls) == 2 and "replaces" not in post.calls[1]["body"]
    assert res["replaces_dropped"] == "b_11111111_ruby_mot" and res["replaces"] is None


# ── 入れないもの（M6） ──

def test_flash_failure_is_not_registered(box):
    _job(d=box, flash_ok=False)
    post = Post()
    with pytest.raises(hf_register.RegisterError) as e:
        hf_register.register("demo", 1, "mi_0001", post=post)
    assert e.value.code == "flash_failed" and not post.calls


def test_declared_audio_missing_is_not_registered(box):
    _job(d=box, audio={"expected": True, "has_audio": False, "ok": False})
    with pytest.raises(hf_register.RegisterError) as e:
        hf_register.register("demo", 1, "mi_0001", post=Post())
    assert e.value.code == "audio_missing"


def test_no_render_and_wrong_job(box):
    with pytest.raises(hf_register.RegisterError) as e:
        hf_register.register("demo", 1, "mi_0001", post=Post())
    assert e.value.code == "no_render" and e.value.status == 404
    _job(JOB_A, target="projects/demo/1/mi_0002", d=None)
    with pytest.raises(hf_register.RegisterError) as e2:
        hf_register.register("demo", 1, "mi_0001", job_id=JOB_A, post=Post())
    assert e2.value.code == "bad_job"
    _job(JOB_B, d=box, kind="stills")
    with pytest.raises(hf_register.RegisterError) as e3:
        hf_register.register("demo", 1, "mi_0001", job_id=JOB_B, post=Post())
    assert e3.value.code == "bad_job"


def test_composition_edited_after_render_is_stale_but_notes_are_not(box):
    _job(d=box)
    # 台帳（motion.json の notes）を直しても描いた絵とは関係ない
    m = dict(MOTION, notes="言い換えた")
    (box / "motion.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    hf_register.register("demo", 1, "mi_0001", post=Post())
    # index.html を直したら、描いた絵とずれる
    (box / "index.html").write_text(INDEX.replace("3.2", "3.6"), encoding="utf-8")
    post = Post()
    with pytest.raises(hf_register.RegisterError) as e:
        hf_register.register("demo", 1, "mi_0001", post=post)
    assert e.value.code == "stale_render" and e.value.status == 409 and not post.calls


def test_material_swap_after_render_is_stale(box):
    _job(d=box)
    (box / "assets" / "ruby.jpg").write_bytes(b"another photo")
    with pytest.raises(hf_register.RegisterError) as e:
        hf_register.register("demo", 1, "mi_0001", post=Post())
    assert e.value.code == "stale_render"


def test_scrapping_unreachable_and_rejected(box, monkeypatch):
    _job(d=box)

    def boom(url, json=None, timeout=None):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(hf_register.RegisterError) as e:
        hf_register.register("demo", 1, "mi_0001")
    assert e.value.code == "scrapping_unreachable" and e.value.status == 502

    class R:
        status_code = 404

        def json(self):
            return {"detail": {"code": "unknown_line", "message": "台本に無い行: line_034"}}

    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: R())
    with pytest.raises(hf_register.RegisterError) as e2:
        hf_register.register("demo", 1, "mi_0001")
    assert e2.value.code == "scrapping_rejected" and e2.value.detail["code"] == "unknown_line"
    assert "台本に無い行" in str(e2.value)
    assert _last(box) is None                                   # 入れていない


# ── 台帳の項目・ジョブの探索 ──

def test_put_keeps_last_registered_when_motion_json_is_overwritten(box):
    _job(d=box)
    hf_register.register("demo", 1, "mi_0001", post=Post())
    hf_put.put("demo", 1, "mi_0001", {"motion.json": {"profile": "insert", "fit": {"line_id": "line_035"}}})
    m = json.loads((box / "motion.json").read_text(encoding="utf-8"))
    assert m["fit"] == {"line_id": "line_035"} and m["last_registered"]["asset_id"] == "b_9d8c7b6a_ruby_mot"
    # 自分で last_registered を書いたらそれが勝つ（消せる）
    hf_put.put("demo", 1, "mi_0001", {"motion.json": {"profile": "insert", "last_registered": None}})
    assert json.loads((box / "motion.json").read_text(encoding="utf-8"))["last_registered"] is None


def test_latest_done_render_picks_newest_matching_job(box):
    assert jobs.latest_done_render(TARGET) is None
    _job("20261012-090000-000001", d=box)
    _job("20261012-100000-000002", d=box, status="error")
    _job("20261012-110000-000003", d=box, kind="stills")
    _job("20261012-120000-000004", target="projects/demo/1/mi_0002")
    _job("20261012-130000-000005", d=box, engine="remotion")
    got = jobs.latest_done_render(TARGET)
    assert got["job_id"] == "20261012-090000-000001"
    _job("20261012-140000-000006", d=box)
    assert jobs.latest_done_render(TARGET)["job_id"] == "20261012-140000-000006"


def test_list_project_shows_what_was_registered(box):
    _job(d=box)
    hf_register.register("demo", 1, "mi_0001", post=Post())
    row = hf.list_project("demo", 1)[0]
    assert row["registered"]["asset_id"] == "b_9d8c7b6a_ruby_mot"


# ── API ──

def test_api_maps_errors_and_validates_target(box, monkeypatch):
    c = TestClient(app)
    assert c.post("/hf/register", json={"target": "samples/person_intro"}).status_code == 400
    assert c.post("/hf/register", json={"target": "projects/demo/x/mi_0001"}).status_code == 400
    r = c.post("/hf/register", json={"target": TARGET})            # 描画が無い
    assert r.status_code == 404 and r.json()["detail"]["code"] == "no_render"
    _job(d=box, flash_ok=False)
    r = c.post("/hf/register", json={"target": TARGET})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "flash_failed"
    r = c.post("/hf/register", json={"target": "projects/demo/1/mi_9999"})
    assert r.status_code == 404
