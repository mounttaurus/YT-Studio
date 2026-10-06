"""型（テンプレート）の一覧と meta.json の読み込み。

型は `remotion/src/templates/{template_id}/v{N}/` に置く。制約の本籍は各版の `meta.json`
（TypeScript 側も同じファイルを読む）。見本は `samples/{name}.input.json`（描画入力）と、
そこから Python が作る `samples/{name}.props.json`（Remotion Studio の既定値）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.config import TEMPLATES_DIR

_ID_RE = re.compile(r"^[a-z0-9_]+$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class TemplateNotFound(KeyError):
    pass


def template_dir(template_id: str, version: int, root: Path | None = None) -> Path:
    if not _ID_RE.match(template_id) or int(version) < 1:
        raise TemplateNotFound(f"{template_id} v{version}")
    d = (root or TEMPLATES_DIR) / template_id / f"v{int(version)}"
    if not (d / "meta.json").is_file():
        raise TemplateNotFound(f"{template_id} v{version}")
    return d


def get_meta(template_id: str, version: int, root: Path | None = None) -> dict:
    return json.loads((template_dir(template_id, version, root) / "meta.json").read_text(encoding="utf-8"))


def list_samples(template_id: str, version: int, root: Path | None = None) -> list[str]:
    d = template_dir(template_id, version, root) / "samples"
    return sorted(p.name[: -len(".input.json")] for p in d.glob("*.input.json")) if d.is_dir() else []


def load_sample_input(template_id: str, version: int, name: str, root: Path | None = None) -> dict:
    if not _NAME_RE.match(name):
        raise TemplateNotFound(f"sample {name}")
    p = template_dir(template_id, version, root) / "samples" / f"{name}.input.json"
    if not p.is_file():
        raise TemplateNotFound(f"sample {name}")
    return json.loads(p.read_text(encoding="utf-8"))


def list_templates(root: Path | None = None) -> list[dict]:
    base = root or TEMPLATES_DIR
    out = []
    for meta_path in sorted(base.glob("*/v*/meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        tid, ver = meta["template_id"], meta["version"]
        out.append({
            "template_id": tid,
            "version": ver,
            "composition_id": meta["composition_id"],
            "title": meta.get("title", tid),
            "summary": meta.get("summary", ""),
            "max_total_sec": meta.get("max_total_sec"),
            "samples": list_samples(tid, ver, base),
        })
    return out
