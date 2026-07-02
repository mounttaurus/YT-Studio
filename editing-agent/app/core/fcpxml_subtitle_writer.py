"""
tts.json から DaVinci Resolve 用の字幕FCPXML(Text+)を直接生成する。
Docs/06_editing.md「字幕FCPXML（Text+）オプション」/ memory `fcpxml-resolve-subtitle-fidelity` 参照。

重要（2026-06-19 実機確定）:
- OTIOのfcpxmlアダプタは使わない（0.17に同梱されず、titleも吐かない）。lxml/ElementTreeで直接書く。
- Apple "Basic Title" effect を参照する <title> を出すと Resolve が Text+ に変換する。
- 焼ける: fontColor/fontSize/bold/italic/strokeColor/strokeWidth/位置(adjust-transform)。
- 焼けない（書かない）: Drop Shadow / alignment=left / param-key位置 / Outside Only。
- 位置: <adjust-transform position="0 adjustY"> が Resolve Transform Position Y に Y=adjustY*(height/100) で焼ける。
  UI/データは Resolve Transform座標（センター=0・下が−、既定 -250）で持ち、ここで ÷(height/100) して書く。
"""
import xml.etree.ElementTree as ET

# Apple "Basic Title" generator。ResolveがこのeffectをText+に変換する。
BASIC_TITLE_UID = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
FCPXML_VERSION = "1.8"

# 字幕スタイルの既定（config.subtitle_style が無い/欠落キーのフォールバック）
DEFAULT_STYLE = {
    "position_y": -250,  # Resolve Transform座標（センター=0・下が負）。下三分の一の目安。
    "default": {
        "font": "Yu Gothic",
        "font_size": 72,
        "color": [1.0, 1.0, 1.0, 1.0],
        "bold": False,
        "italic": False,
        "stroke_color": [0.0, 0.0, 0.0, 1.0],
        "stroke_width": 3,
    },
    "per_speaker": {},
}


def _fcp_time(sec: float, fps: int) -> str:
    """秒をフレーム整合のFCPXML有理数時刻へ。分母は frameDuration と同系(fps*100)。"""
    return f"{round(sec * fps) * 100}/{fps * 100}s"


def _color_str(rgba) -> str:
    return " ".join(_num(c) for c in rgba)


def _num(v) -> str:
    """floatを冗長な小数なしで文字列化（1.0->"1"、0.9->"0.9"）。"""
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


def _resolve_style(style: dict | None, speaker_id: str) -> dict:
    """default に per_speaker[speaker_id] を上書きマージした最終スタイルを返す。"""
    style = style or {}
    base = {**DEFAULT_STYLE["default"], **(style.get("default") or {})}
    override = (style.get("per_speaker") or {}).get(speaker_id) or {}
    base.update(override)
    return base


def _text_style_attrs(s: dict) -> dict:
    attrs = {
        "font": str(s.get("font", "Yu Gothic")),
        "fontSize": str(s.get("font_size", 72)),
        "fontColor": _color_str(s.get("color", [1, 1, 1, 1])),
        "bold": "1" if s.get("bold") else "0",
        "italic": "1" if s.get("italic") else "0",
        "alignment": "center",
    }
    stroke_width = s.get("stroke_width", 0) or 0
    if stroke_width > 0:
        attrs["strokeColor"] = _color_str(s.get("stroke_color", [0, 0, 0, 1]))
        attrs["strokeWidth"] = str(stroke_width)
    return attrs


def build_fcpxml(tts: dict, style: dict | None = None, fps: int = 30,
                 width: int = 1920, height: int = 1080) -> str:
    """字幕Text+のみのFCPXML文字列を返す。"""
    from app.core import srt_writer

    lines = srt_writer.subtitle_lines(tts)
    total_sec = max((ln["end_sec"] for ln in lines), default=0.0)

    position_y = (style or {}).get("position_y", DEFAULT_STYLE["position_y"])
    adjust_y = position_y / (height / 100.0)  # Transform Y = adjustY * (height/100)

    fcpxml = ET.Element("fcpxml", version=FCPXML_VERSION)
    resources = ET.SubElement(fcpxml, "resources")
    ET.SubElement(
        resources, "format",
        id="r1", name=f"FFVideoFormat{height}p{fps}",
        frameDuration=f"100/{fps * 100}s", width=str(width), height=str(height),
        colorSpace="1-1-1 (Rec. 709)",
    )
    ET.SubElement(resources, "effect", id="r2", name="Basic Title", uid=BASIC_TITLE_UID)

    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", name="YT-Studio Subtitles")
    project = ET.SubElement(event, "project", name="subtitles")
    sequence = ET.SubElement(
        project, "sequence",
        format="r1", duration=_fcp_time(total_sec, fps), tcStart="0s", tcFormat="NDF",
        audioLayout="stereo", audioRate="48k",
    )
    spine = ET.SubElement(sequence, "spine")
    gap = ET.SubElement(spine, "gap", name="gap", offset="0s",
                        duration=_fcp_time(total_sec, fps), start="0s")

    for ln in lines:
        dur = ln["end_sec"] - ln["start_sec"]
        if dur <= 0:
            continue
        resolved = _resolve_style(style, ln["speaker_id"])
        ts_id = f"ts{ln['index']}"

        title = ET.SubElement(
            gap, "title",
            ref="r2", lane="1", offset=_fcp_time(ln["start_sec"], fps),
            name=ln["text"][:40], start="0s", duration=_fcp_time(dur, fps),
        )
        ET.SubElement(title, "adjust-transform", position=f"0 {_num(round(adjust_y, 4))}")
        text_el = ET.SubElement(title, "text")
        ET.SubElement(text_el, "text-style", ref=ts_id).text = ln["text"]
        tsd = ET.SubElement(title, "text-style-def", id=ts_id)
        ET.SubElement(tsd, "text-style", **_text_style_attrs(resolved))

    tree = ET.ElementTree(fcpxml)
    ET.indent(tree, space="  ")
    body = ET.tostring(fcpxml, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body + "\n"


def count_titles(tts: dict) -> int:
    from app.core import srt_writer
    return sum(1 for ln in srt_writer.subtitle_lines(tts) if ln["end_sec"] - ln["start_sec"] > 0)
