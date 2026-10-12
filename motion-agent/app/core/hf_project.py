"""話（プロジェクト・エピソード）の文脈が要る検査（Docs/MOTION_UNIFIED_PLAN.md §5-6・§8-2・§8-3・I1）。

- fit.line_id: その行の音声の実測（tts.json の `end − start`）にルートの長さが合っているか。尺ずれの一覧。
- 出典: 素材（motion.json の materials）の `source` が揃っているか（箱の asset_id は B1 の後）。
- 繰り返し: 同じ組み合わせ（ブロックと出し方の指紋）を使い回していないか（止めずに警告）。
話の外（見本 samples/）は文脈が無いので、この検査は何も言わない（行の長さは「未解決」の警告だけ）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.core import hf_blocks, hf_checks

FPS = hf_checks.FPS
SOURCE_KINDS = ("self", "public_domain", "cc0", "cc_by", "cc_by_sa", "licensed", "stock", "episode_broll", "unknown")
ATTRIBUTION_KINDS = ("cc_by", "cc_by_sa")
_EP_RE = re.compile(r"^ep(\d{2,})$")


def _issue(code: str, message: str, level: str = "error", file: str | None = "motion.json") -> dict:
    out = {"code": code, "level": level, "message": message}
    if file:
        out["file"] = file
    return out


def context(d: Path) -> dict | None:
    """コンポジションのフォルダ → {project_dir, episode_dir, project_id, episode, comp_id}。話の外なら None。"""
    parts = d.resolve().parts
    # .../projects/{pid}/episodes/epNN/motion/hf/{comp_id}
    if len(parts) < 7 or parts[-2] != "hf" or parts[-3] != "motion":
        return None
    m = _EP_RE.match(parts[-4])
    if not m or parts[-5] != "episodes":
        return None
    ep_dir = d.resolve().parents[2]
    project_dir = ep_dir.parent.parent
    return {"project_dir": project_dir, "episode_dir": ep_dir, "project_id": project_dir.name,
            "episode": int(m.group(1)), "comp_id": d.name}


def load_tts(ctx: dict) -> dict | None:
    for p in (ctx["episode_dir"] / "tts.json", ctx["project_dir"] / "tts.json"):
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                return None
    return None


def line_seconds(ctx: dict, line_id: str, tts: dict | None = None) -> float | None:
    """行の音声の長さ（実測）。timeline の end − start を先に、無ければ audio_files の duration_sec。"""
    tts = tts if tts is not None else load_tts(ctx)
    if not tts:
        return None
    for e in tts.get("timeline") or []:
        if e.get("line_id") == line_id and e.get("end_sec") is not None and e.get("start_sec") is not None:
            return round(float(e["end_sec"]) - float(e["start_sec"]), 3)
    for e in tts.get("audio_files") or []:
        if e.get("line_id") == line_id and e.get("duration_sec") is not None:
            return round(float(e["duration_sec"]), 3)
    return None


def line_order(ctx: dict, tts: dict | None = None) -> dict[str, int]:
    tts = tts if tts is not None else load_tts(ctx)
    order: dict[str, int] = {}
    for i, e in enumerate((tts or {}).get("timeline") or []):
        if e.get("line_id") not in order:
            order[e["line_id"]] = i
    return order


# ── 長さ（fit.line_id） ──

def fit_info(d: Path, motion: dict | None, duration: float | None) -> dict | None:
    """fit.line_id があれば {line_id, expected_sec, duration, drift_sec}（尺ずれの一覧用）。"""
    fit = (motion or {}).get("fit") or {}
    lid = fit.get("line_id")
    if not lid:
        return None
    ctx = context(d)
    exp = line_seconds(ctx, lid) if ctx else None
    return {"line_id": lid, "expected_sec": exp, "duration": duration,
            "drift_sec": None if exp is None or duration is None else round(duration - exp, 3)}


def check_fit(d: Path, motion: dict | None, duration: float | None) -> list[dict]:
    info = fit_info(d, motion, duration)
    if info is None:
        return []
    if info["expected_sec"] is None:
        return [_issue("fit_line_unresolved", f"fit.line_id {info['line_id']!r} の音声の長さが分からない"
                                              "（話の外か、tts.json に無い）。長さは確かめていない", level="warning")]
    if info["duration"] is not None and abs(info["drift_sec"]) > 1.0 / FPS + 1e-9:
        return [_issue("length_mismatch", f"ルートの data-duration（{info['duration']:g} 秒）が行 {info['line_id']} の音声"
                                          f"（{info['expected_sec']:g} 秒・実測）と合わない（{info['drift_sec']:+g} 秒）。"
                                          "インサートは行の長さに合わせる", file="index.html")]
    return []


# ── 出典 ──

def check_sources(d: Path, motion: dict | None) -> list[dict]:
    mats = (motion or {}).get("materials")
    issues: list[dict] = []
    declared: set[str] = set()
    if isinstance(mats, dict):
        for name, spec in mats.items():
            if not isinstance(spec, dict):
                issues.append(_issue("material_bad", f"materials.{name} は辞書"))
                continue
            path = spec.get("path")
            if isinstance(path, str):
                declared.add(path)
            src = spec.get("source")
            if isinstance(src, str):         # 旧形式（H1 の見本）: 文字列の出典
                issues.append(_issue("source_legacy", f"materials.{name}.source は {{kind, author, url, license}} の辞書にする",
                                     level="warning"))
                continue
            if not isinstance(src, dict) or not src.get("kind"):
                issues.append(_issue("source_missing", f"materials.{name}: 出典（source.kind）が無い", level="warning"))
                continue
            kind = src["kind"]
            if kind not in SOURCE_KINDS:
                issues.append(_issue("source_kind", f"materials.{name}: source.kind は {SOURCE_KINDS} のどれか: {kind!r}"))
            elif kind == "unknown":
                issues.append(_issue("source_unknown", f"materials.{name}: 出典が unknown（クレジットを残せない）", level="warning"))
            elif kind in ATTRIBUTION_KINDS:
                miss = [k for k in ("author", "url", "license") if not src.get(k)]
                if miss:
                    issues.append(_issue("source_incomplete", f"materials.{name}: {kind} は {miss} が要る（表示義務）"))
    # ブロックの素材の変数が指すファイルが、materials に宣言されているか
    index = d / "index.html"
    if index.is_file():
        dic = hf_blocks.load_all()
        for h in hf_blocks.hosts(index.read_text(encoding="utf-8", errors="replace")):
            b = dic.get(h["block"] or "")
            if not b or not h["vars"]:
                continue
            for mt in b["meta"].get("materials") or []:
                path = h["vars"].get(mt["var"])
                if isinstance(path, str) and path and path not in declared:
                    issues.append(_issue("material_undeclared", f"{h['block']}.{mt['var']} の素材 {path} が motion.json の materials に無い"
                                                                "（出典を残す）", level="warning"))
    return issues


# ── 繰り返し ──

def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0


def _episode_prints(project_dir: Path, episode: int) -> list[dict]:
    base = project_dir / "episodes" / f"ep{episode:02d}" / "motion" / "hf"
    out = []
    if base.is_dir():
        for c in sorted(base.iterdir()):
            if (c / "index.html").is_file():
                motion = {}
                mp = c / "motion.json"
                if mp.is_file():
                    try:
                        motion = json.loads(mp.read_text(encoding="utf-8"))
                    except ValueError:
                        motion = {}
                out.append({"comp_id": c.name, "fingerprint": hf_blocks.fingerprint(c),
                            "line_id": (motion.get("fit") or {}).get("line_id"), "profile": motion.get("profile")})
    return out


def check_repeat(d: Path, fingerprint: list[str], min_overlap: float = 0.5) -> list[dict]:
    """同じ組み合わせの繰り返し（警告・止めない）。話の外・用途が insert 以外・指紋が空なら何も言わない。"""
    ctx = context(d)
    if ctx is None or not fingerprint:
        return []
    issues: list[dict] = []
    this = ctx["comp_id"]
    now = [p for p in _episode_prints(ctx["project_dir"], ctx["episode"]) if p["profile"] in (None, "insert")]
    same = [p for p in now if p["comp_id"] != this and p["fingerprint"] == fingerprint]
    if len(same) >= 2:
        issues.append(_issue("repeat_in_episode", f"同じ組み合わせ（{', '.join(fingerprint)}）がこの話で3回目: "
                                                  f"{', '.join(p['comp_id'] for p in same)} と同じ", level="warning", file="index.html"))
    order = line_order(ctx)
    me = next((p for p in now if p["comp_id"] == this), None)
    if me and me.get("line_id") in order:
        pos = sorted((order[p["line_id"]], p) for p in now if p.get("line_id") in order)
        idx = next((i for i, (_, p) in enumerate(pos) if p["comp_id"] == this), None)
        if idx is not None:
            for j in (idx - 1, idx + 1):
                if 0 <= j < len(pos) and pos[j][1]["fingerprint"] == fingerprint:
                    issues.append(_issue("repeat_adjacent", f"台本の順で隣り合う {pos[j][1]['comp_id']} と同じ組み合わせ（{', '.join(fingerprint)}）",
                                         level="warning", file="index.html"))
    for back in (1, 2):
        ep = ctx["episode"] - back
        if ep < 1:
            continue
        for p in _episode_prints(ctx["project_dir"], ep):
            if p["fingerprint"] and _jaccard(fingerprint, p["fingerprint"]) >= min_overlap:
                issues.append(_issue("repeat_recent", f"{ep}話の {p['comp_id']} と組み合わせが半分以上重なる"
                                                      f"（{', '.join(p['fingerprint'])}）", level="warning", file="index.html"))
                break
    return issues
