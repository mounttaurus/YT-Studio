"""演出プラン（型 kinetic_teaser v2・builder "plan"）の検査と props の組み立て。設計: Docs/OPENING_MOTION_PLAN.md §18

演出プラン（描画入力）:
    {
      "brief": {"variant": {...}, "b1_first": [...], "b2_flow": [...], "b3_montage": [{..., "source": {...}}],
                "b4_out": "white|black", "b5_title": {"plate":null, "main":"…", "sub":"…"}},   # v1 の描画入力と同じ中身
      "direction": {
        "bpm": 120,
        "overlay": {"hud": {"label": "…"}, "texture": {"grain": true, "scanlines": true, "vignette": true}},
        "beats": {"b1_first": [{"id": "s1", "gimmick": "B3_decode", "bars": 1.5, "params": {...},
                                "enter": {"type": "A1_portal", "bars": 0.5, "x": 0.5, "y": 0.5}}], ...}
      },
      "notes": "演出の意図"
    }
- 書かなかったビート（direction.beats に無い）は、そのビートの K 部品1つ（v1 と同じ見た目）で描く。
- params を省いたら brief から埋める（語を二重に書かない）。
- 長さは小節（bars・0.25 の倍数）。秒への換算・拍への吸着は timing.plan_timeline。

analyze() が検査・時間・明滅の概算までを一度に行い、validate_input / build_props / API はこれを使う。
"""
from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path

from app.core import fonts as fonts_mod
from app.core import gimmick_params as gp
from app.core import plan_flash, props as props_mod, registry, timing
from app.core.config import REMOTION_DIR
from app.core.templates import get_meta

BEATS = timing.BEATS
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
TEXT_GIMMICKS = {"K1_first", "K2_flow", "B3_decode", "B4_font_cycle"}  # 文字を持つ部品（morph の相手になれる）
CONTENT_GIMMICKS = {"B3_decode", "B4_font_cycle", "C1_particle_morph"}
DEFAULT_CYCLE = ["gothic_black", "mincho_black", "brush", "mono", "pixel", "geometric", "handwriting", "mincho_light"]
# K1〜K5 が brief のどのキーから埋まるか: (brief のキー, params のキー)
BRIEF_KEY = {"K1_first": ("b1_first", "items"), "K2_flow": ("b2_flow", "words"), "K5_title": ("b5_title", "title"),
             "K4_out": ("b4_out", "out")}
_err = gp.err


def base_meta(meta: dict) -> dict:
    ext = meta["extends"]
    return get_meta(ext["template_id"], ext["version"])


# --------------------------------------------------------------------------------------------------
# 正規化
# --------------------------------------------------------------------------------------------------

def _normalize(meta: dict, base: dict, inp: dict, errs: list) -> tuple[float, list[dict]]:
    d = inp.get("direction") or {}
    if not isinstance(d, dict):
        errs.append(_err("direction", "オブジェクトにしてください"))
        d = {}
    bpm = d.get("bpm", meta["bpm"]["default"])
    gp.check_range(errs, "direction.bpm", bpm, meta["bpm"]["min"], meta["bpm"]["max"])
    if not isinstance(bpm, (int, float)) or isinstance(bpm, bool):
        bpm = meta["bpm"]["default"]
    beats_in = d.get("beats") or {}
    if not isinstance(beats_in, dict):
        errs.append(_err("direction.beats", "ビート名をキーにしたオブジェクトにしてください"))
        beats_in = {}
    for k in beats_in:
        if k not in BEATS:
            errs.append(_err(f"direction.beats.{k}", f"ビートは {list(BEATS)} です"))
    shots: list[dict] = []
    for b in BEATS:
        raw = beats_in.get(b)
        if not raw:
            shots.append({"id": f"default_{b}", "beat": b, "gimmick": meta["beats"][b]["default_gimmick"],
                          "bars": timing.default_bars(base, b, bpm), "enter": None, "params": {}, "path": f"direction.beats.{b}",
                          "default": True})
            continue
        if not isinstance(raw, list):
            errs.append(_err(f"direction.beats.{b}", "ショットの配列にしてください"))
            continue
        for j, s in enumerate(raw):
            if not isinstance(s, dict):
                errs.append(_err(f"direction.beats.{b}[{j}]", "オブジェクトにしてください"))
                continue
            e = s.get("enter")
            if e is not None and not isinstance(e, dict):
                errs.append(_err(f"direction.beats.{b}[{j}].enter", "オブジェクトにしてください"))
                e = None
            if e:
                e = {**e, "type": registry.canonical(e.get("type"))}
                if e["type"] in (None, "cut"):
                    e = None
            shots.append({"id": s.get("id"), "beat": b, "gimmick": registry.canonical(s.get("gimmick")),
                          "bars": s.get("bars"), "enter": e,
                          "params": s.get("params") if isinstance(s.get("params"), dict) else {},
                          "path": f"direction.beats.{b}[{j}]"})
    return float(bpm), shots


