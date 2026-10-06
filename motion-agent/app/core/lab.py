"""ギミックの試作場（gimmick_lab）の描画入力の検査と props の組み立て。G2 の「演出プラン」の原型。

描画入力 = ショットの並び（Docs/OPENING_MOTION_PLAN.md §16-2）:
    {
      "hud": {"label": "FILE 0047 // CLASSIFIED"},          # 任意。全編に重ねる HUD の枠（D5）
      "shots": [
        {"id": "s1", "gimmick": "decode", "sec": 3.0, "params": {...}},
        {"id": "s2", "gimmick": "font_cycle", "sec": 2.5, "params": {...}},
        {"id": "s3", "gimmick": "particle_morph", "sec": 5.0,
         "enter": {"type": "portal", "sec": 1.5, "x": 0.5, "y": 0.8},   # 前のショットの点(x,y)へ寄って入る（A1）
         "params": {...}}
      ]
    }

時間: ショットは順に並ぶ。enter が portal のショットは、前のショットの終わりの enter.sec 秒と重なって始まる
（重なりの間がポータルズーム）。秒→フレームは累積で丸める（timing.py と同じ考え方）。
点群（particle_morph）はここで作って props に入れる（points.py）。
"""
from __future__ import annotations

from pathlib import Path

from app.core import gimmick_params as gp
from app.core.fonts import load_fonts  # 書体の束＝同梱＋ユーザーの書体（shared/motion/fonts/）


LAB_IDS = {"decode": "B3_decode", "font_cycle": "B4_font_cycle", "particle_morph": "C1_particle_morph"}
_err = gp.err
_check_range = gp.check_range


def validate_input(meta: dict, inp: dict, shared_dir: Path | None = None, fonts: dict | None = None) -> list[dict]:
    fonts = fonts or load_fonts()
    g = meta["gimmicks"]
    errs: list[dict] = []
    shots = inp.get("shots")
    if not isinstance(shots, list) or not shots:
        return [_err("shots", "ショットが1つ以上要ります")]
    ids = set()
    total = 0.0
    for i, s in enumerate(shots):
        f = f"shots[{i}]"
        sid = s.get("id")
        if not sid or sid in ids:
            errs.append(_err(f"{f}.id", "id が空か重複しています"))
        ids.add(sid)
        gid = s.get("gimmick")
        if gid not in g:
            errs.append(_err(f"{f}.gimmick", f"選べるギミックは {sorted(g)} です（{gid}）"))
            continue
        spec = g[gid]
        _check_range(errs, f"{f}.sec", s.get("sec"), spec["min_sec"], spec["max_sec"], "秒")
        total += s.get("sec") or 0
        enter = s.get("enter")
        if enter:
            if i == 0:
                errs.append(_err(f"{f}.enter", "最初のショットには繋ぎを付けられません"))
            elif enter.get("type") not in meta["enters"]:
                errs.append(_err(f"{f}.enter.type", f"選べる繋ぎは {sorted(meta['enters'])} です"))
            else:
                es = meta["enters"][enter["type"]]
                _check_range(errs, f"{f}.enter.sec", enter.get("sec"), es["min_sec"], es["max_sec"], "秒")
                prev_sec = shots[i - 1].get("sec") or 0
                if isinstance(enter.get("sec"), (int, float)) and enter["sec"] >= prev_sec:
                    errs.append(_err(f"{f}.enter.sec", "前のショットより短くしてください"))
                total -= enter.get("sec") or 0
                for k in ("x", "y"):
                    _check_range(errs, f"{f}.enter.{k}", enter.get(k), 0.05, 0.95)
        gp.check_params(LAB_IDS[gid], spec, s.get("params") or {}, f, fonts, shared_dir, errs)
    if total > meta["max_total_sec"] + 1e-9:
        errs.append(_err("shots", f"合計 {total:.1f} 秒は上限 {meta['max_total_sec']} 秒を超えています"))
    return errs


def build_props(meta: dict, inp: dict, asset_base: str, fonts: dict | None = None,
                shared_dir: Path | None = None) -> dict:
    fonts = fonts or load_fonts()
    fps = int(meta["fps"])
    w, h = meta["width"], meta["height"]
    out_shots = []
    acc = 0.0  # 秒（累積）。enter の重なりは前のショットの終わりから引く
    for i, s in enumerate(inp["shots"]):
        enter = s.get("enter")
        start_sec = acc - (enter["sec"] if enter else 0)
        end_sec = start_sec + s["sec"]
        frm, to = round(start_sec * fps), round(end_sec * fps)
        p = gp.build_params(LAB_IDS[s["gimmick"]], s.get("params"), to - frm, fps, fonts, shared_dir)
        e = None
        if enter:
            e = {"type": enter["type"], "from": frm, "to": round((start_sec + enter["sec"]) * fps),
                 "x": round(enter["x"] * w), "y": round(enter["y"] * h), "r": int(enter.get("r", 12))}
        out_shots.append({"id": s["id"], "gimmick": s["gimmick"], "from": frm, "to": to, "params": p, "enter": e})
        acc = end_sec
    return {
        "template": {"id": meta["template_id"], "version": meta["version"]},
        "fps": fps,
        "total_frames": out_shots[-1]["to"],
        "hud": inp.get("hud"),
        "texture": inp.get("texture"),
        "asset_base": asset_base,
        "shots": out_shots,
    }


def still_frames(props: dict) -> list[dict]:
    """確認用の静止画: ショットごとに 35%・75%、ポータルの途中（40%）。"""
    out = []
    for s in props["shots"]:
        start = s["enter"]["to"] if s["enter"] else s["from"]
        span = s["to"] - start
        for tag, r in (("a", 0.35), ("b", 0.75)):
            out.append({"name": f"{s['id']}_{tag}", "frame": start + int(span * r)})
        if s["enter"]:
            e = s["enter"]
            out.append({"name": f"{s['id']}_portal", "frame": e["from"] + int((e["to"] - e["from"]) * 0.55)})
    return sorted(out, key=lambda x: x["frame"])[:9]
