"""時間の本籍。ビートの秒・②の切り替え・③のカット・効果音の打点をフレームで決める。

絵（Remotion）と音（ffmpeg・M2）は、どちらもここで作った `timing` だけを見る。
同じ数字から作るので、効果音の打点と絵の動きが必ず合う（Docs/OPENING_MOTION_PLAN.md §0）。

秒→フレームは「累積の秒を丸める」。ビートごとに丸めると端数が積み重なってずれるため。
"""
from __future__ import annotations

BEATS = ("b1_first", "b2_flow", "b3_montage", "b4_out", "b5_title")


def beat_seconds(meta: dict, given: dict | None = None) -> dict[str, float]:
    """各ビートの秒。指定が無いビートは meta の default_sec。"""
    given = given or {}
    return {b: float(given.get(b, meta["beats"][b]["default_sec"])) for b in BEATS}


def _bounds(fps: int, sec: dict[str, float]) -> dict[str, dict]:
    out, acc, prev = {}, 0.0, 0
    for b in BEATS:
        acc += sec[b]
        end = round(acc * fps)
        out[b] = {"from": prev, "to": end}
        prev = end
    return out


def _switches(rng: dict, count: int, switch_sec: list[float] | None, fps: int) -> list[int]:
    """②の語の切り替え（絶対フレーム）。switch_sec（②の頭からの秒・M2 でナレーションから来る）が
    無ければ等分する。"""
    start, length = rng["from"], rng["to"] - rng["from"]
    if switch_sec:
        return [start + min(length - 1, round(s * fps)) for s in switch_sec]
    return [start + round(i * length / count) for i in range(count)]


def _cuts(rng: dict, material_count: int, cfg: dict, fps: int) -> list[dict]:
    """③のカット。最初の長さから decay 倍ずつ縮め、min を下回らない。
    min に満たない端数は、先頭（長い方）のカットに1フレームずつ配る。最後に足すと終盤で伸びて
    「だんだん速く④へなだれ込む」が崩れるため。"""
    start, remaining = rng["from"], rng["to"] - rng["from"]
    min_f = max(2, round(cfg["min_sec"] * fps))
    cur = cfg["start_sec"] * fps
    lens: list[int] = []
    while remaining >= min_f:
        n = min(remaining, max(min_f, round(cur)))
        lens.append(n)
        remaining -= n
        cur *= cfg["decay"]
    if not lens:  # ③が min より短い
        lens, remaining = [remaining], 0
    for i in range(remaining):
        lens[i % len(lens)] += 1
    out, pos = [], start
    for i, n in enumerate(lens):
        out.append({"from": pos, "to": pos + n, "material": i % material_count})
        pos += n
    return out


def build_timing(meta: dict, sec: dict[str, float], keyword_count: int, material_count: int,
                 switch_sec: list[float] | None = None) -> dict:
    fps = int(meta["fps"])
    beats = _bounds(fps, sec)
    switches = _switches(beats["b2_flow"], keyword_count, switch_sec, fps)
    cuts = _cuts(beats["b3_montage"], material_count, meta["beats"]["b3_montage"]["cuts"], fps)

    sfx = []
    for cue in meta.get("sfx_cues", []):
        b, on, kind = cue["beat"], cue["on"], cue["kind"]
        if on == "start":
            frames = [beats[b]["from"]]
        elif on == "switch":
            frames = switches
        elif on == "cut":
            frames = [c["from"] for c in cuts]
        else:
            raise ValueError(f"unknown sfx cue: {on}")
        sfx.extend({"frame": f, "kind": kind} for f in frames)
    sfx.sort(key=lambda x: (x["frame"], x["kind"]))

    return {
        "fps": fps,
        "total_frames": beats["b5_title"]["to"],
        "beats": beats,
        "b2_switches": switches,
        "b3_cuts": cuts,
        "sfx": sfx,
    }


# ===== 演出プラン（型 v2）の時間: 拍の格子 ===========================================================
# 1小節＝4拍＝240/bpm 秒。ショットの長さは小節（0.25 の倍数＝1拍単位）で書く。
# 位置は「拍」の整数で持ち、フレームへは累積の位置から丸める（ずれが積み重ならない・v1 と同じ考え方）。

BEATS_PER_BAR = 4


def bar_sec(bpm: float) -> float:
    return 240.0 / bpm


def to_beats(bars: float) -> int:
    """小節→拍。1拍単位でなければ ValueError（検査が先に指摘する）。"""
    b = bars * BEATS_PER_BAR
    if abs(b - round(b)) > 1e-6:
        raise ValueError(f"{bars} 小節は1拍（0.25 小節）の倍数ではありません")
    return int(round(b))


def beat_frame(bpm: float, fps: int, beat: float) -> int:
    return round(beat * 60.0 / bpm * fps)


def default_bars(meta: dict, beat: str, bpm: float) -> float:
    """書かなかったビートの長さ: v1 の既定の秒を一番近い拍に丸めたもの（最低1拍）。"""
    sec = meta["beats"][beat]["default_sec"]
    beats = max(1, round(sec / (60.0 / bpm)))
    return beats / BEATS_PER_BAR


