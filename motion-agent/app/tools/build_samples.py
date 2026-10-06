"""見本の props を作り直す: samples/{name}.input.json → samples/{name}.props.json

props.json は Remotion Studio の既定値（Root.tsx が読む）なので、timing や meta を変えたら必ず流す。
テスト（tests/test_samples.py）が「作り直した結果とファイルが一致するか」を見ている。

    python -m app.tools.build_samples [--check]
"""
from __future__ import annotations

import json
import sys

from app.core import builders
from app.core.templates import get_meta, list_samples, list_templates, load_sample_input, template_dir


def render_sample(template_id: str, version: int, name: str) -> str:
    meta = get_meta(template_id, version)
    inp = load_sample_input(template_id, version, name)
    b = builders.for_meta(meta)
    errors = b.validate_input(meta, inp)
    if errors:
        raise ValueError(f"{template_id} v{version} {name}: {errors}")
    props = b.build_props(meta, inp, "")
    # 試作場の props は点群（数千点）を含むので詰めて書く（読みやすさより大きさ）
    indent = None if meta.get("builder") in ("lab", "plan") else 2
    return json.dumps(props, ensure_ascii=False, indent=indent) + "\n"


def main(check: bool) -> int:
    stale = []
    for t in list_templates():
        for name in list_samples(t["template_id"], t["version"]):
            path = template_dir(t["template_id"], t["version"]) / "samples" / f"{name}.props.json"
            text = render_sample(t["template_id"], t["version"], name)
            current = path.read_text(encoding="utf-8") if path.is_file() else None
            if current != text:
                stale.append(str(path))
                if not check:
                    path.write_bytes(text.encode("utf-8"))
    for p in stale:
        print(("要更新: " if check else "更新: ") + p)
    return 1 if (check and stale) else 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
