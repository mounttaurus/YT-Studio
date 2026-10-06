"""描画入力（render input）の検査と、Remotion に渡す props の組み立て。

描画入力は型の枠を埋めたもの（M2 で opening.json のブリーフから作る）。検査に通ったものだけを
props にする（TypeScript 側は props を信じて描くだけ）。制約は全て meta.json から読む。

描画入力の形（kinetic_teaser v1）:
    {
      "variant": {"palette": "crimson", "montage_motion": "fly", "montage_tone": "mono", "grain": true},
      "b1_first": [{"text": "2025年9月10日", "kind": "date"}, ...],
      "b2_flow": ["語", ...],
      "b3_montage": [{"src": "static/samples/img_01.png" | "<shared/ からの相対パス>", "type": "image|video", "in": 0}],
      "b4_out": "white" | "black",
      "b5_title": {"plate": null | "<shared/ からの相対パス>", "main": "...", "sub": "..."},
      "beat_sec": {"b2_flow": 6.0, ...}   # 任意
    }
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from app.core.timing import BEATS, beat_seconds, build_timing


def _err(field: str, message: str) -> dict:
    return {"field": field, "message": message}


def _check_src(field: str, src, shared_dir: Path | None) -> list[dict]:
    """素材の参照: `static/...`（同梱の見本）か、shared/ からの相対パス。絶対パス・`..` は不可。"""
    if not isinstance(src, str) or not src:
        return [_err(field, "素材のパスが空です")]
    p = PurePosixPath(src)
    if src.startswith("/") or "\\" in src or ".." in p.parts:
        return [_err(field, f"素材のパスは shared/ からの相対パスで指定してください（{src}）")]
    if src.startswith("static/"):
        return []
    if shared_dir is not None and not (shared_dir / src).is_file():
        return [_err(field, f"素材のファイルがありません（shared/{src}）")]
    return []


def resolve_variant(meta: dict, given: dict | None) -> dict:
    given = given or {}
    return {k: given.get(k, v["default"]) for k, v in meta["variants"].items()}


def validate_input(meta: dict, inp: dict, shared_dir: Path | None = None) -> list[dict]:
    """検査の指摘の一覧（空なら合格）。shared_dir を渡すと素材ファイルの実在も見る。"""
    errs: list[dict] = []
    beats = meta["beats"]

    # 変化のつまみ
    for k, given in (inp.get("variant") or {}).items():
        spec = meta["variants"].get(k)
        if spec is None:
            errs.append(_err(f"variant.{k}", "この型に無いつまみです"))
            continue
        options = spec["options"]
        allowed = list(options.keys()) if isinstance(options, dict) else options
        if given not in allowed:
            errs.append(_err(f"variant.{k}", f"選べる値は {allowed} です（{given}）"))

    # ① 最初のキーワード
    b1 = inp.get("b1_first")
    spec = beats["b1_first"]
    if not isinstance(b1, list) or not (spec["items"]["min"] <= len(b1) <= spec["items"]["max"]):
        errs.append(_err("b1_first", f"{spec['items']['min']}〜{spec['items']['max']}個にしてください"))
    else:
        for i, it in enumerate(b1):
            text = (it or {}).get("text", "")
            if not text.strip():
                errs.append(_err(f"b1_first[{i}].text", "空です"))
            elif len(text) > spec["max_chars"]:
                errs.append(_err(f"b1_first[{i}].text", f"{spec['max_chars']}字までです（{len(text)}字）"))
            if (it or {}).get("kind") not in spec["kinds"]:
                errs.append(_err(f"b1_first[{i}].kind", f"種類は {spec['kinds']} のどれかです"))

    # ② キーワードの流れ
    b2 = inp.get("b2_flow")
    spec = beats["b2_flow"]
    if not isinstance(b2, list) or not (spec["keywords"]["min"] <= len(b2) <= spec["keywords"]["max"]):
        errs.append(_err("b2_flow", f"{spec['keywords']['min']}〜{spec['keywords']['max']}語にしてください"))
    else:
        for i, w in enumerate(b2):
            if not isinstance(w, str) or not w.strip():
                errs.append(_err(f"b2_flow[{i}]", "空です"))
            elif len(w) > spec["max_chars"]:
                errs.append(_err(f"b2_flow[{i}]", f"{spec['max_chars']}字までです（{len(w)}字）"))

    # ③ 素材のカット
    b3 = inp.get("b3_montage")
    spec = beats["b3_montage"]
    if not isinstance(b3, list) or not (spec["materials"]["min"] <= len(b3) <= spec["materials"]["max"]):
        errs.append(_err("b3_montage", f"素材は{spec['materials']['min']}〜{spec['materials']['max']}個にしてください"))
    else:
        for i, m in enumerate(b3):
            m = m or {}
            if m.get("type") not in spec["types"]:
                errs.append(_err(f"b3_montage[{i}].type", f"種類は {spec['types']} のどれかです"))
            errs.extend(_check_src(f"b3_montage[{i}].src", m.get("src"), shared_dir))
            if not isinstance(m.get("in", 0), (int, float)) or m.get("in", 0) < 0:
                errs.append(_err(f"b3_montage[{i}].in", "動画の開始点は0以上の秒です"))

    # ④ アウト
    if inp.get("b4_out") not in beats["b4_out"]["choices"]:
        errs.append(_err("b4_out", f"{beats['b4_out']['choices']} のどれかです"))

    # ⑤ タイトルバック
    b5 = inp.get("b5_title") or {}
    spec = beats["b5_title"]
    if b5.get("plate"):
        errs.extend(_check_src("b5_title.plate", b5["plate"], shared_dir))
    elif not (b5.get("main") or "").strip():
        errs.append(_err("b5_title", "タイトル板の画像か、タイトルの文字（main）のどちらかが要ります"))
    if len(b5.get("main") or "") > spec["max_chars_main"]:
        errs.append(_err("b5_title.main", f"{spec['max_chars_main']}字までです"))
    if len(b5.get("sub") or "") > spec["max_chars_sub"]:
        errs.append(_err("b5_title.sub", f"{spec['max_chars_sub']}字までです"))

    # 秒
    given_sec = inp.get("beat_sec") or {}
    for b, v in given_sec.items():
        if b not in BEATS:
            errs.append(_err(f"beat_sec.{b}", "この型に無いビートです"))
        elif not isinstance(v, (int, float)) or not (beats[b]["min_sec"] <= v <= beats[b]["max_sec"]):
            errs.append(_err(f"beat_sec.{b}", f"{beats[b]['min_sec']}〜{beats[b]['max_sec']}秒にしてください"))
    total = sum(beat_seconds(meta, {k: v for k, v in given_sec.items() if k in BEATS}).values())
    if total > meta["max_total_sec"] + 1e-9:
        errs.append(_err("beat_sec", f"合計 {total:.1f} 秒は上限 {meta['max_total_sec']} 秒を超えています"))
    return errs


def build_props(meta: dict, inp: dict, asset_base: str) -> dict:
    """検査に通った描画入力から props を作る（検査は呼び出し側で先に行う）。"""
    sec = beat_seconds(meta, inp.get("beat_sec"))
    timing = build_timing(meta, sec, len(inp["b2_flow"]), len(inp["b3_montage"]), inp.get("switch_sec"))
    b5 = inp.get("b5_title") or {}
    return {
        "template": {"id": meta["template_id"], "version": meta["version"]},
        "variant": resolve_variant(meta, inp.get("variant")),
        "b1_first": [{"text": it["text"], "kind": it["kind"]} for it in inp["b1_first"]],
        "b2_flow": list(inp["b2_flow"]),
        "b3_montage": [{"src": m["src"], "type": m["type"], "in": float(m.get("in", 0))} for m in inp["b3_montage"]],
        "b4_out": inp["b4_out"],
        "b5_title": {"plate": b5.get("plate") or None, "main": b5.get("main") or "", "sub": b5.get("sub") or ""},
        "asset_base": asset_base,
        "timing": timing,
    }


def still_frames(props: dict) -> list[dict]:
    """担当の共通の口（builders.py）。"""
    return default_still_frames(props["timing"])


def default_still_frames(timing: dict) -> list[dict]:
    """確認用の静止画のフレーム（ビートごとの見せ場）。名前はファイル名になる。"""
    b = timing["beats"]
    out = [{"name": "b1_first", "frame": b["b1_first"]["from"] + int((b["b1_first"]["to"] - b["b1_first"]["from"]) * 0.75)}]
    sw = timing["b2_switches"]
    for i, s in enumerate(sw[:3]):
        nxt = sw[i + 1] if i + 1 < len(sw) else b["b2_flow"]["to"]
        out.append({"name": f"b2_flow_{chr(ord('a') + i)}", "frame": s + int((nxt - s) * 0.6)})
    cuts = timing["b3_cuts"]
    for tag, c in zip("abc", [cuts[0], cuts[len(cuts) // 2], cuts[-1]]):
        out.append({"name": f"b3_montage_{tag}", "frame": c["from"] + (c["to"] - c["from"]) // 2})
    out.append({"name": "b4_out", "frame": min(b["b4_out"]["from"] + 4, b["b4_out"]["to"] - 1)})  # 溶けている途中
    out.append({"name": "b5_title", "frame": b["b5_title"]["from"] + int((b["b5_title"]["to"] - b["b5_title"]["from"]) * 0.7)})
    return out