# --------------------------------------------------------------------------------------------------
# 素材の出典・動画の長さ
# --------------------------------------------------------------------------------------------------

def _media_path(src: str, shared_dir: Path | None) -> Path | None:
    if src.startswith("static/"):
        return REMOTION_DIR / "public" / src[len("static/"):]
    return (shared_dir / src) if shared_dir else None


def _video_len(path: Path) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        return float(out)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _check_source(errs: list, field: str, src: str, source, kinds: list[str]) -> None:
    """素材の出典。同梱の見本（static/）は不要。それ以外は必須。"""
    if src.startswith("static/"):
        return
    if not isinstance(source, dict) or source.get("kind") not in kinds:
        errs.append(_err(f"{field}.source", f"出典 source が要ります（kind は {kinds} のどれか）"))
        return
    if source["kind"] in ("cc_by", "cc_by_sa"):
        for k in ("author", "url", "license"):
            if not source.get(k):
                errs.append(_err(f"{field}.source.{k}", "CC BY 系は author・url・license が要ります"))
    elif source["kind"] in ("public_domain", "cc0", "licensed", "stock") and not (source.get("url") or source.get("note")):
        errs.append(_err(f"{field}.source", "出典の url か note（どこで手に入れたか）が要ります"))


# --------------------------------------------------------------------------------------------------
# params を brief から埋める
# --------------------------------------------------------------------------------------------------

def _fill(s: dict, brief: dict, fonts: dict, errs: list) -> dict:
    """ショットの params を brief で埋めたものを返す（足りなければ指摘）。"""
    g, p, f = s["gimmick"], copy.deepcopy(s["params"]), s["path"]

    def need(key: str):
        v = brief.get(key)
        if v is None:
            errs.append(_err(f"{f}.params", f"params を省くには brief.{key} が要ります"))
        return v

    if g == "K1_first":
        p.setdefault("items", need("b1_first"))
    elif g == "K2_flow":
        p.setdefault("words", need("b2_flow"))
    elif g == "K3_montage":
        mats = brief.get("b3_montage")
        if mats is None:
            errs.append(_err(f"{f}.params", "K3_montage には brief.b3_montage が要ります"))
        else:
            p.setdefault("materials", list(range(len(mats))))
    elif g == "K4_out":
        p.setdefault("out", brief.get("b4_out") or "black")
    elif g == "K5_title":
        p.setdefault("title", need("b5_title"))
    elif g == "B3_decode":
        if "lines" not in p:
            items = need("b1_first")
            if items:
                p["lines"] = [{"text": (it or {}).get("text", ""), "font": "mono"} for it in items]
    elif g == "B4_font_cycle":
        if "text" not in p:
            items = need("b1_first")
            if items:
                p["text"] = (items[0] or {}).get("text", "")
        p.setdefault("fonts", [x for x in DEFAULT_CYCLE if x in fonts["fonts"]])
    return p


def _shot_text(s: dict) -> tuple[str, str] | None:
    """形の受け渡し（morph）で字形の点群にする文字と書体の指定。"""
    p, g = s["params"], s["gimmick"]
    try:
        if g == "K1_first":
            return p["items"][0]["text"], "gothic_black"
        if g == "K2_flow":
            return p["words"][-1], "gothic_black"
        if g == "B3_decode":
            ln = p["lines"][-1]
            return ln["text"], ln.get("font", "mono")
        if g == "B4_font_cycle":
            return p["text"], p["fonts"][0]
    except (KeyError, IndexError, TypeError):
        return None
    return None


