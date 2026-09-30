"""§6-2（Docs/LINE_WORKBENCH_PLAN.md）: 行ごとの合成記録・手直しの検知・退避・再合成の既定スキップ。

Photoshop は使わない（bridge は差し替える）。PSD の中身は指紋を取れれば何でもよいのでダミーのバイト列。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_records as br  # noqa: E402


def _psd(psa, lid, data=b"auto"):
    d = os.path.join(psa, "psd_final")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "panel_%s.psd" % lid), "wb") as fh:
        fh.write(data)


def test_legacy_psds_are_initialized_as_automatic_and_not_edited(tmp_path):
    psa = str(tmp_path)
    _psd(psa, "line_001"); _psd(psa, "line_002")
    assert br.init_missing(psa) == ["line_001", "line_002"]
    assert br.init_missing(psa) == [], "冪等（記録済みは触らない）"
    rec = br.load(psa)["records"]["line_001"]
    assert rec["source"] == "initialized" and rec["lang"] == "ja" and len(rec["psd_sha256"]) == 64
    assert br.edited_lines(psa) == set()


def test_edit_is_detected_by_content_not_by_mtime(tmp_path):
    psa = str(tmp_path)
    _psd(psa, "line_001", b"auto"); _psd(psa, "line_002", b"auto")
    br.init_missing(psa)
    p1 = br.psd_path(psa, "line_001")
    p2 = br.psd_path(psa, "line_002")
    st = os.stat(p2)
    os.utime(p2, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))      # 時刻だけ変わった（同期ソフト等）
    with open(p1, "wb") as fh:
        fh.write(b"hand!")                                                    # 中身が変わった（Photoshopで保存）
    assert br.edited_lines(psa) == {"line_001"}, "時刻だけの変化は手直しではない"
    assert br.edited_lines(psa, ["line_002"]) == set()


def test_same_size_edit_is_still_caught(tmp_path):
    psa = str(tmp_path)
    _psd(psa, "line_001", b"AAAA")
    br.init_missing(psa)
    p = br.psd_path(psa, "line_001")
    st = os.stat(p)
    with open(p, "wb") as fh:
        fh.write(b"BBBB")                                                     # 同じ大きさ
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert br.edited_lines(psa) == {"line_001"}


def test_record_built_resets_the_baseline_and_never_drops_other_lines(tmp_path):
    psa = str(tmp_path)
    _psd(psa, "line_001"); _psd(psa, "line_002")
    br.init_missing(psa)
    with open(br.psd_path(psa, "line_001"), "wb") as fh:
        fh.write(b"hand!")
    assert br.record_built(psa, ["line_001", "line_404"]) == ["line_001"], "PSDが無い行は記録しない"
    assert br.edited_lines(psa) == set(), "再合成した時点が新しい基準"
    assert set(br.load(psa)["records"]) == {"line_001", "line_002"}, "他の行の記録を消さない"
    assert br.load(psa)["records"]["line_001"]["source"] == "build"


def test_lines_without_a_record_are_not_reported_as_edited(tmp_path):
    psa = str(tmp_path)
    _psd(psa, "line_001")
    assert br.edited_lines(psa, ["line_001"]) == set(), "記録が無い＝判定不能（手直し扱いしない）"


def test_backup_keeps_the_original_and_never_overwrites(tmp_path):
    psa = str(tmp_path)
    _psd(psa, "line_001", b"hand-edited")
    a = br.backup(psa, "line_001")
    b = br.backup(psa, "line_001")
    assert a != b and os.path.dirname(a).endswith(os.path.join("psd_final", "_backup"))
    assert open(a, "rb").read() == b"hand-edited"
    assert br.backup(psa, "line_404") is None


# ── host_worker: 再合成の既定スキップ・退避 ─────────────────────────────
def _import_host_worker():
    """host_worker（と export_png）は import 時に sys.stdout を包み直す。外したラッパーが GC で閉じると
    pytest の出力が壊れるので、import の間だけ「包み直し」を無効にする。"""
    import io
    real = io.TextIOWrapper
    io.TextIOWrapper = lambda buf, *a, **k: sys.stdout
    try:
        import host_worker as hw
    finally:
        io.TextIOWrapper = real
    return hw


def _worker(monkeypatch, tmp_path):
    hw = _import_host_worker()
    ep = tmp_path / "ep01"
    psa = ep / "psassist"
    psa.mkdir(parents=True)
    (psa / "panel_plan.json").write_text(json.dumps({"panels": [{"line_id": "line_001"}, {"line_id": "line_002"}, {"line_id": "line_003"}]}))
    for lid in ("line_001", "line_002"):
        _psd(str(psa), lid)
    bubbles = tmp_path / "bubbles.psd"
    bubbles.write_bytes(b"x")
    monkeypatch.setenv("PSA_BUBBLES_PSD", str(bubbles))
    calls = []

    def fake_run(argv, env, log, label, on_line=None):
        lines = argv[argv.index("--lines") + 1].split(",")
        calls.append(lines)
        for lid in lines:                                                     # bridge が PSD を書き直す
            _psd(str(psa), lid, b"rebuilt-" + lid.encode())
        return 0
    monkeypatch.setattr(hw, "_run_script", fake_run)
    return hw, str(ep), str(psa), calls


def test_rebuild_skips_hand_edited_lines_by_default(monkeypatch, tmp_path):
    hw, ep, psa, calls = _worker(monkeypatch, tmp_path)
    br.init_missing(psa)
    with open(br.psd_path(psa, "line_001"), "wb") as fh:
        fh.write(b"hand!")                                                    # 001 を手直し
    log = []
    res = hw.run_build_panel_job(ep, {"job_id": "j1"}, log)                   # lines 省略＝全件
    assert calls == [["line_002", "line_003"]], "手直し済みの001は飛ばす"
    assert res["skipped_edited"] == ["line_001"] and res["lines"] == 2 and res["total"] == 2
    assert open(br.psd_path(psa, "line_001"), "rb").read() == b"hand!", "手直しが残っている"
    assert br.edited_lines(psa) == {"line_001"}, "飛ばした行は今も手直し済み"
    assert not os.path.isdir(os.path.join(psa, "psd_final", "_backup"))
    assert br.load(psa)["records"]["line_003"]["source"] == "build", "作った行は記録される"


def test_rebuild_including_edited_backs_up_first(monkeypatch, tmp_path):
    hw, ep, psa, calls = _worker(monkeypatch, tmp_path)
    br.init_missing(psa)
    with open(br.psd_path(psa, "line_001"), "wb") as fh:
        fh.write(b"hand!")
    res = hw.run_build_panel_job(ep, {"job_id": "j2", "lines": ["line_001"], "args": {"include_edited": True}}, [])
    assert calls == [["line_001"]]
    assert len(res["backed_up"]) == 1 and open(res["backed_up"][0], "rb").read() == b"hand!", "元のPSDを退避してから上書き"
    assert "skipped_edited" not in res
    assert br.edited_lines(psa) == set(), "再合成したので新しい基準になる"


def test_skipping_everything_never_falls_back_to_all(monkeypatch, tmp_path):
    """手直し済みだけを指定して全部飛ばした時、空リストを bridge に渡さない（空＝全件になってしまう）。"""
    hw, ep, psa, calls = _worker(monkeypatch, tmp_path)
    br.init_missing(psa)
    with open(br.psd_path(psa, "line_001"), "wb") as fh:
        fh.write(b"hand!")
    res = hw.run_build_panel_job(ep, {"job_id": "j3", "lines": ["line_001"]}, [])
    assert calls == [] and res["lines"] == 0 and res["skipped_edited"] == ["line_001"]


# ── open_psd: 開けるのは psd_final/panel_{line_id}.psd だけ（2026-09-30） ──────────────
def test_open_target_is_confined_to_psd_final(tmp_path):
    hw = _import_host_worker()
    ep = tmp_path / "ep01"
    _psd(str(ep / "psassist"), "line_001")
    assert hw.resolve_open_target(str(ep), "line_001").endswith(os.path.join("psd_final", "panel_line_001.psd"))
    for bad in ("../x", "line_001/../../a", "", "a b"):
        try:
            hw.resolve_open_target(str(ep), bad)
        except ValueError:
            continue
        raise AssertionError("開けてはいけない: %r" % bad)
    try:
        hw.resolve_open_target(str(ep), "line_999")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("無いPSDは開かない")


def test_open_psd_job_uses_startfile_or_explorer(monkeypatch, tmp_path):
    hw = _import_host_worker()
    ep = tmp_path / "ep01"
    _psd(str(ep / "psassist"), "line_001")
    opened, popen = [], []
    monkeypatch.setattr(hw.os, "name", "nt")
    monkeypatch.setattr(hw.os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(hw.subprocess, "Popen", lambda argv: popen.append(argv))
    log = []
    r = hw.run_open_psd_job(str(ep), {"lines": ["line_001"]}, log)
    assert r["reveal"] is False and opened == [r["opened"]]
    r = hw.run_open_psd_job(str(ep), {"lines": ["line_001"], "args": {"reveal": True}}, log)
    assert popen == [["explorer", "/select,", r["opened"]]]
