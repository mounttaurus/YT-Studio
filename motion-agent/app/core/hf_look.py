"""見た目の束（look）の口（Docs/MOTION_UNIFIED_PLAN.md §5-5・I-P4・I1）。

チャンネルの見た目の語彙（色・書体）を1つの束にして、組み立ては毎回変えても統一感を保つ。
- 本籍: `shared/motion/looks/{look_id}.json`（チャンネルのデータ・公開リポに載らない）。
  無ければ辞書側の `motion-agent/hyperframes/looks/{look_id}.json`（見本・候補）を使う。
- `motion.json` の `look` で選ぶ。`prepare` が `look.css`（CSS の変数）をプロジェクトに置く。
  index.html は `<link rel="stylesheet" href="look.css" />` で読む。ブロックは `var(--look-*, 既定)` で受ける。
- 書体は家族名を CSS の変数で渡す（`--look-font-name: "Noto Serif CJK JP"`）。実体はコンテナの fontconfig が持つ。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.core import config

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
COLORS = ("desk", "paper", "ink", "ink_dim", "accent")
FONTS = ("name", "sub", "mono", "hand")
LINK_RE = re.compile(r"""<link\b[^>]*href\s*=\s*["']look\.css["']""", re.I)


def _issue(code: str, message: str, level: str = "error", file: str | None = "motion.json") -> dict:
    out = {"code": code, "level": level, "message": message}
    if file:
        out["file"] = file
    return out


def _dirs() -> list[Path]:
    return [config.SHARED_DIR / "motion" / "looks", config.HF_DIR / "looks"]


def resolve(look_id: str) -> dict | None:
    """look_id → 束（辞書型）。チャンネルのデータを先に見る。"""
    if not isinstance(look_id, str) or not ID_RE.match(look_id):
        return None
    for base in _dirs():
        p = base / f"{look_id}.json"
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def list_looks() -> list[dict]:
    out: dict[str, dict] = {}
    for base in reversed(_dirs()):          # 後ろ（辞書側）を先に入れ、チャンネルのデータで上書き
        if base.is_dir():
            for p in sorted(base.glob("*.json")):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                out[p.stem] = {"id": p.stem, "label": d.get("label"), "colors": d.get("colors"), "fonts": d.get("fonts"),
                               "source": "channel" if base == config.SHARED_DIR / "motion" / "looks" else "dictionary"}
    return list(out.values())


def validate(look: dict) -> list[str]:
    msgs = []
    colors, fonts = look.get("colors") or {}, look.get("fonts") or {}
    for k in COLORS:
        if not COLOR_RE.match(str(colors.get(k, ""))):
            msgs.append(f"colors.{k} は #rrggbb: {colors.get(k)!r}")
    for k in FONTS:
        f = fonts.get(k)
        if not isinstance(f, str) or not f.strip() or '"' in f or ";" in f or "{" in f:
            msgs.append(f"fonts.{k} は書体の家族名（引用符・; ・{{ を含まない）: {f!r}")
    return msgs


def css(look: dict) -> str:
    """束 → look.css の中身。順序は固定（同じ束なら同じバイト列）。"""
    c, f = look.get("colors") or {}, look.get("fonts") or {}
    lines = ["/* motion-agent が motion.json の look から作る。手で直さない（次の準備で戻る） */", ":root {"]
    for k in COLORS:
        if k in c:
            lines.append(f"  --look-{k.replace('_', '-')}: {c[k]};")
    for k in FONTS:
        if k in f:
            lines.append(f'  --look-font-{k}: "{f[k]}";')
    lines.append("}")
    return "\n".join(lines) + "\n"


def sync(d: Path, motion: dict | None) -> list[str]:
    """motion.json の look があれば look.css を置く（中身が同じなら書かない）。返り値: 書いたファイル。"""
    look_id = (motion or {}).get("look")
    if not look_id:
        return []
    look = resolve(look_id)
    if look is None or validate(look):
        return []
    data = css(look).encode("utf-8")
    p = d / "look.css"
    if p.is_file() and p.read_bytes() == data:
        return []
    p.write_bytes(data)
    return ["look.css"]


def check(d: Path, motion: dict | None) -> list[dict]:
    look_id = (motion or {}).get("look")
    if not look_id:
        return []
    look = resolve(look_id)
    if look is None:
        return [_issue("look_unknown", f"見た目の束 {look_id!r} が無い（shared/motion/looks/ か辞書の looks/）")]
    issues = [_issue("look_invalid", m) for m in validate(look)]
    index = d / "index.html"
    if index.is_file() and not LINK_RE.search(index.read_text(encoding="utf-8", errors="replace")):
        issues.append(_issue("look_not_linked", '<head> に <link rel="stylesheet" href="look.css" /> を入れる（束の色・書体が効かない）',
                             level="warning", file="index.html"))
    return issues
