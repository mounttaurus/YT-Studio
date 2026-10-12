"""HyperFrames のコンポジションに効果音を置く道具（Docs/MOTION_UNIFIED_PLAN.md §9-1・A0）。

本籍は motion.json の2つの欄（M1: 打点は Python 側が持ち、HTML はそれを写す）:

    "sounds": {"tick_a": {"kind": "tick", "length": 0.05, "pitch": 2300, "gain": 0.35, "seed": 11}}
    "sfx":    [{"id": "sfx-name-00", "sound": "tick_a", "at": 0.8, "volume": 1.0}]

- sounds: 音の定義。名前 → 部品（sfx_synth.PARTS）の引数。ここから `assets/sfx/{名前}.wav` を作る（M2: 同じ引数なら同じ WAV）。
- sfx:    置き場所。`id` は <audio> の id（ページの中で一意）、`at` は打点（秒）、`volume` は置く側の倍率（既定 1・最大 3.98）。
          `at` は部品の基準点（sfx_synth.anchor）: 頭が打点＝既定。riser・suck は**終わり**が打点で、
          `data-start` は `at − length` になる（打点から逆算）。

連打（タイピングなど）の書き方（A0 の続き）。1件で何十個も置ける。展開した id は `{id}-00`, `{id}-01`…:

    {"id": "sfx-type", "sound": ["key_a", "key_b", "key_c"], "at": 0.8, "volume": 0.8,
     "repeat": {"count": 13, "every": 0.07, "jitter": 0.012, "volume_jitter": 0.25, "seed": 5}}
    {"id": "sfx-hits", "sound": "pop", "at": [1.0, 1.4, 2.2]}            # 打点を並べる

- `sound` は名前の並びでもよい（順に使い回す＝同じ音の連打にしない）。
- `repeat`: count（最大 200）・every（秒）・jitter（打点を ±この秒だけ揺らす・種で決まる）・volume_jitter（0〜1・音量を下へ揺らす）・seed。
  絵の側（GSAP の文字送り）は揺らさず `every` の等間隔にし、音だけを少し揺らすと人が打っているように聞こえる。

`prepare` が WAV を作り、index.html のルートの中に <audio> を書く。書く範囲は次の目印の間だけ:

    <!-- sfx:begin … -->  …  <!-- sfx:end -->

目印が無ければルートの開始タグの直後に作る。手で直しても次の準備で motion.json の通りに戻る。
公式の決まり（hyperframes-core）: audio には必ず id・crossorigin は付けない。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core import hf_checks
from app.core.audio import sfx_synth

SFX_DIR = "assets/sfx"
LANE_BASE = 10                   # Studio のタイムラインの段（表示だけ。重なる音は別の段へ）
MAX_VOLUME = 3.98                # data-volume の上限（+12dB）
BLOCK_BEGIN = "<!-- sfx:begin 効果音（motion.json の sfx から motion-agent が書く。手で直さない） -->"
BLOCK_END = "<!-- sfx:end -->"
_BLOCK_RE = re.compile(r"[ \t]*<!--\s*sfx:begin\b.*?-->.*?<!--\s*sfx:end\s*-->[ \t]*(?:\r?\n)?", re.S)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
_AUDIO_RE = re.compile(r"<audio\b[^>]*>", re.I | re.S)
_ATTR_RE = re.compile(r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_INDENT = "      "


def _issue(code: str, message: str, level: str = "error", file: str | None = "motion.json") -> dict:
    out = {"code": code, "level": level, "message": message}
    if file:
        out["file"] = file
    return out


def has_sfx(motion: dict | None) -> bool:
    m = motion or {}
    return bool(m.get("sounds") or m.get("sfx"))


def sound_file(name: str) -> str:
    return f"{SFX_DIR}/{name}.wav"


MAX_REPEAT = 200   # 1件で置ける個数の上限（<audio> が増えすぎない）


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _expand(cid: str, c: dict) -> tuple[list[dict], list[dict]]:
    """1件の置き場所を、置く音1つずつの {id, sound, at, volume} に展開する。
    単発（at が数）なら id はそのまま。at が並び・repeat なら `{id}-NN`。誤りは (空, 指摘)。"""
    snd, at, rep = c.get("sound"), c.get("at"), c.get("repeat")
    vol = c.get("volume", 1.0)
    names = snd if isinstance(snd, list) else [snd]
    if not names or not all(isinstance(n, str) for n in names):
        return [], [_issue("sfx_unknown_sound", f"{cid}: sound は音の名前か、名前の並び: {snd!r}")]
    if rep is None and not isinstance(at, list):
        return [{"id": cid, "sound": names[0], "at": at, "volume": vol}], []
    if rep is not None:
        if not isinstance(rep, dict):
            return [], [_issue("sfx_bad_repeat", f"{cid}: repeat は {{count, every, jitter, volume_jitter, seed}} の辞書")]
        if isinstance(at, list):
            return [], [_issue("sfx_bad_repeat", f"{cid}: repeat と at の並びは同時に使えない（at は始めの1つだけ）")]
        count, every = rep.get("count"), rep.get("every")
        jitter, vj, seed = rep.get("jitter", 0.0), rep.get("volume_jitter", 0.0), rep.get("seed", 0)
        if isinstance(count, bool) or not isinstance(count, int) or not (1 <= count <= MAX_REPEAT):
            return [], [_issue("sfx_bad_repeat", f"{cid}: repeat.count は 1〜{MAX_REPEAT} の整数: {count!r}")]
        if not _is_num(every) or every < 0.01:
            return [], [_issue("sfx_bad_repeat", f"{cid}: repeat.every は 0.01 秒以上: {every!r}")]
        if not _is_num(jitter) or not (0 <= jitter <= every / 2):
            return [], [_issue("sfx_bad_repeat", f"{cid}: repeat.jitter は 0〜every/2 秒: {jitter!r}")]
        if not _is_num(vj) or not (0 <= vj <= 1):
            return [], [_issue("sfx_bad_repeat", f"{cid}: repeat.volume_jitter は 0〜1: {vj!r}")]
        if isinstance(seed, bool) or not isinstance(seed, int):
            return [], [_issue("sfx_bad_repeat", f"{cid}: repeat.seed は整数: {seed!r}")]
        if not _is_num(at) or at < 0:
            return [], [_issue("sfx_bad_at", f"{cid}: at は 0 以上の秒: {at!r}")]
        if not _is_num(vol):
            return [], [_issue("sfx_bad_volume", f"{cid}: volume は 0〜{MAX_VOLUME}: {vol!r}")]
        ut = sfx_synth._unit(count, seed, 1)
        uv = sfx_synth._unit(count, seed, 2)
        out = []
        for k in range(count):
            t = float(at) + k * float(every) + (float(ut[k]) * 2.0 - 1.0) * float(jitter)
            out.append({"id": f"{cid}-{k:02d}", "sound": names[k % len(names)], "at": max(0.0, round(t, 4)),
                        "volume": round(float(vol) * (1.0 - float(vj) * float(uv[k])), 4)})
        return out, []
    # at の並び
    if not at or len(at) > MAX_REPEAT or not all(_is_num(t) and t >= 0 for t in at):
        return [], [_issue("sfx_bad_at", f"{cid}: at の並びは 0 以上の秒を1〜{MAX_REPEAT}個: {at!r}")]
    if not _is_num(vol):
        return [], [_issue("sfx_bad_volume", f"{cid}: volume は 0〜{MAX_VOLUME}: {vol!r}")]
    return [{"id": f"{cid}-{k:02d}", "sound": names[k % len(names)], "at": float(t), "volume": vol}
            for k, t in enumerate(at)], []


def resolve(motion: dict | None) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """motion.json の sounds・sfx を読み、検査して解く。
    返り値: (置く音のリスト, 有効な音の定義 {名前: 正規化した引数}, 指摘)。誤りのある項目は落とす。"""
    m = motion or {}
    issues: list[dict] = []
    sounds: dict[str, dict] = {}
    raw_sounds = m.get("sounds") or {}
    if not isinstance(raw_sounds, dict):
        return [], {}, [_issue("sfx_bad_sounds", "sounds は {名前: 引数} の辞書")]
    for name, spec in raw_sounds.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            issues.append(_issue("sfx_bad_sound_name", f"音の名前は英数字・-・_ だけ（48字まで）: {name!r}"))
            continue
        if not isinstance(spec, dict) or "kind" not in spec:
            issues.append(_issue("sfx_bad_sound", f"音 {name} は {{kind, length, pitch, gain, seed}} の辞書"))
            continue
        kind = spec["kind"]
        if kind not in sfx_synth.PARTS:
            issues.append(_issue("sfx_unknown_kind", f"音 {name}: 部品は {sorted(sfx_synth.PARTS)} のどれか: {kind!r}"))
            continue
        try:
            sounds[name] = {"kind": kind, **sfx_synth.normalize_params(kind, **{k: v for k, v in spec.items() if k != "kind"})}
        except ValueError as e:
            issues.append(_issue("sfx_bad_param", f"音 {name}: {e}"))

    raw_sfx = m.get("sfx") or []
    if not isinstance(raw_sfx, list):
        return [], sounds, issues + [_issue("sfx_bad_sfx", "sfx は置き場所の並び")]
    cues: list[dict] = []
    seen: set[str] = set()
    for i, c in enumerate(raw_sfx):
        label = f"sfx[{i}]"
        if not isinstance(c, dict):
            issues.append(_issue("sfx_bad_cue", f"{label} は辞書"))
            continue
        cid = c.get("id")
        if not isinstance(cid, str) or not _NAME_RE.match(cid):
            issues.append(_issue("sfx_bad_id", f"{label}: id は英数字・-・_ だけ（<audio> の id になる）: {cid!r}"))
            continue
        items, errs = _expand(cid, c)
        if errs:
            issues += errs
            continue
        for it in items:
            iid = it["id"]
            if iid in seen:
                issues.append(_issue("sfx_duplicate_id", f"{label}: id {iid} が重なっている"))
                continue
            seen.add(iid)
            sound, at, vol = it["sound"], it["at"], it["volume"]
            if not isinstance(sound, str) or sound not in raw_sounds:
                issues.append(_issue("sfx_unknown_sound", f"{iid}: sounds に {sound!r} が無い"))
                continue
            if sound not in sounds:      # 定義の誤りは上で指摘済み
                continue
            if isinstance(at, bool) or not isinstance(at, (int, float)) or at < 0:
                issues.append(_issue("sfx_bad_at", f"{iid}: at は 0 以上の秒: {at!r}"))
                continue
            if isinstance(vol, bool) or not isinstance(vol, (int, float)) or not (0 <= vol <= MAX_VOLUME):
                issues.append(_issue("sfx_bad_volume", f"{iid}: volume は 0〜{MAX_VOLUME}: {vol!r}"))
                continue
            p = sounds[sound]
            start = float(at) - p["length"] if sfx_synth.anchor(p["kind"]) == "end" else float(at)
            if start < -1e-9:
                issues.append(_issue("sfx_before_start", f"{iid}: 打点 {at:g} 秒に頂点を合わせると頭が {start:g} 秒になる"
                                                          f"（{sound} の length {p['length']:g} を短くするか at を遅らせる）"))
                continue
            cues.append({"id": iid, "sound": sound, "kind": p["kind"], "at": float(at), "start": max(0.0, start),
                         "length": p["length"], "volume": float(vol), "file": sound_file(sound)})
    return cues, sounds, issues


# ── WAV ──

def generate(d: Path, sounds: dict[str, dict], only: set[str] | None = None) -> list[str]:
    """assets/sfx/{名前}.wav を作る。中身が同じなら書かない。返り値: 書いたファイルの相対パス。"""
    made: list[str] = []
    for name, p in sounds.items():
        if only is not None and name not in only:
            continue
        data = sfx_synth.make_wav(p["kind"], **{k: v for k, v in p.items() if k != "kind"})
        out = d / sound_file(name)
        if not out.is_file() or out.read_bytes() != data:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            made.append(sound_file(name))
    return made


# ── <audio> ──

def _num(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def markup(cues: list[dict]) -> str:
    """<audio> の並び（目印は含まない）。重なる音は別の段（data-track-index）へ。"""
    lanes: list[float] = []
    lines: list[str] = []
    for c in sorted(cues, key=lambda c: (c["start"], c["id"])):
        end = c["start"] + c["length"]
        for i, e in enumerate(lanes):
            # 端が接するだけでも別の段へ（lint の重なり判定は浮動小数の誤差を許さない）
            if e + 0.001 <= c["start"]:
                lanes[i] = end
                lane = i
                break
        else:
            lanes.append(end)
            lane = len(lanes) - 1
        lines.append(f'<audio id="{c["id"]}" src="{c["file"]}" data-start="{_num(c["start"])}" '
                     f'data-duration="{_num(c["length"])}" data-track-index="{LANE_BASE + lane}" '
                     f'data-volume="{_num(c["volume"])}"></audio>')
    return "\n".join(lines)


def _block(cues: list[dict], nl: str) -> str:
    body = markup(cues)
    if not body:
        return ""
    return nl.join([_INDENT + BLOCK_BEGIN, *(_INDENT + ln for ln in body.split("\n")), _INDENT + BLOCK_END]) + nl


def sync_tags(d: Path, cues: list[dict]) -> bool:
    """index.html の効果音のブロックを motion.json の通りにする。変えたら True。"""
    p = d / "index.html"
    if not p.is_file():
        return False
    text = p.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    block = _block(cues, nl)
    m = _BLOCK_RE.search(text)
    if m:
        new = text[:m.start()] + block + text[m.end():]
    elif block:
        end = hf_checks.root_open_end(text)
        if end is None:
            return False
        new = text[:end] + nl + block.rstrip("\r\n") + text[end:]
    else:
        return False
    if new == text:
        return False
    p.write_bytes(new.encode("utf-8"))
    return True


def prepare(d: Path, motion: dict | None) -> list[str]:
    """WAV を作り、<audio> を書く。作った・変えたファイルの相対パスを返す（変えた index.html は "index.html"）。"""
    if not has_sfx(motion):
        # 以前の効果音のブロックが残っていれば外す（sfx を消した時）
        return ["index.html"] if sync_tags(d, []) else []
    cues, sounds, _ = resolve(motion)
    made = generate(d, sounds)
    if sync_tags(d, cues):
        made.append("index.html")
    return made


# ── 検査 ──

def _audio_tags(html: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in _AUDIO_RE.finditer(html):
        attrs = {k.lower(): v1 or v2 for k, v1, v2 in _ATTR_RE.findall(m.group(0))}
        attrs["crossorigin"] = bool(re.search(r"\bcrossorigin\b", m.group(0), re.I))
        if attrs.get("id"):
            out[attrs["id"]] = attrs
    return out


def check(d: Path, motion: dict | None, duration: float | None = None) -> list[dict]:
    """効果音の検査。誤り（error）は描かない（M6）。"""
    if not has_sfx(motion):
        return []
    cues, sounds, issues = resolve(motion)
    for c in cues:
        end = c["start"] + c["length"]
        if duration is not None and end > duration + 1e-6:
            issues.append(_issue("sfx_past_end", f"{c['id']}: 音の終わり（{end:.2f} 秒）がコンポジションの長さ"
                                                  f"（{duration:g} 秒）を越える。越えた分は切れる", level="warning"))
    if (motion or {}).get("profile") == "insert" and cues:
        issues.append(_issue("insert_has_sfx", "インサートは既定で無音（行の声に重なる＝§7）。効果音を使う時は声との釣り合いを聴いて決める",
                             level="warning"))
    index = d / "index.html"
    if index.is_file():
        tags = _audio_tags(index.read_text(encoding="utf-8", errors="replace"))
        for c in cues:
            t = tags.get(c["id"])
            if t is None:
                issues.append(_issue("sfx_tag_missing", f"{c['id']}: index.html に <audio> が無い（準備で書く）", file="index.html"))
                continue
            try:
                start_ok = abs(float(t.get("data-start", "nan")) - c["start"]) <= 1.0 / hf_checks.FPS
                vol_ok = abs(float(t.get("data-volume", "1")) - c["volume"]) <= 0.005
            except ValueError:
                start_ok = vol_ok = False
            if t.get("src") != c["file"] or not start_ok or not vol_ok or t.get("crossorigin"):
                issues.append(_issue("sfx_tag_mismatch", f"{c['id']}: <audio> が motion.json と合わない（src・data-start・data-volume）",
                                     file="index.html"))
    return issues
