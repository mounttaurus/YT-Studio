"""HyperFrames のコンポジションに効果音を置く道具（app/core/hf_sfx.py・A0）。CLI は呼ばない。"""
import json
import re
import shutil
from pathlib import Path

import pytest

from app.core import config, hf, hf_sfx
from app.core.audio import sfx_synth

INDEX = """<!doctype html><html><head><script src="vendor/gsap.min.js"></script></head>
<body><div id="root" data-composition-id="main" data-start="0" data-width="1920" data-height="1080" data-duration="5">
  <div id="scene"></div>
</div><script>window.__timelines["main"] = gsap.timeline({paused: true});</script></body></html>
"""

MOTION = {
    "profile": "opening",
    "sounds": {
        "tick_a": {"kind": "tick", "length": 0.05, "pitch": 2300, "gain": 0.35, "seed": 11},
        "rise": {"kind": "riser", "length": 1.0, "pitch": 150, "gain": 0.7, "seed": 2},
    },
    "sfx": [
        {"id": "sfx-a", "sound": "tick_a", "at": 0.8, "volume": 0.7},
        {"id": "sfx-b", "sound": "tick_a", "at": 0.85},
        {"id": "sfx-rise", "sound": "rise", "at": 3.0},
    ],
}


def _comp(tmp_path, motion=MOTION, index=INDEX) -> Path:
    d = tmp_path / "comp"
    d.mkdir()
    (d / "index.html").write_bytes(index.encode("utf-8"))   # 改行を変えない
    (d / "motion.json").write_text(json.dumps(motion), encoding="utf-8")
    return d


def _codes(issues):
    return [i["code"] for i in issues]


# ── 解く・検査 ──

def test_resolve_places_riser_by_its_end():
    cues, sounds, issues = hf_sfx.resolve(MOTION)
    assert issues == [] and set(sounds) == {"tick_a", "rise"}
    by = {c["id"]: c for c in cues}
    assert by["sfx-a"]["start"] == pytest.approx(0.8) and by["sfx-a"]["volume"] == pytest.approx(0.7)
    assert by["sfx-rise"]["start"] == pytest.approx(2.0)             # 打点 3.0 から長さ 1.0 を逆算
    assert by["sfx-rise"]["file"] == "assets/sfx/rise.wav"


@pytest.mark.parametrize("patch,code", [
    ({"sounds": {"x": {"kind": "bell"}}, "sfx": []}, "sfx_unknown_kind"),
    ({"sounds": {"x": {"kind": "tick", "pitch": 5}}, "sfx": []}, "sfx_bad_param"),
    ({"sounds": {"x": "tick"}, "sfx": []}, "sfx_bad_sound"),
    ({"sounds": {"bad name": {"kind": "tick"}}, "sfx": []}, "sfx_bad_sound_name"),
    ({"sounds": {"x": {"kind": "tick"}}, "sfx": [{"id": "a", "sound": "nope", "at": 1}]}, "sfx_unknown_sound"),
    ({"sounds": {"x": {"kind": "tick"}}, "sfx": [{"id": "a b", "sound": "x", "at": 1}]}, "sfx_bad_id"),
    ({"sounds": {"x": {"kind": "tick"}}, "sfx": [{"id": "a", "sound": "x", "at": 1}, {"id": "a", "sound": "x", "at": 2}]},
     "sfx_duplicate_id"),
    ({"sounds": {"x": {"kind": "tick"}}, "sfx": [{"id": "a", "sound": "x", "at": -1}]}, "sfx_bad_at"),
    ({"sounds": {"x": {"kind": "tick"}}, "sfx": [{"id": "a", "sound": "x", "at": 1, "volume": 9}]}, "sfx_bad_volume"),
    ({"sounds": {"x": {"kind": "riser", "length": 2.0}}, "sfx": [{"id": "a", "sound": "x", "at": 1.0}]}, "sfx_before_start"),
    ({"sounds": [], "sfx": []}, None),
])
def test_bad_definitions_are_errors(patch, code):
    cues, _, issues = hf_sfx.resolve(patch)
    if code is None:
        assert cues == []
        return
    assert code in _codes(issues)
    assert all(i["level"] == "error" for i in issues if i["code"] == code)
    assert cues == [] or code in ("sfx_duplicate_id",)    # 誤りのある項目は置かない


