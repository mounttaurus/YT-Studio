"""演出プランの明滅の概算（描く前に、プランの宣言から）。数え方は flash.py（実測）と同じ。

見るもの: ショットの切れ目の外周の色の明るさの差・K2 の語の切り替え（背景→文字色の一瞬の光→背景）・
K4 のアウト・K5 の現れ方・A3_light の白への往復。K3（写真）は明るさが素材しだいなので、概算では
カットの速さ（毎秒3回を超える区間）を警告に回し、実測に任せる。
"""
from __future__ import annotations

from app.core import flash, registry

K2_FLASH_FRAMES = 3  # B2Flow.tsx: 切り替えの瞬間に文字色で光る長さ


def _sign(a: float, b: float) -> int:
    return 1 if b > a else -1


def estimate(base: dict, variant: dict, shots: list[dict], out_color: str | None, fps: int) -> dict:
    """shots は timeline の shots（gimmick・from・to・enter・switches・cuts）。"""
    pal = base["variants"]["palette"]["options"][variant["palette"]]
    lum = flash.hex_luminance
    flow = pal["flow"]

    def backdrop(gid: str) -> str | None:
        key = (registry.gimmick(gid) or {}).get("backdrop")
        if key is None:
            return None
        if key == "palette.base":
            return pal["base"]
        if key == "palette.flow0":
            return flow[0][0]
        if key == "palette.title_bg":
            return pal["title_bg"]
        return key  # #rrggbb

    out_hex = {"white": "#ffffff", "black": "#000000"}.get(out_color or "black", "#000000")
    events: list[tuple[int, int]] = []
    warnings: list[str] = []
    prev_color: str | None = None  # 直前に画面に出ている外周の色
    for s in shots:
        gid = s["gimmick"]
        if gid == "K4_out":
            y0 = lum(prev_color or pal["base"])
            if flash.is_transition(y0, lum(out_hex)):
                events.append((s["from"] + (4 if out_hex == "#ffffff" else 7), _sign(y0, lum(out_hex))))
            prev_color = out_hex
            continue
        color = backdrop(gid)
        enter = s.get("enter")
        if color and prev_color and not enter:
            y0, y1 = lum(prev_color), lum(color)
            when = s["from"] + (6 if gid == "K5_title" else 0)
            if flash.is_transition(y0, y1):
                events.append((when, _sign(y0, y1)))
        if enter and enter["type"] == "A3_light":
            n = enter["to"] - enter["from"]
            events.append((enter["from"] + round(n * 0.4), 1))
            events.append((enter["from"] + round(n * 0.7), -1))
        if gid == "K2_flow":
            sw = s["switches"]
            for k, f in enumerate(sw):
                bg_prev = flow[(k - 1) % len(flow)][0] if k > 0 else (prev_color or flow[0][0])
                bg, fg = flow[k % len(flow)]
                if k > 0 and flash.is_transition(lum(bg_prev), lum(fg)):
                    events.append((f, _sign(lum(bg_prev), lum(fg))))
                if flash.is_transition(lum(fg), lum(bg)):
                    events.append((f + K2_FLASH_FRAMES, _sign(lum(fg), lum(bg))))
        if gid == "K3_montage":
            starts = [c["from"] for c in s["cuts"]]
            worst = max((sum(1 for x in starts if f <= x < f + fps) for f in starts), default=0)
            if worst > flash.MAX_PER_SEC:
                warnings.append(f"K3 のカットが毎秒 {worst} 回の区間があります。素材の明るさが離れていると明滅の上限（毎秒3回）を超える"
                                "（描画後の実測 flash で確かめる。超えたら min_sec を伸ばすか montage_tone: mono で明るさを寄せる）")
        prev_color = color or prev_color
    runs = flash.runs_from_events(events)
    return {**flash.verdict(runs, fps), "warnings": warnings}
