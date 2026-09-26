"""台本の行を「カット」へまとめる（穴9・`Docs/SUBLINE_PLAN.md` §6。旧規則は
`Docs/AROLL_UNIFIED_FLOW_PLAN.md` §11-2＝2026-09-26に本ファイル§6-3へ置き換え）。

## なぜ要るか

`line` が TTS単位・画像単位・字幕単位・OTIOクリップ単位を**全部兼ねている**ため、
TTSの精度のために長い台詞を分割すると画像も自動的に割れ、同一話者の細切れカットが並ぶ。
打ち手は「行の割り方の調整」ではなく**画像の単位を行から切り離すこと**。

## カットの定義（`Docs/SUBLINE_PLAN.md` §6-3・2026-09-26改訂）

1. 話者交代・セクション交代は**必ず**境界
2. **1行（サブ行を含む）に1枚が基本** → 既定では ``solo``
3. 同じ話者・同じセクションが続く中で、**短すぎる行**（推定 ``SHORT_LINE_SEC`` 未満。
   実尺があれば実尺）は**直前の行**（のカット）へ束ねる → ``bundled``。ランの先頭行は
   束ねる相手がいないので、短くても単独になる
4. 手直し（``cut_overrides`` の ``boundary``）はそのまま。別の話者との join は無視する
5. **廃止**: 決め台詞（最終行を常に単独にする規則）、前振りを ``max_sec`` まで束ねる規則
   （サブ行と衝突する＝最後のサブ行が短くても束ねられない／絵を変えたくて分けた行を
   1枚に戻してしまう。§6-2）

⚠️ **行数を基準にしない。** 行数は TTS の都合で決まった数なので、それを画像側の基準にすると
「TTSが細かく割った話者ほどカットが増える」という症状が設定値として固定される。

## 尺の出どころ

``tts.json`` の実尺を使い、無ければ文字数から推定する（境界判断には推定で十分）。
"""
from __future__ import annotations

# 短すぎる行（直前の行と束ねる閾値・秒）。§0-2の実測（秒≈0.8+0.2×字）で
# 10字前後に当たる値（`Docs/SUBLINE_PLAN.md` §2-7・§6-3）。
SHORT_LINE_SEC = 3.0

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


def _bundle_short_lines(run: list[dict], dur: dict[str, float],
                        short_line_sec: float) -> list[list[dict]]:
    """ラン内で、短すぎる行を直前の行（のカット）へ束ねる（§6-3 規則2・3）。

    ランの先頭行は束ねる相手がいないので、短くても単独になる。
    """
    out: list[list[dict]] = []
    for p in run:
        d = dur.get(p.get("line_id"), 0.0)
        if out and d < short_line_sec:
            out[-1].append(p)
        else:
            out.append([p])
    return out


def plan_cuts(panels: list[dict], durations: dict[str, float] | None = None,
              overrides: dict | None = None, short_line_sec: float = SHORT_LINE_SEC) -> list[dict]:
    """パネル列をカットへまとめる。**純粋関数**（何も保存しない・課金しない）。

    panels: `aroll.json` の ``panels``（台本順）。``orphan`` は呼び出し側で除くこと。
    durations: {line_id: 秒}。省略/欠落した行は ``text`` から推定する。
    overrides: ユーザーの手直し。``{line_id: {"boundary": "start"|"join"}}``
      - ``boundary="start"`` … その行から新しいカットを始める（分ける）
      - ``boundary="join"``  … 前のカットにつなげる（境界を消す）
      ⚠️ 話者が違う行の ``join`` は**無視する**（別人の絵を1カットに混ぜないため）。

    返り値: ``[{"cut_id", "line_ids", "role", "duration_sec", "speaker_id", "section"}, ...]``
      role は ``solo``（1行）/ ``bundled``（短すぎる行を束ねた2行以上）。
    """
    durations = durations or {}
    overrides = overrides or {}
    dur = {}
    for p in panels:
        lid = p.get("line_id")
        dur[lid] = durations.get(lid) or estimate_sec(p.get("text"))

    cuts: list[list[dict]] = []
    for run in _runs(panels):
        cuts.extend(_bundle_short_lines(run, dur, short_line_sec))

    cuts = _apply_overrides(cuts, overrides)

    out = []
    for i, lines in enumerate(cuts, 1):
        first = lines[0]
        out.append({
            "cut_id": "cut_%03d" % i,
            "line_ids": [l.get("line_id") for l in lines],
            "role": "solo" if len(lines) == 1 else "bundled",
            "duration_sec": round(sum(dur.get(l.get("line_id"), 0.0) for l in lines), 1),
            "speaker_id": first.get("speaker_id", ""),
            "section": first.get("section", ""),
        })
    return out


def _apply_overrides(cuts: list[list[dict]], overrides: dict) -> list[list[dict]]:
    """ユーザーの手直しを当てる。**再計算しても消えないのが要件**（§11-2）。

    行の並びへ一度ばらしてから組み直す＝自動の結果と手直しが同じ土俵に乗る。
    """
    if not overrides:
        return cuts
    flat: list[tuple[dict, bool]] = []
    for c in cuts:
        for i, l in enumerate(c):
            flat.append((l, i == 0))  # (行, 自動境界か)

    rebuilt: list[list[dict]] = []
    for line, auto_start in flat:
        ov = overrides.get(line.get("line_id")) or {}
        b = ov.get("boundary")
        start = auto_start
        if b == "start":
            start = True
        elif b == "join" and rebuilt:
            # ⚠️ 話者が違う行は繋げない（別人の絵が1カットに混ざる）
            prev = rebuilt[-1][-1]
            start = prev.get("speaker_id") != line.get("speaker_id")
        if start or not rebuilt:
            rebuilt.append([line])
        else:
            rebuilt[-1].append(line)
    return rebuilt


def cut_of_line(cuts: list[dict]) -> dict[str, dict]:
    """{line_id: そのカット} の逆引き。選定・背景割当がカット単位で動くのに使う。"""
    return {lid: c for c in cuts for lid in c["line_ids"]}
