"""台本の行を「カット」へまとめる（穴9・`Docs/AROLL_UNIFIED_FLOW_PLAN.md` §9・§11-2）。

## なぜ要るか

`line` が TTS単位・画像単位・字幕単位・OTIOクリップ単位を**全部兼ねている**ため、
TTSの精度のために長い台詞を分割すると画像も自動的に割れ、同一話者の細切れカットが並ぶ。
打ち手は「行の割り方の調整」ではなく**画像の単位を行から切り離すこと**。

## カットの定義（§11-2・実データで決めた）

1. 話者交代・セクション交代は**必ず**境界
2. 1行だけのラン → ``solo``
3. 2行以上のラン → **最終行は単独で ``kime``**（決め台詞）
4. 最終行より前（前振り）は合計 ``max_sec`` 以下まで結合して ``setup``
5. 1行が ``max_sec`` を超えても割らない（1行の途中で絵は変えられない）

⚠️ **尺だけで結合してはいけない。** 実測（`20260905_001` ep01・64行）では、
尺だけで束ねると**結合対象7組のうち5組が決め台詞**だった
（「今も、見つかっていない。」「「重力」と「UAP」です。」「なぜ、よりによって今なんだ？」）。
短い前振り＋短い決めの組ほど上限に収まるので、狙い撃ちで潰れる。

⚠️ **行数を基準にしない。** 行数は TTS の都合で決まった数なので、それを画像側の基準にすると
「TTSが細かく割った話者ほどカットが増える」という症状が設定値として固定される。

## 尺の出どころ

``tts.json`` の実尺を使い、無ければ文字数から推定する（境界判断には推定で十分）。
"""
from __future__ import annotations

# 1カットに束ねる前振りの上限（秒）。ユーザーの体感「12秒くらい」＋実測
# （分割前の話数は1枚あたり中央9.9秒で、それが許容されていた）から決めた。
DEFAULT_MAX_SEC = 12.0

# TTS前に尺を見積もるための係数。実測（64行・419秒・日本語ナレーション）から
# 1文字あたり約0.18秒。⚠️ これは**境界を決めるためだけ**の粗い値で、
# タイムラインの尺には絶対に使わない（そちらは tts.json の実測が正本）。
SEC_PER_CHAR = 0.18
MIN_LINE_SEC = 1.0


def estimate_sec(text: str) -> float:
    """文字数から尺を見積もる（TTS前の暫定値）。"""
    return max(MIN_LINE_SEC, len((text or "").strip()) * SEC_PER_CHAR)


def durations_from_tts(tts: dict | None) -> dict[str, float]:
    """``tts.json`` の timeline から {line_id: 尺+間} を作る。

    間（``pause_after_sec``）を足すのは、**画面に絵が出ている時間**が知りたいから。
    """
    out: dict[str, float] = {}
    for e in (tts or {}).get("timeline", []):
        lid = e.get("line_id")
        if not lid:
            continue
        try:
            out[lid] = float(e["end_sec"]) - float(e["start_sec"]) + float(e.get("pause_after_sec") or 0)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _runs(panels: list[dict]) -> list[list[dict]]:
    """話者・セクションが変わるところで切った「ラン」に分ける。"""
    runs: list[list[dict]] = []
    cur: list[dict] = []
    for p in panels:
        if cur and (p.get("speaker_id") != cur[-1].get("speaker_id")
                    or p.get("section") != cur[-1].get("section")):
            runs.append(cur)
            cur = []
        cur.append(p)
    if cur:
        runs.append(cur)
    return runs


def _pack(lines: list[dict], dur: dict[str, float], max_sec: float) -> list[list[dict]]:
    """前振りを max_sec 以下で貪欲にまとめる。1行が上限を超えても割らない。"""
    out: list[list[dict]] = []
    cur: list[dict] = []
    acc = 0.0
    for p in lines:
        d = dur.get(p.get("line_id"), 0.0)
        if cur and acc + d > max_sec:
            out.append(cur)
            cur, acc = [], 0.0
        cur.append(p)
        acc += d
    if cur:
        out.append(cur)
    return out


