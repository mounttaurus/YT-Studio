"""HyperFrames のコンポジションの検査のうち、CLI を使わずに決められるもの（Docs/MOTION_UNIFIED_PLAN.md §8）。

- network: 描画中にネットワークを使わない（M3）。`http(s)://` を指す src・href・url()・@import と、
  fetch・XMLHttpRequest・WebSocket・動的 import を止める。
- length: ルートの data-duration と motion.json の fit が一致する（インサートは行の長さに合わせる）。

結果は {code, level, message, file?, line?} の並び。level は error（描かない）か warning。
"""
from __future__ import annotations

import re
from pathlib import Path

FPS = 30
# 調べるファイル。CLI の出力・コンテナが置く物は見ない（vendor は公開済みのライブラリ）
SCAN_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".svg"}
SKIP_DIRS = {"vendor", "node_modules", "snapshots", "renders", ".hyperframes", "history"}

_NETWORK_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("network_url", re.compile(r"""(?:src|href|srcset|poster|data-composition-src)\s*=\s*["']\s*(?:https?:)?//""", re.I),
     "外部の URL を読んでいる（素材・ライブラリはプロジェクトの中に置く）"),
    ("network_url", re.compile(r"""url\(\s*["']?\s*(?:https?:)?//""", re.I), "CSS の url() が外部を指している"),
    ("network_url", re.compile(r"""@import\s+(?:url\()?\s*["']?\s*(?:https?:)?//""", re.I), "CSS の @import が外部を指している"),
    ("network_api", re.compile(r"""\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\s*\(|\bEventSource\s*\(|navigator\.sendBeacon"""),
     "描画中に通信する API を使っている"),
    ("network_api", re.compile(r"""\bimport\s*\(\s*["'](?:https?:)?//"""), "外部のモジュールを動的に読んでいる"),
]


def _issue(code: str, message: str, level: str = "error", file: str | None = None, line: int | None = None) -> dict:
    out: dict = {"code": code, "level": level, "message": message}
    if file is not None:
        out["file"] = file
    if line is not None:
        out["line"] = line
    return out


def _scan_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SCAN_SUFFIXES:
            continue
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        yield p, rel.as_posix()


def check_network(root: Path) -> list[dict]:
    issues: list[dict] = []
    for p, rel in _scan_files(root):
        text = p.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            for code, pat, msg in _NETWORK_RULES:
                if pat.search(line):
                    issues.append(_issue(code, msg, file=rel, line=n))
    return issues


_ROOT_RE = re.compile(r"<[a-zA-Z][^>]*\bdata-composition-id\s*=\s*[\"'][^\"']+[\"'][^>]*>", re.S)
_DUR_RE = re.compile(r"\bdata-duration\s*=\s*[\"']\s*([0-9]+(?:\.[0-9]+)?)\s*[\"']")
_ID_RE = re.compile(r"\bdata-composition-id\s*=\s*[\"']([^\"']+)[\"']")


def root_info(index_html: str) -> dict:
    """index.html の最初のルート（data-composition-id を持つ要素）の id と data-duration（秒）。"""
    m = _ROOT_RE.search(index_html)
    if not m:
        return {"id": None, "duration": None}
    tag = m.group(0)
    dur = _DUR_RE.search(tag)
    return {"id": _ID_RE.search(tag).group(1), "duration": float(dur.group(1)) if dur else None}


def root_open_end(index_html: str) -> int | None:
    """index.html の最初のルート（data-composition-id を持つ要素）の開始タグの終わりの位置。"""
    m = _ROOT_RE.search(index_html)
    return m.end() if m else None


def check_length(root: Path, motion: dict | None) -> tuple[list[dict], float | None]:
    """返り値: (指摘, ルートの秒)。fit.sec と1フレーム以上ずれていれば誤り。"""
    index = root / "index.html"
    if not index.is_file():
        return [_issue("missing_index", "index.html が無い")], None
    info = root_info(index.read_text(encoding="utf-8", errors="replace"))
    issues: list[dict] = []
    if info["id"] is None:
        return [_issue("missing_root", "data-composition-id を持つルートが無い", file="index.html")], None
    dur = info["duration"]
    if dur is None:
        issues.append(_issue("missing_duration", "ルートに data-duration が無い（長さを固定する）", file="index.html"))
    fit = (motion or {}).get("fit") or {}
    if "sec" in fit and dur is not None:
        want = float(fit["sec"])
        if abs(want - dur) > 1.0 / FPS + 1e-9:
            issues.append(_issue("length_mismatch", f"ルートの data-duration（{dur:g} 秒）が fit.sec（{want:g} 秒）と合わない",
                                 file="index.html"))
    elif "line_id" in fit:
        issues.append(_issue("fit_line_unsupported", "fit.line_id（行の音声の長さ）は I1 で対応する。今は長さを確かめていない",
                             level="warning"))
    if motion is None:
        issues.append(_issue("missing_motion_json", "motion.json（添え書き）が無い", level="warning"))
    return issues, dur
