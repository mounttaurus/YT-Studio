import copy

import pytest
from fastapi.testclient import TestClient

from app.core import config, plan_store
from app.core.templates import load_sample_input
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "projects" / "p1" / "episodes" / "ep01").mkdir(parents=True)
    monkeypatch.setattr(config, "SHARED_DIR", tmp_path)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "motion" / "_work")
    return TestClient(app)


@pytest.fixture
def mk():
    return load_sample_input("kinetic_teaser", 2, "mkultra")


def test_gimmicks_catalog(client):
    j = client.get("/gimmicks").json()
    ids = {g["id"] for g in j["gimmicks"]}
    assert {"K1_first", "K3_montage", "B3_decode", "B4_font_cycle", "C1_particle_morph"} <= ids
    assert {e["id"] for e in j["enters"]} >= {"A1_portal", "A3_light", "A4_iris", "morph", "cut"}
    assert j["aliases"]["decode"] == "B3_decode" and j["beats"]["b4_out"]["allowed"] == ["K4_out"]
    assert all(g["effect"] for g in j["gimmicks"])


def test_save_get_history_roundtrip(client, mk):
    assert client.get("/plans/p1/1").status_code == 404
    mk["direction"]["beats"]["b1_first"][0]["gimmick"] = "decode"  # 別名
    r = client.put("/plans/p1/1", json={"plan": mk})
    assert r.status_code == 200, r.text
    assert r.json()["analysis"]["ok"] is True and r.json()["analysis"]["total_sec"] == 17.5
    saved = client.get("/plans/p1/1").json()
    assert saved["plan"]["direction"]["beats"]["b1_first"][0]["gimmick"] == "B3_decode"  # 正式な ID で保存
    assert saved["plan"]["template"] == {"id": "kinetic_teaser", "version": 2}
    assert [s["id"] for s in saved["analysis"]["shots"]][:3] == ["s1", "s2", "s3"]
    assert saved["analysis"]["shots"][1]["at"] == "2小節目の3拍目"
    assert client.get("/plans/p1/1/history").json()["history"] == []
    mk["notes"] = "二版"
    client.put("/plans/p1/1", json={"plan": mk})
    hist = client.get("/plans/p1/1/history").json()["history"]
    assert len(hist) == 1
    old = client.get(f"/plans/p1/1/history/{hist[0]}").json()
    assert old.get("notes") != "二版"  # 直前の版


def test_history_is_capped_and_atomic(client, mk, tmp_path):
    for i in range(plan_store.KEEP_HISTORY + 5):
        mk["notes"] = f"v{i}"
        assert client.put("/plans/p1/1", json={"plan": mk}).status_code == 200
    assert len(client.get("/plans/p1/1/history").json()["history"]) == plan_store.KEEP_HISTORY
    assert not list((tmp_path / "projects" / "p1" / "episodes" / "ep01" / "opening").glob("*.tmp"))


def test_invalid_plan_is_saved_with_errors(client, mk):
    bad = copy.deepcopy(mk)
    bad["direction"]["beats"]["b1_first"][0]["bars"] = 1.3
    r = client.put("/plans/p1/1", json={"plan": bad})
    assert r.status_code == 200 and r.json()["analysis"]["ok"] is False
    assert any(e["field"].endswith(".bars") for e in r.json()["analysis"]["errors"])
    assert client.get("/plans/p1/1").json()["plan"]["direction"]["beats"]["b1_first"][0]["bars"] == 1.3


def test_bad_identifiers(client, mk):
    assert client.put("/plans/nope/1", json={"plan": mk}).status_code == 404
    assert client.put("/plans/p1/0", json={"plan": mk}).status_code == 422
    assert client.put("/plans/p1/1", json={"plan": {"template": {"id": "kinetic_teaser", "version": 1}}}).status_code == 422
    assert client.get("/plans/p1/1/history/../../x").status_code in (404, 422)
    assert client.get("/plans/p1/1/history/evil.json").status_code == 422


def test_render_refuses_an_invalid_saved_plan(client, mk):
    bad = copy.deepcopy(mk)
    bad["direction"]["beats"]["b1_first"][0]["gimmick"] = "explode"
    client.put("/plans/p1/1", json={"plan": bad})
    assert client.post("/plans/p1/1/render").status_code == 422
    assert client.post("/plans/p1/2/render").status_code == 404


def test_validate_endpoint_returns_warnings_and_flash_estimate(client, mk):
    r = client.post("/validate", json={"template_id": "kinetic_teaser", "version": 2, "input": mk}).json()
    assert r["ok"] is True and "flash_estimate" in r and r["shots"][3]["enter"]["type"] == "A1_portal"


def test_font_specimen(client, tmp_path):
    r = client.post("/fonts/specimen", json={"ids": ["gothic_black", "role:毛筆"], "text": "洗脳"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["shown"] == 2 and j["ids"][0] == "gothic_black"
    assert (tmp_path / "motion" / "_work" / "specimens" / j["file"]).is_file()
    r = client.post("/fonts/specimen", json={"tag": "不穏", "limit": 3}).json()
    assert 1 <= r["shown"] <= 3 and r["total"] >= r["shown"]


def test_fonts_list_has_tags(client):
    fonts = {f["id"]: f for f in client.get("/fonts").json()["fonts"]}
    assert "不穏" in fonts["brush"]["tags"] and fonts["gothic"]["tags"] == ["標準"]