# --------------------------------------------------------------------------------------------------
# 検査
# --------------------------------------------------------------------------------------------------

def analyze(meta: dict, inp: dict, shared_dir: Path | None = None, fonts: dict | None = None) -> dict:
    """検査・時間の表・明滅の概算まで。{errors, warnings, shots（正規化済み）, bpm, timeline, flash, variant, base}"""
    fonts = fonts or fonts_mod.load_fonts()
    base = base_meta(meta)
    errs: list[dict] = []
    warns: list[str] = []
    brief = inp.get("brief")
    if not isinstance(brief, dict):
        return {"errors": [_err("brief", "brief（v1 の描画入力と同じ中身）が要ります")], "warnings": [], "shots": [], "timeline": None}
    bpm, shots = _normalize(meta, base, inp, errs)

    # brief の検査: 書いてあるキーは常に、v1 の検査（個数・字数・種類・素材の実在）に通す
    used = {"b1_first": False, "b2_flow": False, "b3_montage": False, "b4_out": False, "b5_title": False}
    present = {k: v for k, v in brief.items() if k in used or k == "variant"}
    errs.extend(_v1_errors(base, present, shared_dir, "brief."))
    for k in brief:
        if k not in used and k not in ("variant", "beat_sec"):
            errs.append(_err(f"brief.{k}", "brief に無いキーです"))
    if "beat_sec" in brief:
        errs.append(_err("brief.beat_sec", "長さは direction の bars（小節）で決めます"))
    variant = props_mod.resolve_variant(base, brief.get("variant"))

    # ショットごとの検査
    ids: set = set()
    filled: list[dict] = []
    resolved_fonts: dict[str, str] = {}
    rbeats = {b: [] for b in BEATS}
    for i, s in enumerate(shots):
        f = s["path"]
        if not s.get("default"):
            if not isinstance(s["id"], str) or not _ID_RE.match(s["id"]):
                errs.append(_err(f"{f}.id", "id は英数字・_・-（40字まで）です"))
            if s["id"] in ids:
                errs.append(_err(f"{f}.id", f"id が重複しています（{s['id']}）"))
            ids.add(s["id"])
        g = s["gimmick"]
        spec = registry.gimmick(g) if g else None
        if spec is None:
            errs.append(_err(f"{f}.gimmick", f"選べるギミックは {sorted(registry.load()['gimmicks'])} です（{g}）"))
            continue
        if g not in meta["beats"][s["beat"]]["allowed"]:
            errs.append(_err(f"{f}.gimmick", f"{s['beat']} で使えるのは {meta['beats'][s['beat']]['allowed']} です（{g}）"))
        explicit = set(s["params"])
        p = _fill(s, brief, fonts, errs)
        s["params"] = p
        # 部品ごとの params
        if g in CONTENT_GIMMICKS:
            gp.check_params(g, {**spec.get("limits", {})}, p, f, fonts, shared_dir, errs, safe=True)
            for ref in _font_refs(g, p):
                rid = fonts_mod.resolve_ref(fonts, ref)
                if rid and ref != rid:
                    resolved_fonts[ref] = rid
        elif g in ("K1_first", "K2_flow", "K5_title") and BRIEF_KEY[g][1] in explicit:
            bkey, pkey = BRIEF_KEY[g]
            errs.extend(_v1_errors(base, {bkey: p[pkey]}, shared_dir, f"{f}.params.", rename=(bkey, pkey)))
        if g == "K3_montage":
            _check_k3(base, meta, brief, p, f, errs, shared_dir)
        if g == "K4_out" and p.get("out") not in base["beats"]["b4_out"]["choices"]:
            errs.append(_err(f"{f}.params.out", f"{base['beats']['b4_out']['choices']} のどれかです"))
        rbeats[s["beat"]].append(s)
        filled.append(s)
    if True:
        for i, m in enumerate(brief.get("b3_montage") or []):
            if isinstance(m, dict) and isinstance(m.get("src"), str):
                _check_source(errs, f"brief.b3_montage[{i}]", m["src"], m.get("source"), meta["material_source_kinds"])
                p_ = _media_path(m["src"], shared_dir)
                if m.get("type") == "video" and p_ and p_.is_file():
                    ln = _video_len(p_)
                    if ln is not None and isinstance(m.get("in", 0), (int, float)) and m.get("in", 0) >= ln:
                        errs.append(_err(f"brief.b3_montage[{i}].in", f"動画の長さ {ln:.1f} 秒より短い開始点にしてください"))

    # 長さ・繋ぎ（構造が壊れていると時間の表は作れない）
    structural = _check_lengths(meta, shots, bpm, errs)
    timeline = flash_est = None
    if not structural and not any(s.get("gimmick") is None or registry.gimmick(s["gimmick"]) is None for s in shots):
        try:
            timeline = _timeline(meta, base, shots, bpm, brief)
        except ValueError as e:
            errs.append(_err("direction", str(e)))
    if timeline:
        _check_timeline(meta, shots, timeline, errs, warns)
        _check_morph(meta, base, shots, timeline, fonts, errs)
        out_color = next((s["params"].get("out") for s in shots if s["gimmick"] == "K4_out"), brief.get("b4_out"))
        flash_est = plan_flash.estimate(base, variant, timeline["shots"], out_color, timeline["fps"])
        warns.extend(flash_est.pop("warnings"))
        if not flash_est["ok"]:
            errs.append(_err("direction", f"明滅が多すぎます（概算で毎秒 {flash_est['max_per_sec']} 回・{flash_est['worst_at_sec']} 秒付近・"
                                          f"上限 {flash_est['limit_per_sec']}）。語の切り替えを遅くするか、明るさの近い色にしてください"))
    # overlay
    ov = (inp.get("direction") or {}).get("overlay") or {}
    if ov.get("hud") is not None:
        if not isinstance(ov["hud"], dict) or len(str(ov["hud"].get("label", ""))) > 40:
            errs.append(_err("direction.overlay.hud.label", "hud は {label: 40字まで} です"))
    if ov.get("texture") is not None and not isinstance(ov["texture"], dict):
        errs.append(_err("direction.overlay.texture", "texture は {grain, scanlines, vignette}（真偽）です"))
    return {"errors": errs, "warnings": warns, "shots": shots, "bpm": bpm, "timeline": timeline, "flash": flash_est,
            "variant": variant, "base": base, "resolved_fonts": resolved_fonts, "brief": brief}


