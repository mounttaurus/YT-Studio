"""型ごとの「描画入力の検査と props の組み立て」の担当を選ぶ。

meta.json の "builder" で決まる（無ければ "beats"＝kinetic_teaser のビート型）。
担当は同じ3つの関数を持つ: validate_input(meta, inp, shared_dir) / build_props(meta, inp, asset_base) /
still_frames(props)。
"""
from __future__ import annotations

from types import ModuleType

from app.core import lab, plan, props

_BUILDERS: dict[str, ModuleType] = {"beats": props, "lab": lab, "plan": plan}


def for_meta(meta: dict) -> ModuleType:
    name = meta.get("builder", "beats")
    if name not in _BUILDERS:
        raise ValueError(f"unknown builder: {name}")
    return _BUILDERS[name]
