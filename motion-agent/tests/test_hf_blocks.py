"""HyperFrames の辞書（ブロック）の口（app/core/hf_blocks.py・I1）と文字の枠の実測（hf.deep_check）。CLI・ブラウザは呼ばない。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core import config, hf, hf_blocks

HOST = ('<div id="n1" class="clip" data-gimmick="L1_name" data-variant="decode" data-composition-id="L1_name" '
        'data-composition-src="compositions/blocks/L1_name.html" data-variable-values=\'{"name":"ジョン","sub":"証言者","variant":"decode","x":100,"y":100}\' '
        'data-start="0" data-duration="3" data-track-index="1" data-width="1920" data-height="1080"></div>')


def _index(*hosts: str) -> str:
    return ('<!doctype html><html><head><script src="vendor/gsap.min.js"></script></head><body>'
            '<div id="root" data-composition-id="main" data-start="0" data-width="1920" data-height="1080" data-duration="5">'
            + "".join(hosts) + '</div><script>window.__timelines["main"] = gsap.timeline({paused: true});</script></body></html>')


def _comp(tmp_path: Path, *hosts: str) -> Path:
    d = tmp_path / "comp"
    d.mkdir()
    (d / "index.html").write_text(_index(*hosts), encoding="utf-8")
    return d


def _codes(issues):
    return sorted(i["code"] for i in issues)


# ── 辞書 ──

def test_dictionary_is_valid():
    assert hf_blocks.validate_dictionary() == []


def test_dictionary_lists_the_first_blocks_with_vars():
    blocks = {b["id"]: b for b in hf_blocks.list_blocks()}
    for bid in ("F2_photo", "F4_dossier", "L1_name", "L2_title_card", "F6_spotlight", "L3_callout", "A8_wipe", "D3_texture"):
        assert bid in blocks, bid                                       # 計画 §6-2 の I1 の部品 ＋ 質感の重ね
    kinds = {bid: b["kind"] for bid, b in blocks.items()}
    assert kinds["F2_photo"] == kinds["F4_dossier"] == kinds["L2_title_card"] == "base"
    assert kinds["L1_name"] == kinds["F6_spotlight"] == kinds["L3_callout"] == "layer"
    assert kinds["A8_wipe"] == "enter" and kinds["D3_texture"] == "overlay"
    n = blocks["L1_name"]
    assert n["kind"] == "layer" and n["variants"]["options"] == ["decode", "type", "stamp", "wipe", "slide"]
    assert {v["id"] for v in n["vars"]} >= {"name", "sub", "variant", "x", "y", "size", "seed"}
    assert blocks["F4_dossier"]["materials"] == [{"var": "photo", "kinds": ["image"]}]


# ── host の読み取り・プロジェクトへ写す ──

def test_hosts_reads_attributes_and_variables():
    hs = hf_blocks.hosts(_index(HOST))
    assert len(hs) == 1
    h = hs[0]
    assert h["block"] == "L1_name" and h["variant"] == "decode" and h["gimmick"] == "L1_name"
    assert h["vars"]["name"] == "ジョン" and h["vars"]["x"] == 100 and h["duration"] == "3"


def test_sync_copies_used_blocks_once_and_freezes_them(tmp_path):
    d = _comp(tmp_path, HOST)
    made = hf_blocks.sync_project(d)
    assert made == ["compositions/blocks/L1_name.html", "compositions/blocks/_lock.json"]
    copied = (d / "compositions/blocks/L1_name.html").read_text(encoding="utf-8")
    assert copied == (config.HF_DIR / "blocks/L1_name/block.html").read_text(encoding="utf-8")
    lock = json.loads((d / "compositions/blocks/_lock.json").read_text(encoding="utf-8"))
    assert lock["L1_name"]["version"] == 1 and len(lock["L1_name"]["sha1"]) == 40
    assert hf_blocks.sync_project(d) == []                               # 2回目は何もしない
    (d / "compositions/blocks/L1_name.html").write_text("<!-- 凍結した版 -->", encoding="utf-8")
    assert hf_blocks.sync_project(d) == []
    assert (d / "compositions/blocks/L1_name.html").read_text(encoding="utf-8") == "<!-- 凍結した版 -->"   # 辞書を直しても上書きしない（M7）


def test_sync_ignores_unknown_blocks_and_plain_html(tmp_path):
    d = _comp(tmp_path, HOST.replace("L1_name.html", "Nope.html"))
    assert hf_blocks.sync_project(d) == []
    assert not (d / "compositions").exists()


def test_prepare_through_hf_copies_blocks(tmp_path, monkeypatch):
    hf_root = tmp_path / "hfroot"
    (hf_root / "node_modules/gsap/dist").mkdir(parents=True)
    (hf_root / "node_modules/gsap/dist/gsap.min.js").write_text("/*gsap*/", encoding="utf-8")
    shutil.copytree(config.HF_DIR / "blocks", hf_root / "blocks")
    monkeypatch.setattr(config, "HF_DIR", hf_root)
    d = _comp(tmp_path, HOST)
    made = hf.prepare(d)
    assert made == ["vendor/gsap.min.js", "compositions/blocks/L1_name.html", "compositions/blocks/_lock.json"]


# ── 検査 ──

def _host(**over) -> str:
    vals = {"name": "ジョン", "sub": "証言者", "variant": "decode", "x": 100, "y": 100}
    attrs = {"id": "n1", "gimmick": "L1_name", "variant": "decode", "cid": "L1_name", "src": "compositions/blocks/L1_name.html",
             "start": "0", "duration": "3"}
    for k, v in over.items():
        if k in vals or k in ("size", "seed", "bogus"):
            vals[k] = v
        else:
            attrs[k] = v
    parts = [f'<div id="{attrs["id"]}"']
    if attrs["gimmick"] is not None:
        parts.append(f' data-gimmick="{attrs["gimmick"]}"')
    if attrs["variant"] is not None:
        parts.append(f' data-variant="{attrs["variant"]}"')
    parts.append(f' data-composition-id="{attrs["cid"]}" data-composition-src="{attrs["src"]}"')
    parts.append(f" data-variable-values='{json.dumps(vals, ensure_ascii=False)}'")
    if attrs["start"] is not None:
        parts.append(f' data-start="{attrs["start"]}"')
    if attrs["duration"] is not None:
        parts.append(f' data-duration="{attrs["duration"]}"')
    parts.append(' data-track-index="1" data-width="1920" data-height="1080"></div>')
    return "".join(parts)


def test_valid_host_has_no_issues(tmp_path):
    d = _comp(tmp_path, _host())
    assert hf_blocks.check(d, {"profile": "insert"}) == []


@pytest.mark.parametrize("over,code", [
    ({"name": "あ" * 15}, "block_bad_var"),                    # maxLength 14
    ({"x": 99999}, "block_bad_var"),                           # max 1700
    ({"x": "100"}, "block_bad_var"),                           # 数でない
    ({"variant": "spin"}, "block_bad_var"),                    # enum の外
    ({"variant": "type"}, "block_variant_mismatch"),           # data-variant と変数が違う（data-variant=decode のまま）
    ({"cid": "L1_title"}, "block_host_id"),
    ({"start": None}, "block_host_attr"),
    ({"duration": None}, "block_host_attr"),
])
def test_bad_hosts_are_errors(tmp_path, over, code):
    d = _comp(tmp_path, _host(**over))
    issues = hf_blocks.check(d, {"profile": "insert"})
    assert code in _codes(issues)
    assert all(i["level"] == "error" for i in issues if i["code"] == code)


def test_unknown_var_and_missing_gimmick_are_warnings(tmp_path):
    d = _comp(tmp_path, _host(bogus=1, gimmick=None))
    issues = hf_blocks.check(d, {"profile": "insert"})
    assert _codes(issues) == ["block_host_gimmick", "block_unknown_var"]
    assert {i["level"] for i in issues} == {"warning"}


def test_unknown_block_is_an_error_unless_it_was_copied(tmp_path):
    d = _comp(tmp_path, _host(src="compositions/blocks/Nope.html", cid="Nope", gimmick="Nope"))
    assert _codes(hf_blocks.check(d, {"profile": "insert"})) == ["block_unknown"]
    (d / "compositions/blocks").mkdir(parents=True)
    (d / "compositions/blocks/Nope.html").write_text("<template></template>", encoding="utf-8")
    assert hf_blocks.check(d, {"profile": "insert"}) == []             # 写しがあれば（その場の部品など）形だけ見て通す


def test_profile_must_be_allowed_by_the_block(tmp_path):
    d = _comp(tmp_path, _host())
    assert "block_profile" not in _codes(hf_blocks.check(d, {"profile": "free"}))
    assert "block_profile" in _codes(hf_blocks.check(d, {"profile": "movie"}))


def test_material_must_exist_inside_the_project(tmp_path):
    h = ('<div id="dos" data-gimmick="F4_dossier" data-composition-id="F4_dossier" data-composition-src="compositions/blocks/F4_dossier.html" '
         'data-variable-values=\'{"photo":"assets/p.png"}\' data-start="0" data-duration="5" data-track-index="1" data-width="1920" data-height="1080"></div>')
    d = _comp(tmp_path, h)
    assert _codes(hf_blocks.check(d, {"profile": "insert"})) == ["material_missing"]
    (d / "assets").mkdir()
    (d / "assets/p.png").write_bytes(b"png")
    assert hf_blocks.check(d, {"profile": "insert"}) == []
    h2 = h.replace("assets/p.png", "../escape.png")
    (d / "index.html").write_text(_index(h2), encoding="utf-8")
    assert _codes(hf_blocks.check(d, {"profile": "insert"})) == ["material_missing"]    # プロジェクトの外は素材にしない


def test_fingerprint_ignores_seed_and_timing(tmp_path):
    a = _comp(tmp_path, _host(seed=1, start="0"))
    fa = hf_blocks.fingerprint(a)
    shutil.rmtree(a)
    b = _comp(tmp_path, _host(seed=99, start="2.5"))
    assert hf_blocks.fingerprint(b) == fa == ["L1_name:decode"]


# ── 見本 ──

def test_sample_person_intro_blocks_is_valid(tmp_path):
    src = config.HF_SAMPLES_DIR / "person_intro_blocks"
    if not src.is_dir():
        pytest.skip("見本が無い環境")
    d = tmp_path / "pib"
    d.mkdir()
    for name in ("index.html", "motion.json"):
        shutil.copyfile(src / name, d / name)
    motion = hf.read_motion(d)
    made = hf_blocks.sync_project(d)
    assert sorted(m for m in made if m.endswith(".html")) == sorted(
        f"compositions/blocks/{b}.html" for b in ("D3_texture", "F4_dossier", "F6_spotlight", "L1_name", "L3_callout"))
    (d / "assets/_gen").mkdir(parents=True)
    (d / "assets/_gen/portrait.png").write_bytes(b"png")
    issues = hf_blocks.check(d, motion)
    assert [i for i in issues if i["level"] == "error"] == []
    assert hf_blocks.fingerprint(d) == ["D3_texture:", "F4_dossier:", "F6_spotlight:", "L1_name:decode", "L3_callout:"]


def test_sample_title_card_blocks_is_valid(tmp_path):
    src = config.HF_SAMPLES_DIR / "title_card_blocks"
    if not src.is_dir():
        pytest.skip("見本が無い環境")
    d = tmp_path / "tcb"
    d.mkdir()
    for name in ("index.html", "motion.json"):
        shutil.copyfile(src / name, d / name)
    hf_blocks.sync_project(d)
    (d / "assets/_gen").mkdir(parents=True)
    (d / "assets/_gen/portrait.png").write_bytes(b"png")
    assert [i for i in hf_blocks.check(d, hf.read_motion(d)) if i["level"] == "error"] == []
    assert hf_blocks.fingerprint(d) == ["A8_wipe:", "D3_texture:", "F2_photo:", "L2_title_card:wipe"]


# ── 文字の枠の実測（deep_check）: node は呼ばず、出力の読み取りだけ確かめる ──

def _fake_run(stdout: str, code: int = 0):
    def run(args, **kw):
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")
    return run


def test_deep_check_maps_findings_to_issues(tmp_path, monkeypatch):
    res = {"ok": True, "canvas": {"width": 1920, "height": 1080}, "duration": 5, "samples": 17, "texts": 40, "band": 0.2, "safe": 0.9,
           "findings": [{"code": "subtitle_band", "level": "error", "selector": "#n", "text": "注記", "time": 1.2, "over": 40,
                         "rect": {"x": 100, "y": 900, "w": 300, "h": 80}, "source": "index.html", "message": "帯に掛かる", "hits": 3}]}
    monkeypatch.setattr(subprocess, "run", _fake_run("noise\n@@RESULT@@ " + json.dumps(res, ensure_ascii=False) + "\n"))
    out = hf.deep_check(tmp_path, "insert")
    assert out["texts"] == 40 and out["issues"][0]["code"] == "subtitle_band" and out["issues"][0]["file"] == "index.html"
    assert out["issues"][0]["time"] == 1.2 and out["issues"][0]["level"] == "error"


def test_deep_check_failures_are_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("ログだけ\n"))
    assert [i["code"] for i in hf.deep_check(tmp_path, "insert")["issues"]] == ["measure_failed"]
    monkeypatch.setattr(subprocess, "run", _fake_run('@@RESULT@@ {"ok": false, "error": "chrome が起動しない", "findings": []}'))
    out = hf.deep_check(tmp_path, "insert")
    assert out["issues"][0]["code"] == "measure_failed" and "chrome" in out["issues"][0]["message"]

    def boom(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)
    monkeypatch.setattr(subprocess, "run", boom)
    assert [i["code"] for i in hf.deep_check(tmp_path, "insert")["issues"]] == ["measure_failed"]


def test_profiles_are_the_source_of_the_numbers(tmp_path):
    ins, opn, free = (hf.profile_config(p) for p in ("insert", "opening", "free"))
    assert ins["band"] == 0.2 and ins["safe"] == 0.9
    assert opn["band"] == 0.0 and opn["safe"] == 0.9 and opn["max_sec"] == 20
    assert free["band"] == 0.0 and free["safe"] == 0.0
    assert hf.profile_config("unknown")["band"] == 0.2                  # 知らない用途は insert と同じ
    assert hf.deep_check(tmp_path, "free")["skipped"] is True            # 見るものが無い用途はブラウザを呼ばない