def _font_refs(g: str, p: dict) -> list[str]:
    if g == "B3_decode":
        return [(ln or {}).get("font", "mono") for ln in p.get("lines") or []] + ([p.get("stamp_font", "brush")] if p.get("stamp") else [])
    if g == "B4_font_cycle":
        return list(p.get("fonts") or [])
    if g == "C1_particle_morph":
        return [st.get("font", "gothic_black") for st in p.get("stages") or [] if (st or {}).get("shape") == "glyph"]
    return []


def _v1_errors(base: dict, part: dict, shared_dir: Path | None, prefix: str, rename: tuple[str, str] | None = None) -> list[dict]:
    """v1 の検査を、part に書いてあるキーだけに当てる（無いキーは通る仮の値で埋めて、その指摘は捨てる）。"""
    placeholder = {
        "b1_first": [{"text": "-", "kind": "word"}], "b2_flow": ["a", "b", "c"],
        "b3_montage": [{"src": "static/samples/img_01.png", "type": "image"}] * 3, "b4_out": "black",
        "b5_title": {"main": "-"},
    }
    inp = {**placeholder, **part}
    out = []
    for e in props_mod.validate_input(base, inp, shared_dir):
        root = re.split(r"[\.\[]", e["field"])[0]
        if root == "beat_sec" or (root not in part and root != "variant"):
            continue
        if root == "variant" and "variant" not in part:
            continue
        field = e["field"]
        if rename and root == rename[0]:
            field = rename[1] + field[len(root):]
        out.append(_err(prefix + field, e["message"]))
    return out


