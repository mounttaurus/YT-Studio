"""HyperFrames の辞書（ブロック）の口（Docs/MOTION_UNIFIED_PLAN.md §5-3・§6・I1）。

- 本籍: `motion-agent/hyperframes/blocks/{block_id}/block.json`（名前・種別・用途・変数の制約・文字・素材・明滅・出し方）と
  `block.html`（サブコンポジション。`<template>` の中にスタイル・マークアップ・スクリプト。変数は `data-composition-variables`）。
- 使う時は**プロジェクトの `compositions/blocks/{id}.html` へ写す**（M7: 辞書を直しても過去のインサートは変わらない）。
  index.html に `data-composition-src="compositions/blocks/L1_name.html"` の host を書けば、`prepare` が辞書から写す
  （すでにあれば上書きしない＝凍結）。写した版と中身の指紋は `compositions/blocks/_lock.json` に残す。
- host の約束（公式 `sub-compositions.md`）: `data-composition-id` はブロックの id と同じ・`data-start`・`data-duration`・
  `data-width`・`data-height`。引数は `data-variable-values`（JSON）。目印に `data-gimmick`（ブロック id）・`data-variant`。
- 検査（check）: ブロックの存在・用途・変数の型と範囲・文字数・出し方・素材の実在。繰り返しの指紋（§8-3）。
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from app.core import config

ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,40}$")
KINDS = ("base", "layer", "enter", "overlay")
PROFILES = ("insert", "opening", "free")
BLOCKS_REL = "compositions/blocks"
LOCK_NAME = "_lock.json"
_VARS_RE = re.compile(r"""data-composition-variables\s*=\s*(?:'([^']*)'|"([^"]*)")""", re.S)
_SRC_RE = re.compile(rf"^{re.escape(BLOCKS_REL)}/([A-Za-z0-9_]+)\.html$")


def _issue(code: str, message: str, level: str = "error", file: str | None = None) -> dict:
    out = {"code": code, "level": level, "message": message}
    if file:
        out["file"] = file
    return out


def dictionary_dir() -> Path:
    return config.HF_DIR / "blocks"


# ── 辞書 ──

def parse_vars(block_html: str) -> list[dict]:
    """block.html の data-composition-variables（宣言の並び）。"""
    m = _VARS_RE.search(block_html)
    if not m:
        return []
    raw = html_lib.unescape(m.group(1) if m.group(1) is not None else m.group(2))
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _read_block(block_dir: Path) -> dict | None:
    meta_p, html_p = block_dir / "block.json", block_dir / "block.html"
    if not (meta_p.is_file() and html_p.is_file()):
        return None
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    text = html_p.read_text(encoding="utf-8")
    return {"meta": meta, "html": text, "vars": parse_vars(text), "path": html_p,
            "sha1": hashlib.sha1(text.encode("utf-8")).hexdigest()}


def load_all() -> dict[str, dict]:
    out: dict[str, dict] = {}
    base = dictionary_dir()
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if d.is_dir():
            b = _read_block(d)
            if b is not None:
                out[b["meta"].get("id", d.name)] = b
    return out


def list_blocks() -> list[dict]:
    """台帳（API・MCP が返す形）。"""
    out = []
    for bid, b in load_all().items():
        m = b["meta"]
        out.append({
            "id": bid, "label": m.get("label"), "kind": m.get("kind"), "version": m.get("version"),
            "profiles": m.get("profiles"), "summary": m.get("summary"), "variants": m.get("variants"),
            "vars": [{k: v for k, v in d.items() if k in ("id", "type", "label", "default", "min", "max", "maxLength", "options")}
                     for d in b["vars"]],
            "text": m.get("text"), "materials": m.get("materials"), "flash": m.get("flash"),
            "placement": m.get("placement"), "notes": m.get("notes"), "sha1": b["sha1"],
        })
    return out


