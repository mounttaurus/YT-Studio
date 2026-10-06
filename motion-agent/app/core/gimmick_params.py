"""ギミックの params の検査と props 化（試作場 lab.py と演出プラン plan.py で共有する）。

ギミックは正式な ID（B3_decode・B4_font_cycle・C1_particle_morph）で扱う。限界値は台帳の
`limits`（試作場は meta.json の gimmicks）から渡される `spec` が持つ。
`safe=True` の時は、文字の実寸が安全域（measure.SAFE_W）に収まるかも見る（演出プラン）。
"""
from __future__ import annotations

from pathlib import Path

from app.core import fonts as fonts_mod
from app.core import measure, props as props_mod
from app.core import points as pts
from app.core.config import REMOTION_DIR

DECODE_LEFT = 170      # Decode.tsx の paddingLeft
DECODE_SPACING = 0.06  # Decode.tsx の letterSpacing（em）
DECODE_SIZE = 96       # 行の既定の大きさ
CYCLE_SIZE = 330       # FontCycle.tsx の fontSize
FRAME_W = 1920


def err(field: str, message: str) -> dict:
    return {"field": field, "message": message}


def check_text(errs, field, text, max_chars):
    if not isinstance(text, str) or not text.strip():
        errs.append(err(field, "空です"))
    elif len(text) > max_chars:
        errs.append(err(field, f"{max_chars}字までです（{len(text)}字）"))


def check_range(errs, field, v, lo, hi, unit=""):
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not (lo <= v <= hi):
        errs.append(err(field, f"{lo}〜{hi}{unit}にしてください（{v}）"))


def font_ok(errs, field, fonts, ref, default):
    """書体の指定が束で解決できるか。できれば解決した id を返す。"""
    fid = fonts_mod.resolve_ref(fonts, ref if ref is not None else default)
    if fid is None:
        errs.append(err(field, f"書体の束に無い書体です（{ref}）。id か role:<用途タグ> で指定してください"))
    return fid


def check_params(gid: str, spec: dict, p: dict, f: str, fonts: dict, shared_dir: Path | None,
                 errs: list, safe: bool = False) -> None:
    """gid は正式な ID。f は指摘の field の接頭辞（shots[0] など）。"""
    if gid == "B3_decode":
        lines = p.get("lines")
        if not isinstance(lines, list) or not (1 <= len(lines) <= spec["lines_max"]):
            errs.append(err(f"{f}.params.lines", f"1〜{spec['lines_max']}行にしてください"))
        else:
            for j, ln in enumerate(lines):
                ln = ln or {}
                check_text(errs, f"{f}.params.lines[{j}].text", ln.get("text"), spec["max_chars"])
                fid = font_ok(errs, f"{f}.params.lines[{j}].font", fonts, ln.get("font"), "mono")
                if safe and fid and isinstance(ln.get("text"), str) and ln["text"].strip():
                    size = int(ln.get("size", DECODE_SIZE))
                    w = measure.text_width(fonts["fonts"][fid], ln["text"], size, DECODE_SPACING)
                    room = FRAME_W - (FRAME_W - measure.SAFE_W) / 2 - DECODE_LEFT
                    if w > room:
                        errs.append(err(f"{f}.params.lines[{j}].text",
                                        f"安全域からはみ出します（{w:.0f}px > {room:.0f}px）。短くするか size を小さく"))
        if p.get("stamp"):
            check_text(errs, f"{f}.params.stamp", p["stamp"], 4)
            font_ok(errs, f"{f}.params.stamp_font", fonts, p.get("stamp_font"), "brush")
    elif gid == "B4_font_cycle":
        check_text(errs, f"{f}.params.text", p.get("text"), spec["max_chars"])
        if p.get("sub"):
            check_text(errs, f"{f}.params.sub", p["sub"], 30)
        fl = p.get("fonts") or []
        ids = [fonts_mod.resolve_ref(fonts, x) for x in fl] if isinstance(fl, list) else []
        if not (2 <= len(fl) <= 16) or any(i is None for i in ids):
            errs.append(err(f"{f}.params.fonts", "書体の id か role:<用途タグ> を2〜16個指定してください（束に無いものがあります）"))
        elif safe and isinstance(p.get("text"), str) and p["text"].strip():
            for fid in dict.fromkeys(ids):
                w = measure.text_width(fonts["fonts"][fid], p["text"], CYCLE_SIZE)
                if w > measure.SAFE_W:
                    errs.append(err(f"{f}.params.text",
                                    f"書体 {fid} で安全域からはみ出します（{w:.0f}px > {measure.SAFE_W}px）。字数を減らすか書体を替える"))
                    break
        check_range(errs, f"{f}.params.every", p.get("every", 3), spec["every_min"], 12, "フレーム")
    elif gid == "C1_particle_morph":
        check_range(errs, f"{f}.params.n", p.get("n", 1200), 100, spec["n_max"], "点")
        stages = p.get("stages")
        if not isinstance(stages, list) or not (2 <= len(stages) <= 5):
            errs.append(err(f"{f}.params.stages", "形を2〜5個並べてください"))
        else:
            for j, st in enumerate(stages):
                st = st or {}
                shape = st.get("shape")
                if shape not in spec["shapes"]:
                    errs.append(err(f"{f}.params.stages[{j}].shape", f"選べる形は {spec['shapes']} です"))
                if shape == "glyph":
                    check_text(errs, f"{f}.params.stages[{j}].text", st.get("text"), 6)
                    font_ok(errs, f"{f}.params.stages[{j}].font", fonts, st.get("font"), "gothic_black")
                if shape == "image":
                    errs.extend(props_mod._check_src(f"{f}.params.stages[{j}].src", st.get("src"), shared_dir))


