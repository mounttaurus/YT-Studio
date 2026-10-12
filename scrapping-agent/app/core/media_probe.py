"""素材ファイルの種類・尺・解像度を、**中身**で調べる（Docs/BROLL_PLAN.md B1・BR4）。

拡張子だけに頼らない（`.jpg` の名前の PNG・名前だけ動画のファイルを見分ける）。
- 種類: 先頭のバイト列（PNG・JPEG・WebP・MP4/MOV・WebM）。
- 画像: Pillow で幅・高さ。
- 動画: ffprobe があればそれ。無ければ MP4/MOV の `moov`（`mvhd` の尺・`tkhd` の幅と高さ）を自前で読む。
  WebM・MKV は ffprobe が無いと尺・解像度が分からない（None）。
このコンテナには ffmpeg を入れていない（イメージを増やさない）ので、自前の読み取りが既定の道。
"""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

from PIL import Image

# 先頭バイトの判定 → (media_type, 正しい拡張子)
_SNIFF_HEAD = 4096


def sniff(path: Path) -> tuple[str, str] | None:
    """(media_type, ext)。分からなければ None。media_type は photo / video。"""
    with path.open("rb") as f:
        head = f.read(_SNIFF_HEAD)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "photo", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "photo", ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "photo", ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return "video", (".mov" if brand == b"qt  " else ".mp4")
    if head[4:8] in (b"moov", b"mdat", b"wide", b"free"):      # 古い QuickTime
        return "video", ".mov"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", ".webm"
    return None


def _boxes(f, start: int, end: int):
    """MP4 の box を (type, body_start, body_end) で順に返す。"""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size, typ = struct.unpack(">I4s", hdr)
        header = 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end + 1:
            return
        yield typ.decode("latin-1"), pos + header, pos + size
        pos += size


def _mp4_info(path: Path) -> dict:
    """moov から尺（mvhd）と最初の映像トラックの幅・高さ（tkhd）を読む。"""
    out: dict = {}
    size = path.stat().st_size
    with path.open("rb") as f:
        moov = next(((s, e) for t, s, e in _boxes(f, 0, size) if t == "moov"), None)
        if moov is None:
            return out
        for t, s, e in _boxes(f, *moov):
            if t == "mvhd":
                f.seek(s)
                ver = f.read(1)[0]
                f.seek(s + 4)
                if ver == 1:
                    f.seek(s + 4 + 16)
                    scale, dur = struct.unpack(">IQ", f.read(12))
                else:
                    f.seek(s + 4 + 8)
                    scale, dur = struct.unpack(">II", f.read(8))
                if scale:
                    out["duration_sec"] = round(dur / scale, 3)
            elif t == "trak" and "width" not in out:
                for t2, s2, e2 in _boxes(f, s, e):
                    if t2 == "tkhd":
                        f.seek(s2)
                        ver = f.read(1)[0]
                        f.seek(e2 - 8)
                        w, h = struct.unpack(">II", f.read(8))
                        w, h = w >> 16, h >> 16
                        if w and h and ver in (0, 1):
                            out["width"], out["height"] = w, h
    return out


def _ffprobe(path: Path) -> dict:
    exe = shutil.which("ffprobe")
    if not exe:
        return {}
    try:
        cp = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration:stream=width,height,codec_type",
                             "-of", "json", str(path)], capture_output=True, text=True, timeout=60)
        data = json.loads(cp.stdout or "{}")
    except (subprocess.SubprocessError, ValueError, OSError):
        return {}
    out: dict = {}
    dur = (data.get("format") or {}).get("duration")
    if dur not in (None, "N/A"):
        try:
            out["duration_sec"] = round(float(dur), 3)
        except ValueError:
            pass
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and s.get("width") and s.get("height"):
            out["width"], out["height"] = int(s["width"]), int(s["height"])
            break
    return out


def probe(path: Path) -> dict:
    """{media_type, ext, duration_sec, width, height, resolution}。種類が分からなければ media_type は None。"""
    kind = sniff(path)
    if kind is None:
        return {"media_type": None, "ext": path.suffix.lower(), "duration_sec": None, "width": None, "height": None,
                "resolution": None}
    media_type, ext = kind
    info: dict = {}
    if media_type == "photo":
        try:
            with Image.open(path) as im:
                info["width"], info["height"] = im.size
        except Exception:                                   # noqa: BLE001 — 壊れた画像は寸法なしで登録できるようにはしない
            return {"media_type": None, "ext": ext, "duration_sec": None, "width": None, "height": None, "resolution": None,
                    "error": "画像として読めない"}
        info["duration_sec"] = None
    else:
        info = _ffprobe(path) or (_mp4_info(path) if ext in (".mp4", ".mov") else {})
        if "duration_sec" not in info:
            info["duration_sec"] = None
    w, h = info.get("width"), info.get("height")
    return {"media_type": media_type, "ext": ext, "duration_sec": info.get("duration_sec"), "width": w, "height": h,
            "resolution": f"{w}x{h}" if w and h else None}