# ── WAV ──

def test_generate_writes_wav_once_and_matches_synth(tmp_path):
    d = _comp(tmp_path)
    _, sounds, _ = hf_sfx.resolve(MOTION)
    made = hf_sfx.generate(d, sounds)
    assert sorted(made) == ["assets/sfx/rise.wav", "assets/sfx/tick_a.wav"]
    assert (d / "assets/sfx/tick_a.wav").read_bytes() == sfx_synth.make_wav("tick", length=0.05, pitch=2300, gain=0.35, seed=11)
    assert hf_sfx.generate(d, sounds) == []           # 同じ中身なら書かない
    sounds["tick_a"]["seed"] = 12
    assert hf_sfx.generate(d, sounds) == ["assets/sfx/tick_a.wav"]


# ── <audio> ──

def test_markup_has_id_and_never_crossorigin():
    cues, _, _ = hf_sfx.resolve(MOTION)
    html = hf_sfx.markup(cues)
    tags = re.findall(r"<audio\b[^>]*>", html)
    assert len(tags) == 3
    for t in tags:
        assert re.search(r'\bid="sfx-[a-z]+"', t) and "crossorigin" not in t and "data-start=" in t and "data-volume=" in t
    assert 'id="sfx-rise" src="assets/sfx/rise.wav" data-start="2" data-duration="1"' in html


def test_markup_puts_touching_or_overlapping_sounds_on_different_tracks():
    motion = {"sounds": {"t": {"kind": "tick", "length": 0.05}},
              "sfx": [{"id": "a", "sound": "t", "at": 1.0}, {"id": "b", "sound": "t", "at": 1.05},   # 端が接する
                      {"id": "c", "sound": "t", "at": 1.02}, {"id": "d", "sound": "t", "at": 2.0}]}
    cues, _, _ = hf_sfx.resolve(motion)
    lanes = {m.group(1): int(m.group(2)) for m in re.finditer(r'id="(\w+)".*?data-track-index="(\d+)"', hf_sfx.markup(cues))}
    assert lanes["a"] == 10 and lanes["c"] == 11 and lanes["b"] == 12 and lanes["d"] == 10


def test_sync_inserts_after_root_then_is_idempotent_and_removable(tmp_path):
    d = _comp(tmp_path)
    cues, _, _ = hf_sfx.resolve(MOTION)
    assert hf_sfx.sync_tags(d, cues) is True
    text = (d / "index.html").read_text(encoding="utf-8")
    root_end = text.index(">", text.index("data-composition-id")) + 1
    assert text[root_end:].lstrip().startswith("<!-- sfx:begin")      # ルートの開始タグの直後
    assert text.count("<audio ") == 3 and "<!-- sfx:end -->" in text
    assert '<div id="scene"></div>' in text                          # 他は触らない
    assert hf_sfx.sync_tags(d, cues) is False                        # 2回目は変えない
    assert (d / "index.html").read_text(encoding="utf-8") == text
    # 手で壊しても、次の同期で motion.json の通りに戻る
    (d / "index.html").write_text(text.replace('data-start="0.8"', 'data-start="9"'), encoding="utf-8")
    assert hf_sfx.sync_tags(d, cues) is True
    assert (d / "index.html").read_text(encoding="utf-8") == text
    # sfx を空にすれば、ブロックごと消えて元の HTML に戻る
    assert hf_sfx.sync_tags(d, []) is True
    assert (d / "index.html").read_text(encoding="utf-8") == INDEX


def test_sync_keeps_crlf(tmp_path):
    d = _comp(tmp_path, index=INDEX.replace("\n", "\r\n"))
    cues, _, _ = hf_sfx.resolve(MOTION)
    hf_sfx.sync_tags(d, cues)
    raw = (d / "index.html").read_bytes()
    assert b"\r\n" in raw and not re.search(rb"(?<!\r)\n", raw)