def _check_k3(base: dict, meta: dict, brief: dict, p: dict, f: str, errs: list, shared_dir) -> None:
    mats = brief.get("b3_montage") or []
    idx = p.get("materials")
    if not isinstance(idx, list) or not idx or any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(mats) for i in idx):
        errs.append(_err(f"{f}.params.materials", f"brief.b3_montage の番号（0〜{len(mats) - 1}）の並びにしてください"))
    cfg = meta["k3_cuts"]
    start, mn = p.get("start_sec", cfg["start_sec"]), p.get("min_sec", cfg["min_sec"])
    gp.check_range(errs, f"{f}.params.start_sec", start, 0.1, 2.0, "秒")
    gp.check_range(errs, f"{f}.params.min_sec", mn, 0.1, 1.0, "秒")
    if isinstance(start, (int, float)) and isinstance(mn, (int, float)) and mn > start:
        errs.append(_err(f"{f}.params.min_sec", "最短のカットは最初のカットより短くしてください"))


def _check_lengths(meta: dict, shots: list[dict], bpm: float, errs: list) -> bool:
    """bars・enter の形と秒の範囲。時間の表が作れない壊れ方なら True。"""
    broken = False
    bar = timing.bar_sec(bpm)
    for i, s in enumerate(shots):
        f = s["path"]
        spec = registry.gimmick(s["gimmick"]) if s["gimmick"] else None
        bars = s["bars"]
        if not isinstance(bars, (int, float)) or isinstance(bars, bool) or bars <= 0 or abs(bars * 4 - round(bars * 4)) > 1e-6:
            errs.append(_err(f"{f}.bars", f"小節は 0.25（1拍）の倍数で書いてください（{bars}）"))
            broken = True
            continue
        sec = bars * bar
        if spec and not (spec["min_sec"] - 1e-9 <= sec <= spec["max_sec"] + 1e-9):
            errs.append(_err(f"{f}.bars", f"{spec['name']}は {spec['min_sec']}〜{spec['max_sec']} 秒です"
                                          f"（今の {bars} 小節＝{sec:.2f} 秒・BPM {bpm:g}）"))
        e = s["enter"]
        if not e:
            continue
        if i == 0:
            errs.append(_err(f"{f}.enter", "最初のショットには繋ぎを付けられません"))
            broken = True
            continue
        espec = registry.enter(e["type"])
        if espec is None:
            errs.append(_err(f"{f}.enter.type", f"選べる繋ぎは {sorted(registry.load()['enters'])} です（{e['type']}）"))
            broken = True
            continue
        eb = e.get("bars")
        if not isinstance(eb, (int, float)) or isinstance(eb, bool) or abs(eb * 4 - round(eb * 4)) > 1e-6 \
                or not espec["min_bars"] <= eb <= espec["max_bars"]:
            errs.append(_err(f"{f}.enter.bars", f"{espec['name']}は {espec['min_bars']}〜{espec['max_bars']} 小節（0.25 の倍数）です（{eb}）"))
            broken = True
            continue
        if s["gimmick"] in ("K4_out", "K5_title") or shots[i - 1]["gimmick"] == "K4_out":
            errs.append(_err(f"{f}.enter", "④アウトと⑤タイトルバックには繋ぎを付けられません（④の直後は切り替えのみ）"))
            broken = True
        if espec.get("needs_point"):
            for k in ("x", "y"):
                gp.check_range(errs, f"{f}.enter.{k}", e.get(k), 0.05, 0.95)
        prev_bars = shots[i - 1]["bars"]
        if (isinstance(prev_bars, (int, float)) and eb >= prev_bars) or eb >= bars:
            errs.append(_err(f"{f}.enter.bars", "繋ぎの重なりは前後どちらのショットより短くしてください"))
            broken = True
    return broken