def validate_dictionary() -> list[dict]:
    """辞書そのものの検査（テストで回す）。"""
    issues: list[dict] = []
    base = dictionary_dir()
    if not base.is_dir():
        return [_issue("no_dictionary", f"辞書が無い: {base}")]
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        b = _read_block(d)
        where = f"blocks/{d.name}"
        if b is None:
            issues.append(_issue("block_files", "block.json と block.html が要る", file=where))
            continue
        m, text = b["meta"], b["html"]
        bid = m.get("id")
        if bid != d.name or not ID_RE.match(str(bid)):
            issues.append(_issue("block_id", f"id はフォルダ名と同じ英数字・_: {bid!r}", file=where))
        if m.get("kind") not in KINDS:
            issues.append(_issue("block_kind", f"kind は {KINDS}: {m.get('kind')!r}", file=where))
        if not isinstance(m.get("profiles"), list) or not set(m["profiles"]) <= set(PROFILES) or not m["profiles"]:
            issues.append(_issue("block_profiles", f"profiles は {PROFILES} の部分集合", file=where))
        if not isinstance(m.get("version"), int):
            issues.append(_issue("block_version", "version は整数", file=where))
        if "<template" not in text or "</template>" not in text:
            issues.append(_issue("block_template", "<template> の中に書く（外は捨てられる）", file=where))
        if f'data-composition-id="{bid}"' not in text:
            issues.append(_issue("block_composition_id", f'内側の data-composition-id は "{bid}"', file=where))
        if f'window.__timelines["{bid}"]' not in text:
            issues.append(_issue("block_timeline", f'window.__timelines["{bid}"] に登録する', file=where))
        if re.search(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//""", text, re.I) or "fetch(" in text:
            issues.append(_issue("block_network", "外部の URL・fetch は使えない（M3）", file=where))
        if "Math.random" in text:
            issues.append(_issue("block_random", "Math.random は使えない（種つきの乱数を使う）", file=where))
        if not b["vars"]:
            issues.append(_issue("block_vars", "data-composition-variables が無い／読めない", file=where, level="warning"))
        ids = {v.get("id") for v in b["vars"]}
        var = (m.get("variants") or {}).get("var")
        if m.get("variants"):
            enum = next((v for v in b["vars"] if v.get("id") == var), None)
            opts = [o.get("value") for o in (enum or {}).get("options", [])]
            if enum is None or sorted(opts) != sorted(m["variants"].get("options", [])):
                issues.append(_issue("block_variants", f"variants.options と変数 {var} の options が合わない", file=where))
        for t in m.get("text") or []:
            if t.get("var") not in ids:
                issues.append(_issue("block_text_var", f"text の変数 {t.get('var')!r} が宣言に無い", file=where))
        for mt in m.get("materials") or []:
            if mt.get("var") not in ids:
                issues.append(_issue("block_material_var", f"materials の変数 {mt.get('var')!r} が宣言に無い", file=where))
        # ブロックの中の id はブロック名で始める（組み立てたページの中で一意にする）
        for el_id in re.findall(r"""(?<![\w-])id\s*=\s*["']([^"']+)["']""", text):
            if el_id != "root" and not el_id.startswith(f"{bid}-"):
                issues.append(_issue("block_element_id", f"要素の id は {bid}- で始める: {el_id!r}", file=where, level="warning"))
    return issues


# ── プロジェクトへ写す ──

class _HostParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if "data-composition-src" in a or "data-gimmick" in a:
            self.tags.append({"tag": tag, "attrs": a})


def hosts(index_html: str) -> list[dict]:
    """index.html の中のブロックの host（data-composition-src を持つ要素）。
    返り値の各要素: {id, block, src, variant, gimmick, vars, start, duration, attrs}。"""
    p = _HostParser()
    p.feed(index_html)
    out = []
    for t in p.tags:
        a = t["attrs"]
        src = a.get("data-composition-src")
        if not src:
            continue
        m = _SRC_RE.match(src)
        raw = a.get("data-variable-values", "")
        try:
            values = json.loads(raw) if raw else {}
        except ValueError:
            values = None
        out.append({"id": a.get("data-composition-id"), "block": m.group(1) if m else None, "src": src,
                    "variant": a.get("data-variant"), "gimmick": a.get("data-gimmick"),
                    "vars": values, "raw_vars": raw, "start": a.get("data-start"), "duration": a.get("data-duration"),
                    "attrs": a})
    return out


def sync_project(d: Path) -> list[str]:
    """index.html が使うブロックを辞書から compositions/blocks/ へ写す（無ければ。あれば触らない＝凍結）。
    返り値: 写したファイルの相対パス。"""
    index = d / "index.html"
    if not index.is_file():
        return []
    dic = load_all()
    made: list[str] = []
    lock_p = d / BLOCKS_REL / LOCK_NAME
    lock = json.loads(lock_p.read_text(encoding="utf-8")) if lock_p.is_file() else {}
    changed = False
    for h in hosts(index.read_text(encoding="utf-8", errors="replace")):
        bid = h["block"]
        if not bid or bid not in dic:
            continue
        dst = d / BLOCKS_REL / f"{bid}.html"
        if dst.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(dic[bid]["html"].encode("utf-8"))
        lock[bid] = {"version": dic[bid]["meta"].get("version"), "sha1": dic[bid]["sha1"]}
        made.append(f"{BLOCKS_REL}/{bid}.html")
        changed = True
    if changed:
        lock_p.write_text(json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        made.append(f"{BLOCKS_REL}/{LOCK_NAME}")
    return made


# ── 検査 ──

def _check_value(spec: dict, value, label: str) -> str | None:
    t = spec.get("type")
    if t == "string":
        if not isinstance(value, str):
            return f"{label}: 文字列: {value!r}"
        ml = spec.get("maxLength")
        if ml is not None and len(value) > ml:
            return f"{label}: {ml} 字まで（{len(value)} 字）"
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{label}: 数: {value!r}"
        if spec.get("min") is not None and value < spec["min"]:
            return f"{label}: {spec['min']} 以上: {value}"
        if spec.get("max") is not None and value > spec["max"]:
            return f"{label}: {spec['max']} 以下: {value}"
    elif t == "boolean":
        if not isinstance(value, bool):
            return f"{label}: true／false: {value!r}"
    elif t == "enum":
        opts = [o.get("value") for o in spec.get("options", [])]
        if value not in opts:
            return f"{label}: {opts} のどれか: {value!r}"
    return None


def check(d: Path, motion: dict | None) -> list[dict]:
    """ブロックの使い方の検査。ブロックを使っていなければ空。"""
    index = d / "index.html"
    if not index.is_file():
        return []
    profile = (motion or {}).get("profile")
    dic = load_all()
    issues: list[dict] = []
    for h in hosts(index.read_text(encoding="utf-8", errors="replace")):
        bid, hid = h["block"], h["id"] or "?"
        where = f"index.html（{hid}）"
        if bid is None:
            # compositions/custom/ などの、その場の部品（I2）。ここでは形だけ
            continue
        b = dic.get(bid)
        locked = (d / BLOCKS_REL / f"{bid}.html").is_file()
        if b is None:
            if not locked:
                issues.append(_issue("block_unknown", f"辞書にも compositions/blocks/ にも無いブロック: {bid}", file=where))
            continue
        m = b["meta"]
        if hid != bid:
            issues.append(_issue("block_host_id", f'host の data-composition-id は "{bid}" と同じにする（違うと動かない）', file=where))
        if h["gimmick"] != bid:
            issues.append(_issue("block_host_gimmick", f'data-gimmick="{bid}" を付ける（繰り返しの検査が読む）', level="warning", file=where))
        if profile and profile not in (m.get("profiles") or []):
            issues.append(_issue("block_profile", f"{bid} は用途 {m.get('profiles')} 用（この話は {profile}）", file=where))
        for need in ("data-start", "data-duration", "data-width", "data-height"):
            if need not in h["attrs"]:
                issues.append(_issue("block_host_attr", f"host に {need} が要る", file=where))
        if h["vars"] is None:
            issues.append(_issue("block_vars_json", "data-variable-values が JSON として読めない", file=where))
            continue
        decl = {v["id"]: v for v in b["vars"] if "id" in v}
        for key, val in h["vars"].items():
            spec = decl.get(key)
            if spec is None:
                issues.append(_issue("block_unknown_var", f"{bid}: 宣言に無い変数 {key}（{sorted(decl)}）", level="warning", file=where))
                continue
            msg = _check_value(spec, val, f"{bid}.{key}")
            if msg:
                issues.append(_issue("block_bad_var", msg, file=where))
        var = (m.get("variants") or {}).get("var")
        if var:
            chosen = h["vars"].get(var, (m["variants"].get("default")))
            if h["variant"] and h["variant"] != chosen:
                issues.append(_issue("block_variant_mismatch", f"data-variant={h['variant']!r} と変数 {var}={chosen!r} が違う", file=where))
        for t in m.get("text") or []:
            val = h["vars"].get(t["var"])
            if isinstance(val, str) and t.get("max_chars") and len(val) > t["max_chars"]:
                issues.append(_issue("block_text_long", f"{bid}.{t['var']}: {t['max_chars']} 字まで（{len(val)} 字）。一瞬で読める量にする", file=where))
        for mt in m.get("materials") or []:
            path = h["vars"].get(mt["var"])
            if isinstance(path, str) and path:
                p = (d / path).resolve()
                if d.resolve() not in p.parents or not p.is_file():
                    issues.append(_issue("material_missing", f"{bid}.{mt['var']}: プロジェクトの中に無い素材: {path}", file=where))
    return issues


def fingerprint(d: Path) -> list[str]:
    """組み合わせの指紋（§8-3）: ブロックの id と出し方の並び（色・秒は含めない・種は含めない）。"""
    index = d / "index.html"
    if not index.is_file():
        return []
    out = []
    for h in hosts(index.read_text(encoding="utf-8", errors="replace")):
        if h["block"]:
            out.append(f"{h['block']}:{h['variant'] or (h['vars'] or {}).get('variant') or ''}")
    return sorted(out)