def resolve_font(fonts: dict, ref: str) -> dict:
    fid = fonts_mod.resolve_ref(fonts, ref)
    f = fonts["fonts"][fid]
    return {"id": fid, "family": f["family"], "weight": f.get("weight", 400), "role": f.get("role", "")}


def shape_points(st: dict, n: int, seed: int, fonts: dict, shared_dir: Path | None) -> list:
    shape = st["shape"]
    if shape == "scatter":
        p = pts.scatter_points(n, seed)
    elif shape == "sphere":
        p = pts.sphere_points(n)
    elif shape == "glyph":
        f = fonts["fonts"][fonts_mod.resolve_ref(fonts, st.get("font", "gothic_black"))]
        p = pts.glyph_points(st["text"], f["file"], n, seed, index=f.get("index", 0))
    elif shape == "image":
        src = st["src"]
        if src.startswith("static/"):  # 同梱の見本（Remotion の public/）
            path = REMOTION_DIR / "public" / src[len("static/"):]
        else:
            path = (shared_dir or Path("/shared")) / src
        p = pts.image_points(path, n, seed, mode=st.get("mode", "dark"))
    else:
        raise ValueError(shape)
    return pts.order_for_morph(p)


def build_params(gid: str, p: dict, frames: int, fps: int, fonts: dict, shared_dir: Path | None) -> dict:
    """検査に通った params を、描画が受け取る形にする。frames＝ショットの長さ（フレーム）。"""
    p = dict(p or {})
    if gid == "B3_decode":
        p["lines"] = [{"text": ln["text"], "size": ln.get("size", DECODE_SIZE),
                       "font": resolve_font(fonts, ln.get("font", "mono"))} for ln in p["lines"]]
        p["len"] = frames
        stamp_font = p.pop("stamp_font", "brush")
        p["stamp"] = {"text": p["stamp"], "font": resolve_font(fonts, stamp_font)} if p.get("stamp") else None
    elif gid == "B4_font_cycle":
        p["fonts"] = [resolve_font(fonts, x) for x in p["fonts"]]
        p.setdefault("every", 3)
        p.setdefault("sub", "")
    elif gid == "C1_particle_morph":
        n = int(p.get("n", 1200))
        stages = p["stages"]
        # 形ごとの時刻: 各形が hold（既定1）の比率でショットの長さを分け合う。最後の形にも見せる時間が残る
        holds = [float(st.get("hold", 1.0)) for st in stages]
        at, acc_h = [], 0.0
        for hv in holds:
            at.append(round(frames * acc_h / sum(holds)))
            acc_h += hv
        p = {
            "n": n,
            "colors": p.get("colors", "rainbow"),
            "spin": float(p.get("spin", 0.6)),
            "stages": [{"shape": st["shape"], "at": st.get("at", at[j]),
                        "spin": st["shape"] in ("sphere", "scatter"),
                        "points": shape_points(st, n, 1 + j, fonts, shared_dir)}
                       for j, st in enumerate(stages)],
            "morph_frames": int(round(fps * float(p.get("morph_sec", 0.9)))),
        }
    return p
