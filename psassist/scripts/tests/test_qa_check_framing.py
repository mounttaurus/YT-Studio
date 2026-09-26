"""S4（Docs/SUBLINE_PLAN.md §8-2）: FRAMING_MISMATCH がズーム後の実効画角で判定されることを固定する。

psassist には自動テストが無い（Photoshop実行そのものの検証は別）。ここは
qa_check.py の純粋関数（PSD/画像を一切読まない）だけを対象にする。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qa_check


def test_effective_framing_no_zoom_is_unchanged():
    assert qa_check.effective_framing("waist_up", None) == "waist_up"
    assert qa_check.effective_framing("waist_up", 1.0) == "waist_up"


def test_effective_framing_shifts_toward_closeup_by_zoom_steps():
    """1.3倍(1段)でbust、1.69倍(2段)でface_closeupへ寄る（付録Cの試作と同じ換算）。"""
    assert qa_check.effective_framing("waist_up", 1.3) == "bust"
    assert qa_check.effective_framing("waist_up", 1.69) == "face_closeup"


def test_effective_framing_never_goes_past_face_closeup():
    """既に一番寄りのframingは、それ以上寄れない（配列の先頭で止まる）。"""
    assert qa_check.effective_framing("face_closeup", 1.3) == "face_closeup"


def test_effective_framing_unknown_framing_passthrough():
    assert qa_check.effective_framing(None, 1.3) is None
    assert qa_check.effective_framing("unknown_label", 1.3) == "unknown_label"


def test_framing_mismatch_would_false_positive_without_zoom_correction():
    """このテストが固定したいのは「拡大した分を考慮しないと誤検知になる」その事実。

    腰上(waist_up)の背景を1.3倍(bust相当)に拡大した行で、実測shotがbustなら
    ズーム込みの判定は一致するが、ズーム抜きの判定（生のframing）は不一致になる。
    """
    zoom = 1.3
    raw_framing = "waist_up"
    measured_shot = "bust"
    assert qa_check.FRAMING_OF_SHOT.get(measured_shot) != raw_framing, \
        "ズームを考慮しない判定は本来ズレるはず（誤検知の再現）"
    assert qa_check.FRAMING_OF_SHOT.get(measured_shot) == qa_check.effective_framing(raw_framing, zoom)