def _timeline(meta: dict, base: dict, shots: list[dict], bpm: float, brief: dict) -> dict:
    cfg0 = meta["k3_cuts"]
    flat = []
    for s in shots:
        item = {"id": s["id"], "beat": s["beat"], "gimmick": s["gimmick"], "bars": s["bars"], "enter": s["enter"]}
        if s["gimmick"] == "K2_flow":
            n_words = len(s["params"].get("words") or [])
            slots = int(2 * timing.to_beats(s["bars"]))
            if n_words > slots:
                raise ValueError(f"{s['path']}: 語が多すぎます（{n_words} 語に対して半拍の枠が {slots} しかありません）。小節を延ばすか語を減らす")
            item["n_words"] = max(1, n_words)
        if s["gimmick"] == "K3_montage":
            p = s["params"]
            item["n_materials"] = max(1, len(p.get("materials") or [1]))
            item["cut_cfg"] = {**cfg0, "start_sec": p.get("start_sec", cfg0["start_sec"]), "min_sec": p.get("min_sec", cfg0["min_sec"])}
        flat.append(item)
    return timing.plan_timeline(meta, bpm, flat)


def _check_timeline(meta: dict, shots: list[dict], tl: dict, errs: list, warns: list) -> None:
    if tl["total_sec"] > meta["max_total_sec"] + 1e-9:
        errs.append(_err("direction", f"合計 {tl['total_sec']:.2f} 秒は上限 {meta['max_total_sec']} 秒を超えています"))
    for b, r in tl["beats"].items():
        sec = (r["to"] - r["from"]) / tl["fps"]
        lo, hi = meta["beats"][b]["min_sec"], meta["beats"][b]["max_sec"]
        if not (lo - 0.05 <= sec <= hi + 0.05):
            errs.append(_err(f"direction.beats.{b}", f"{meta['beats'][b]['label']}は合計 {lo}〜{hi} 秒です（今 {sec:.2f} 秒）"))
    prev_end = -1
    for s, t in zip(shots, tl["shots"]):
        e = t["enter"]
        if e:
            if e["from"] < prev_end:
                errs.append(_err(f"{s['path']}.enter", "繋ぎの重なりが前の繋ぎと重なっています。ショットを長くするか重なりを短くしてください"))
            prev_end = e["to"]
    default_ids = {s["id"] for s in shots if s.get("default")}
    for b in tl["beats"]:
        first = next(t for t in tl["shots"] if t["beat"] == b)
        if first["id"] in default_ids:
            continue  # 書かなかったビートは v1 の既定の長さ（拍に丸めただけ）
        if first["start_beat"] % timing.BEATS_PER_BAR and first["enter"] is None:
            warns.append(f"{meta['beats'][b]['label']}（{b}）が小節の頭から始まっていません（{first['bar_no']}小節目の{first['beat_no']}拍目）")


def _check_morph(meta: dict, base: dict, shots: list[dict], tl: dict, fonts: dict, errs: list) -> None:
    """形の受け渡し: 片側が C1 で、もう片側が文字を持つ部品。文字は6字まで。C1 側に時間が要る。"""
    for i, s in enumerate(shots):
        e = s["enter"]
        if not e or e["type"] != "morph":
            continue
        f, prev = s["path"], shots[i - 1]
        a, b = prev["gimmick"], s["gimmick"]
        if not ((a == "C1_particle_morph" and b in TEXT_GIMMICKS) or (b == "C1_particle_morph" and a in TEXT_GIMMICKS)):
            errs.append(_err(f"{f}.enter.type", "morph は C1_particle_morph と文字を持つ部品（K1・K2・B3・B4）の間だけです"))
            continue
        st = _shot_text(prev if a in TEXT_GIMMICKS else s)
        if not st or not 1 <= len(st[0]) <= 6:
            errs.append(_err(f"{f}.enter.type", f"morph する文字は6字までです（{st[0] if st else '取れません'}）"))
        elif fonts_mod.resolve_ref(fonts, st[1]) is None:
            errs.append(_err(f"{f}.enter.type", f"morph する文字の書体が束にありません（{st[1]}）"))
    # C1 のショットに、受け渡しの時間と各形が落ち着く時間（1形12フレーム）が残っているか
    for i, s in enumerate(shots):
        if s["gimmick"] != "C1_particle_morph" or not isinstance(s["params"].get("stages"), list):
            continue
        t = tl["shots"][i]
        mf = round(tl["fps"] * float(s["params"].get("morph_sec", 0.9)))
        head = (t["enter"]["to"] - t["from"]) if shots[i]["enter"] and shots[i]["enter"]["type"] == "morph" else 0
        nxt = tl["shots"][i + 1] if i + 1 < len(shots) else None
        tail = (nxt["enter"]["to"] - nxt["enter"]["from"] + round(mf * 0.5)) if nxt and nxt["enter"] and nxt["enter"]["type"] == "morph" else 0
        if (head or tail) and (t["to"] - t["from"]) < head + tail + 12 * len(s["params"]["stages"]):
            errs.append(_err(f"{s['path']}.bars", "形の受け渡しを受ける点群のショットが短すぎます。小節を延ばしてください"))