def bar_position(start_beat: int) -> dict:
    """「3小節目の2拍目」（1始まり）。"""
    return {"bar_no": start_beat // BEATS_PER_BAR + 1, "beat_no": start_beat % BEATS_PER_BAR + 1}


def k2_switch_beats(length_beats: int, words: int) -> list[float]:
    """K2 の語の切り替え位置（ショットの頭からの拍）。等分を一番近い拍に、語が拍より多ければ半拍に吸着。
    重なったら次の空きへ送る。収まらなければ ValueError（検査が先に「語が多すぎる」と指摘する）。"""
    unit = 1.0 if words <= length_beats else 0.5
    slots = int(length_beats / unit)
    if words > slots:
        raise ValueError("語が多すぎます")
    out: list[float] = []
    nxt = 0
    for i in range(words):
        want = round(i * length_beats / words / unit)
        want = min(max(want, nxt), slots - (words - i))
        out.append(want * unit)
        nxt = want + 1
    return out


def snap_cuts(cuts: list[dict], start: int, end: int, f16: float) -> list[dict]:
    """K3 のカットの境界を16分に吸着させる（切り替えを拍の格子に乗せる）。
    吸着で長さが2フレーム未満になるカットは前に束ねる。"""
    bounds = [c["from"] for c in cuts][1:]
    keep: list[int] = []
    prev = start
    for b in bounds:
        s = start + round(round((b - start) / f16) * f16)
        if s - prev >= 2 and end - s >= 2:
            keep.append(s)
            prev = s
    edges = [start] + keep + [end]
    return [{"from": edges[i], "to": edges[i + 1], "material": i} for i in range(len(edges) - 1)]


def plan_timeline(meta: dict, bpm: float, shots: list[dict]) -> dict:
    """演出プランの時間の表。shots は beat の順に並べた平らなリスト:
        {"id","beat","gimmick"（正式な ID）,"bars","enter":{"type","bars",...}|None,
         "n_words"（K2）,"n_materials"・"cut_cfg"（K3）}
    返り値の shots には from/to（フレーム）・hold_to・位置（小節・拍）・K2 の切り替え・K3 のカットが付く。
    ④（K4_out）は直前のショットの上に重なる＝直前のショットは④の終わりまで描き続ける（hold_to）。"""
    fps = int(meta["fps"])
    f16 = fps * 60.0 / bpm / 4
    out: list[dict] = []
    acc = 0
    for s in shots:
        e = s.get("enter")
        eb = to_beats(e["bars"]) if e and e.get("type") not in (None, "cut") else 0
        start_beat = acc - eb
        end_beat = start_beat + to_beats(s["bars"])
        frm, to = beat_frame(bpm, fps, start_beat), beat_frame(bpm, fps, end_beat)
        item = {"id": s["id"], "beat": s["beat"], "gimmick": s["gimmick"], "from": frm, "to": to, "hold_to": to,
                "start_beat": start_beat, "end_beat": end_beat, **bar_position(start_beat)}
        if eb:
            item["enter"] = {"type": e["type"], "from": frm, "to": beat_frame(bpm, fps, start_beat + eb),
                             "beats": eb, "x": e.get("x"), "y": e.get("y")}
        else:
            item["enter"] = None
        if s["gimmick"] == "K2_flow":
            n = end_beat - start_beat
            item["switches"] = [beat_frame(bpm, fps, start_beat + b) for b in k2_switch_beats(n, s["n_words"])]
        if s["gimmick"] == "K3_montage":
            raw = _cuts({"from": frm, "to": to}, s["n_materials"], s["cut_cfg"], fps)
            item["cuts"] = snap_cuts(raw, frm, to, f16)
        out.append(item)
        acc = end_beat
    for i, it in enumerate(out):
        if it["gimmick"] == "K4_out" and i > 0:
            out[i - 1]["hold_to"] = max(out[i - 1]["hold_to"], it["to"])
    beats: dict[str, dict] = {}
    for it in out:
        b = beats.setdefault(it["beat"], {"from": it["from"], "to": it["to"]})
        b["from"], b["to"] = min(b["from"], it["from"]), max(b["to"], it["to"])
    return {"fps": fps, "bpm": bpm, "bar_sec": bar_sec(bpm), "total_frames": out[-1]["to"] if out else 0,
            "total_sec": round((out[-1]["to"] if out else 0) / fps, 3), "shots": out, "beats": beats,
            "sfx": plan_sfx(out)}


def plan_sfx(shots: list[dict]) -> list[dict]:
    """効果音の打点（フレーム）。短いカットの間引きは M2（音の組み立て）で決める。"""
    sfx: list[dict] = []
    for it in shots:
        g = it["gimmick"]
        if g == "K2_flow":
            sfx.extend({"frame": f, "kind": "whoosh"} for f in it["switches"])
        elif g == "K3_montage":
            sfx.extend({"frame": c["from"], "kind": "hit"} for c in it["cuts"])
        elif g == "K4_out":
            sfx.append({"frame": it["from"], "kind": "flash"})
        elif g == "K5_title":
            sfx.append({"frame": it["from"], "kind": "riser"})
        else:
            sfx.append({"frame": it["from"], "kind": "impact"})
    sfx.sort(key=lambda x: (x["frame"], x["kind"]))
    return sfx