def test_prepare_through_hf_makes_wavs_and_tags(tmp_path, monkeypatch):
    hf_root = tmp_path / "hfroot"
    (hf_root / "node_modules/gsap/dist").mkdir(parents=True)
    (hf_root / "node_modules/gsap/dist/gsap.min.js").write_text("/*gsap*/", encoding="utf-8")
    monkeypatch.setattr(config, "HF_DIR", hf_root)
    d = _comp(tmp_path)
    made = hf.prepare(d)
    assert made == ["vendor/gsap.min.js", "assets/sfx/tick_a.wav", "assets/sfx/rise.wav", "index.html"]
    assert hf.prepare(d) == []
    assert hf_sfx.check(d, MOTION, 5.0) == []
    # sfx を motion.json から消した後の準備で、<audio> が外れる
    m = dict(MOTION)
    m["sfx"] = []
    (d / "motion.json").write_text(json.dumps(m), encoding="utf-8")
    assert hf.prepare(d) == ["index.html"] and "<audio" not in (d / "index.html").read_text(encoding="utf-8")


def test_no_sfx_means_no_changes(tmp_path):
    d = _comp(tmp_path, motion={"profile": "insert", "fit": {"sec": 5}})
    assert hf_sfx.prepare(d, {"profile": "insert"}) == []
    assert hf_sfx.check(d, {"profile": "insert"}, 5.0) == []
    assert (d / "index.html").read_text(encoding="utf-8") == INDEX


# ── 検査 ──

def test_check_flags_missing_and_mismatched_tags(tmp_path):
    d = _comp(tmp_path)
    issues = hf_sfx.check(d, MOTION, 5.0)
    assert _codes(issues) == ["sfx_tag_missing"] * 3 and all(i["level"] == "error" for i in issues)
    cues, _, _ = hf_sfx.resolve(MOTION)
    hf_sfx.sync_tags(d, cues)
    assert hf_sfx.check(d, MOTION, 5.0) == []
    p = d / "index.html"
    p.write_text(p.read_text(encoding="utf-8").replace('id="sfx-b" src="assets/sfx/tick_a.wav"',
                                                       'id="sfx-b" crossorigin src="assets/sfx/tick_a.wav"'), encoding="utf-8")
    assert _codes(hf_sfx.check(d, MOTION, 5.0)) == ["sfx_tag_mismatch"]


def test_check_warns_about_past_end_and_insert(tmp_path):
    d = _comp(tmp_path)
    cues, _, _ = hf_sfx.resolve(MOTION)
    hf_sfx.sync_tags(d, cues)
    issues = hf_sfx.check(d, MOTION, 2.5)       # 打点 3.0 の riser は 2.0〜3.0 秒＝2.5 秒を越える
    assert [(i["code"], i["level"]) for i in issues] == [("sfx_past_end", "warning")]
    insert = {**MOTION, "profile": "insert"}
    assert ("insert_has_sfx", "warning") in [(i["code"], i["level"]) for i in hf_sfx.check(d, insert, 5.0)]


# ── 見本 ──

def test_sample_person_intro_sfx_is_in_sync_and_clean(tmp_path):
    src = config.HF_SAMPLES_DIR / "person_intro_sfx"
    if not src.is_dir():
        pytest.skip("見本が無い環境")
    d = tmp_path / "person_intro_sfx"
    d.mkdir()
    for name in ("index.html", "motion.json"):
        shutil.copyfile(src / name, d / name)
    motion = hf.read_motion(d)
    before = (d / "index.html").read_text(encoding="utf-8")
    made = hf_sfx.prepare(d, motion)
    assert "index.html" not in made                                  # 置いてある <audio> は motion.json と同期済み
    assert (d / "index.html").read_text(encoding="utf-8") == before
    assert all(m.startswith("assets/sfx/") for m in made) and len(made) == len(motion["sounds"])
    assert [i for i in hf_sfx.check(d, motion, 5.0) if i["level"] == "error"] == []
    cues, _, issues = hf_sfx.resolve(motion)
    assert issues == [] and len(cues) == 14
    assert {c["kind"] for c in cues} == {"tick", "whoosh", "impact"}