# --------------------------------------------------------------------------------------------------
# builders の共通の口
# --------------------------------------------------------------------------------------------------

def validate_input(meta: dict, inp: dict, shared_dir: Path | None = None, fonts: dict | None = None) -> list[dict]:
    return analyze(meta, inp, shared_dir, fonts)["errors"]


def build_props(meta: dict, inp: dict, asset_base: str, fonts: dict | None = None, shared_dir: Path | None = None) -> dict:
    """検査に通った演出プランから、描画が受け取る props を作る。"""
    fonts = fonts or fonts_mod.load_fonts()
    a = analyze(meta, inp, shared_dir, fonts)
    if a["errors"]:
        raise ValueError(a["errors"])
    tl, shots, brief, base = a["timeline"], a["shots"], a["brief"], a["base"]
    fps = tl["fps"]
    w, h = meta["width"], meta["height"]
    out_color = next((s["params"].get("out") for s in shots if s["gimmick"] == "K4_out"), brief.get("b4_out") or "black")
    out_shots = []
    for idx, (s, t) in enumerate(zip(shots, tl["shots"])):
        g, p = s["gimmick"], s["params"]
        frames = t["to"] - t["from"]
        if g == "K1_first":
            params = {"items": [{"text": it["text"], "kind": it["kind"]} for it in p["items"]]}
        elif g == "K2_flow":
            params = {"words": list(p["words"]), "switches": [x - t["from"] for x in t["switches"]]}
        elif g == "K3_montage":
            mats = [brief["b3_montage"][i] for i in p["materials"]]
            params = {"materials": [{"src": m["src"], "type": m["type"], "in": float(m.get("in", 0))} for m in mats],
                      "cuts": [{"from": c["from"] - t["from"], "to": c["to"] - t["from"], "material": c["material"] % len(mats)}
                               for c in t["cuts"]]}
        elif g == "K4_out":
            params = {"out": p["out"]}
        elif g == "K5_title":
            tt = p["title"]
            params = {"title": {"plate": tt.get("plate") or None, "main": tt.get("main") or "", "sub": tt.get("sub") or ""},
                      "out": out_color}
        else:
            params = _content_params(g, p, idx, shots, tl, a, fonts, shared_dir)
        e = None
        if t["enter"]:
            te = t["enter"]
            e = {"type": te["type"], "from": te["from"], "to": te["to"],
                 "x": round((te["x"] or 0.5) * w), "y": round((te["y"] or 0.5) * h), "r": int(s["enter"].get("r", 12))}
        out_shots.append({"id": s["id"], "beat": s["beat"], "gimmick": g, "from": t["from"], "to": t["to"],
                          "hold_to": t["hold_to"], "params": params, "enter": e})
    ov = (inp.get("direction") or {}).get("overlay") or {}
    return {
        "template": {"id": meta["template_id"], "version": meta["version"]},
        "fps": fps,
        "bpm": tl["bpm"],
        "total_frames": tl["total_frames"],
        "variant": a["variant"],
        "overlay": {"hud": ov.get("hud"), "texture": ov.get("texture")},
        "asset_base": asset_base,
        "shots": out_shots,
        "sfx": tl["sfx"],
    }