def plan_cuts(panels: list[dict], durations: dict[str, float] | None = None,
              overrides: dict | None = None, max_sec: float = DEFAULT_MAX_SEC) -> list[dict]:
    """パネル列をカットへまとめる。**純粋関数**（何も保存しない・課金しない）。

    panels: `aroll.json` の ``panels``（台本順）。``orphan`` は呼び出し側で除くこと。
    durations: {line_id: 秒}。省略/欠落した行は ``text`` から推定する。
    overrides: ユーザーの手直し。``{line_id: {"boundary": "start"|"join", "role": "kime"|...}}``
      - ``boundary="start"`` … その行から新しいカットを始める（分ける）
      - ``boundary="join"``  … 前のカットにつなげる（境界を消す）
      ⚠️ 話者が違う行の ``join`` は**無視する**（別人の絵を1カットに混ぜないため）。

    返り値: ``[{"cut_id", "line_ids", "role", "duration_sec", "speaker_id", "section"}, ...]``
      role は ``solo``（1行のラン）/ ``setup``（前振り）/ ``kime``（ランの最終行）。
    """
    durations = durations or {}
    overrides = overrides or {}
    dur = {}
    for p in panels:
        lid = p.get("line_id")
        dur[lid] = durations.get(lid) or estimate_sec(p.get("text"))

    groups: list[list[dict]] = []
    for run in _runs(panels):
        if len(run) == 1:
            groups.append([("solo", run)])
            continue
        # ★ ランの最終行は単独の「決め」。前振りだけを尺でまとめる（§11-2）
        head, tail = run[:-1], run[-1]
        packed: list[tuple[str, list[dict]]] = [("setup", c) for c in _pack(head, dur, max_sec)]
        packed.append(("kime", [tail]))
        groups.append(packed)

    cuts: list[dict] = []
    for run_cuts in groups:
        for role, lines in run_cuts:
            cuts.append({"role": role, "lines": list(lines)})

    cuts = _apply_overrides(cuts, overrides)

    out = []
    for i, c in enumerate(cuts, 1):
        lines = c["lines"]
        first = lines[0]
        out.append({
            "cut_id": "cut_%03d" % i,
            "line_ids": [l.get("line_id") for l in lines],
            "role": c["role"],
            "duration_sec": round(sum(dur.get(l.get("line_id"), 0.0) for l in lines), 1),
            "speaker_id": first.get("speaker_id", ""),
            "section": first.get("section", ""),
        })
    return out


def _apply_overrides(cuts: list[dict], overrides: dict) -> list[dict]:
    """ユーザーの手直しを当てる。**再計算しても消えないのが要件**（§11-2）。

    行の並びへ一度ばらしてから組み直す＝自動の結果と手直しが同じ土俵に乗る。
    """
    if not overrides:
        return cuts
    flat: list[tuple[dict, str, bool]] = []
    for c in cuts:
        for i, l in enumerate(c["lines"]):
            flat.append((l, c["role"], i == 0))  # (行, 役, 自動境界か)

    rebuilt: list[dict] = []
    for line, role, auto_start in flat:
        ov = overrides.get(line.get("line_id")) or {}
        b = ov.get("boundary")
        start = auto_start
        if b == "start":
            start = True
        elif b == "join" and rebuilt:
            # ⚠️ 話者が違う行は繋げない（別人の絵が1カットに混ざる）
            prev = rebuilt[-1]["lines"][-1]
            start = prev.get("speaker_id") != line.get("speaker_id")
        if start or not rebuilt:
            rebuilt.append({"role": ov.get("role") or role, "lines": [line]})
        else:
            rebuilt[-1]["lines"].append(line)
            if ov.get("role"):
                rebuilt[-1]["role"] = ov["role"]
    return rebuilt


def cut_of_line(cuts: list[dict]) -> dict[str, dict]:
    """{line_id: そのカット} の逆引き。選定・背景割当がカット単位で動くのに使う。"""
    return {lid: c for c in cuts for lid in c["line_ids"]}