def _content_params(g: str, p: dict, idx: int, shots: list[dict], tl: dict, a: dict, fonts: dict, shared_dir: Path | None) -> dict:
    """B3・B4・C1 の params。C1 は形の受け渡し（morph）で前後の文字の字形を端に足す。"""
    t = tl["shots"][idx]
    frames = t["to"] - t["from"]
    fps = tl["fps"]
    if g != "C1_particle_morph":
        return gp.build_params(g, p, frames, fps, fonts, shared_dir)
    p = copy.deepcopy(p)
    stages = p["stages"]
    mf = int(round(fps * float(p.get("morph_sec", 0.9))))
    head = tail = None
    if shots[idx]["enter"] and shots[idx]["enter"]["type"] == "morph":
        head = _shot_text(shots[idx - 1])
    if idx + 1 < len(shots) and shots[idx + 1]["enter"] and shots[idx + 1]["enter"]["type"] == "morph":
        tail = _shot_text(shots[idx + 1])
    if not head and not tail:
        return gp.build_params(g, p, frames, fps, fonts, shared_dir)
    # 先頭に前の文字の字形、末尾に次の文字の字形を足し、各形の時刻を明示する
    start = (t["enter"]["to"] - t["from"]) if head else 0
    end = (tl["shots"][idx + 1]["enter"]["from"] - t["from"] - round(mf * 0.5)) if tail else frames
    holds = [float(st.get("hold", 1.0)) for st in stages]
    all_stages, times = [], []
    if head:
        all_stages.append({"shape": "glyph", "text": head[0], "font": head[1]})
        times.append(0)
    acc = 0.0
    for st, hv in zip(stages, holds):
        all_stages.append(st)
        times.append(start + round((end - start) * acc / sum(holds)))
        acc += hv
    if tail:
        all_stages.append({"shape": "glyph", "text": tail[0], "font": tail[1]})
        times.append(end)
    for k, (st, at) in enumerate(zip(all_stages, times)):
        st["at"] = at
    p["stages"] = all_stages
    return gp.build_params(g, p, frames, fps, fonts, shared_dir)


def still_frames(props: dict) -> list[dict]:
    """確認用の静止画（最大12枚）。ショットごとに1枚（55%）、繋ぎの途中、余りがあれば75%の2枚目。"""
    out: list[dict] = []

    def solo(s):
        return (s["enter"]["to"] if s["enter"] else s["from"]), s["to"]

    for s in props["shots"]:
        a, b = solo(s)
        frame = s["from"] + 4 if s["gimmick"] == "K4_out" else a + int((b - a) * 0.55)
        out.append({"name": s["id"], "frame": min(frame, props["total_frames"] - 1)})
    for s in props["shots"]:
        if s["enter"]:
            e = s["enter"]
            out.append({"name": f"{s['id']}_enter", "frame": e["from"] + int((e["to"] - e["from"]) * 0.5)})
    for s in props["shots"]:
        if len(out) >= 12:
            break
        if s["gimmick"] != "K4_out":
            a, b = solo(s)
            out.append({"name": f"{s['id']}_b", "frame": a + int((b - a) * 0.85)})
    return sorted(out[:12], key=lambda x: x["frame"])


def sheet_layout(props: dict) -> dict:
    return {"columns": 4, "cell": [480, 270]}


def summarize(meta: dict, inp: dict, shared_dir: Path | None = None) -> dict:
    """API・MCP が返す検査の要約: errors・warnings・時間の表（ショットごとの秒と小節の位置）・明滅の概算・解決した書体。"""
    a = analyze(meta, inp, shared_dir)
    tl = a["timeline"]
    shots = []
    if tl:
        fps = tl["fps"]
        for t in tl["shots"]:
            e = t["enter"]
            shots.append({"id": t["id"], "beat": t["beat"], "gimmick": t["gimmick"],
                          "from_sec": round(t["from"] / fps, 3), "to_sec": round(t["to"] / fps, 3),
                          "at": f"{t['bar_no']}小節目の{t['beat_no']}拍目", "frames": [t["from"], t["to"]],
                          "enter": e and {"type": e["type"], "sec": round((e["to"] - e["from"]) / fps, 3)}})
    return {"ok": not a["errors"], "errors": a["errors"], "warnings": a["warnings"],
            "bpm": a.get("bpm"), "total_sec": tl["total_sec"] if tl else None, "shots": shots,
            "flash_estimate": a.get("flash"), "resolved_fonts": a.get("resolved_fonts", {})}
