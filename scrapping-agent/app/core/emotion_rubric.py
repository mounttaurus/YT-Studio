"""感情ラベルのルーブリック（判定基準・隣接・3群）の唯一の本籍。

`Docs/STOCK_LABEL_ACCURACY_PLAN.md` 付録A（2026-10-03 ユーザー確認済み＝`v1`）をコードにしたもの。
人（在庫の確認）・私（一次ラベル付け）・視覚モデル（登録時の観察）が**同じ物差し**で判定するためにある。

- 語彙は `panel_presets` の emotion と同じ 11 語（語彙は変えない。テストで一致を固定）。
- 判定は**顔の見た目の手がかり**で行う。手・体のポーズは pose の担当（感情の手がかりにしない）。
- 2層の辞書（§14・v2）: 細かいタグ（絵の描写）→ 系統（上の 11 語）。1枚の絵は主タグ＋副タグ（最大2）。
  副タグの系統は**主タグと同じ系統か、その隣接だけ・陽↔陰をまたがない**（不変条件 I3）。
  検査は `validate_tags` 1箇所（唯一の書き手 `panel_library_manager.set_emotion_tags` がここを呼ぶ）。
  `validate_allowed` は系統どうしの同じ検査（11 語の段階の名残・テストで固定）。

上書き: `shared/imagegen/emotion_rubric.json` があれば `emotions`・`groups` をそれで置き換える
（無ければコードの既定。自動では書き出さない＝既定を勝手に固定しない）。版を変えたら `version` も変えること
（`emotion_check.rubric` に刻まれ、版違いの観察は再観察の対象になる）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
RUBRIC_FILE = SHARED_DIR / "imagegen" / "emotion_rubric.json"

MAX_SECONDARY = 2

DEFAULT_RUBRIC: dict = {
    "version": "v1.1",  # v1.1: 5-3 でユーザーが付けた副タグの組を隣接に足した（§13 5-3）
    # 陽↔陰をまたぐ取り違えだけが「悲しい行に笑顔」級の実害（memory: emotion-label-accuracy-measured）
    "groups": {
        "positive": ["happy", "excited", "shy"],
        "negative": ["sad", "angry", "troubled"],
        "middle": ["neutral", "serious", "thoughtful", "question", "surprised"],
    },
    "emotions": {
        "neutral": {
            "cues": "力みが無い。眉は水平・口は閉じる/ゆるい・視線は正面か自然",
            "confused_with": "serious＝力み（顎・眉間）がある",
            "adjacent": ["serious", "thoughtful", "question", "happy", "excited"],
        },
        "serious": {
            "cues": "口を引き結ぶ・眉は水平〜わずかに下・視線が据わる。眉間の深い皺や歯は無い",
            "confused_with": "angry＝眉が鋭く下がる・歯/怒鳴り",
            "adjacent": ["neutral", "thoughtful", "angry", "question", "troubled"],
        },
        "thoughtful": {
            "cues": "視線が逸れる（下・横・上）・内向き・口は閉じる/わずかに開く",
            "confused_with": "serious＝視線がこちらに据わる",
            "adjacent": ["serious", "neutral", "troubled", "question", "sad"],
        },
        "question": {
            "cues": "眉が上がる/左右非対称・目が見開き気味でこちらを見る・首の傾き・口が軽く開く（「え？」）",
            "confused_with": "しかめ面＝serious/angry・顎に手だけ＝thoughtful",
            "adjacent": ["thoughtful", "surprised", "troubled", "serious", "angry", "neutral"],
        },
        "troubled": {
            "cues": "眉の内側が上がって寄る・目が不安げ・口が波/小さく開く・冷や汗",
            "confused_with": "sad＝涙・口角が下がり悲しみが主",
            "adjacent": ["sad", "question", "thoughtful"],
        },
        "sad": {
            "cues": "伏し目・眉の内側が上がる・口角が下がる・涙",
            "confused_with": "troubled＝不安・焦りが主",
            "adjacent": ["troubled", "thoughtful", "surprised"],
        },
        "angry": {
            "cues": "眉が鋭く下がる・睨む・歯を食いしばる/怒鳴る",
            "confused_with": "serious＝眉間が穏やか",
            "adjacent": ["serious", "question"],
        },
        "surprised": {
            "cues": "目を大きく見開く・眉が高く上がる・口が丸く開く",
            "confused_with": "question＝驚きより困惑・探る",
            "adjacent": ["question", "excited", "sad"],
        },
        "excited": {
            "cues": "目がきらきら・大きな笑顔/開口・勢いがある",
            "confused_with": "happy＝穏やかな笑み",
            "adjacent": ["happy", "surprised", "neutral"],
        },
        "happy": {
            "cues": "笑顔・目が和らぐ",
            "confused_with": "excited＝勢い",
            "adjacent": ["excited", "shy", "neutral"],
        },
        "shy": {
            "cues": "頬の赤み・視線を逸らす・小さな笑み",
            "confused_with": "happy＝照れが無い",
            "adjacent": ["happy"],
        },
    },
}


# --------------------------------------------------------------------- 2層の辞書（v2・§14）
#
# 細かいタグ＝絵の描写（5-3 でユーザーが使った言葉）／系統＝上の 11 語。
# 各系統の語そのものも細かいタグとして持つ（既存のラベル・行の感情はそのまま有効）。
# `face_hidden`（後ろ姿など顔が見えない）は系統なし＝自動選択に出ない（§14 D3）。
# 振り分けは 2026-10-03 ユーザー承認（smiling→neutral・dissatisfied→angry・defiant→excited・confused→troubled）。
TAGS_VERSION = "v2"
# 系統の日本語名（panel_presets の label_ja と同じ。ここは辞書の表示用）
FAMILY_LABEL_JA = {
    "neutral": "通常", "serious": "真剣", "thoughtful": "物思い", "question": "疑問", "troubled": "困惑",
    "sad": "悲しい", "angry": "怒り", "surprised": "驚き", "excited": "興奮", "happy": "嬉しい", "shy": "照れ",
}
FACE_HIDDEN = "face_hidden"
EXTRA_TAGS: dict[str, dict] = {
    "calm": {"family": "neutral", "label_ja": "落ち着き", "cues": "穏やかで静か。力みが無く、表情の動きが小さい"},
    "smiling": {"family": "neutral", "label_ja": "微笑み", "cues": "口元が穏やかに笑む。嬉しさの勢いは無い（happy ではない）"},
    "acceptance": {"family": "neutral", "label_ja": "納得", "cues": "うなずく・腑に落ちた顔。軽い笑みを含むことがある"},
    "certainty": {"family": "serious", "label_ja": "確信", "cues": "迷いの無い目・口を結ぶ。言い切る顔"},
    "curious": {"family": "question", "label_ja": "興味", "cues": "目が開き、身を乗り出す・探る"},
    "suspicion": {"family": "question", "label_ja": "疑い", "cues": "目を細める・半目・片眉・横目で探る"},
    "confused": {"family": "troubled", "label_ja": "混乱", "cues": "眉が上がって寄る・口が開く・頭に手。どうしていいか分からない"},
    "denying": {"family": "troubled", "label_ja": "否定（いやいや）", "cues": "手を振る/両手を上げる・苦笑い・冷や汗"},
    "despair": {"family": "sad", "label_ja": "絶望", "cues": "血の気が引く・目を見開いて固まる・暗い色調"},
    "dissatisfied": {"family": "angry", "label_ja": "不満（むくれ）", "cues": "口をとがらせる・むくれる・横目。怒りより弱い"},
    "hate": {"family": "angry", "label_ja": "嫌悪", "cues": "顔をしかめ、歯をむく・嫌がる"},
    "defiant": {"family": "excited", "label_ja": "不敵（挑戦的な笑み）", "cues": "口角を上げた挑むような笑み・腕を広げる"},
    FACE_HIDDEN: {"family": None, "label_ja": "顔が見えない（後ろ姿）", "cues": "顔が写っていない・判定できない"},
}


def tags(rubric: dict | None = None) -> dict[str, dict]:
    """細かいタグの辞書 {tag: {family, label_ja, cues}}。系統の 11 語も同名のタグとして含む。"""
    r = rubric or load_rubric()
    out = {e: {"family": e, "label_ja": r["emotions"][e].get("label_ja") or FAMILY_LABEL_JA.get(e, e),
               "cues": r["emotions"][e].get("cues", "")}
           for e in r["emotions"]}
    out.update(r.get("tags") or EXTRA_TAGS)
    return out


def family_of(tag: str | None, rubric: dict | None = None) -> str | None:
    """細かいタグ → 系統（11 語）。未知のタグ・face_hidden は None。"""
    if not tag:
        return None
    return (tags(rubric).get(tag) or {}).get("family")


def validate_tags(main: str | None, subs: list[str] | None, rubric: dict | None = None) -> list[str]:
    """主タグ・副タグの検査（不変条件 I3 の2層版）。問題があれば理由のリスト。

    - 主タグは辞書にあること。face_hidden は副タグを持たない・副タグにもならない
    - 副タグは最大2・重複なし・主タグ自身を含めない
    - 副タグの系統は「主タグと同じ系統」か「主タグの系統の隣接」で、陽↔陰をまたがない
    """
    r = rubric or load_rubric()
    d = tags(r)
    subs = list(subs or [])
    if main not in d:
        return [f"主タグが辞書にありません: {main!r}"]
    errs: list[str] = []
    if main == FACE_HIDDEN and subs:
        return ["顔が見えない絵に副タグは付けられません"]
    if len(subs) > MAX_SECONDARY:
        errs.append(f"副タグは最大 {MAX_SECONDARY} 個です（{len(subs)} 個）")
    if len(set(subs)) != len(subs):
        errs.append("副タグが重複しています")
    fm = d[main]["family"]
    for s in subs:
        if s == main:
            errs.append(f"副タグに主タグ {s} を含めない")
        elif s not in d or s == FACE_HIDDEN:
            errs.append(f"副タグにできないタグです: {s!r}")
        else:
            fs = d[s]["family"]
            if fs != fm and crosses_polarity(fm, fs, r):
                errs.append(f"{main}（{fm}）と {s}（{fs}）は陽↔陰をまたぐので副タグにできません")
            elif fs != fm and fs not in adjacent(fm, r):
                errs.append(f"{s}（{fs}）は {main}（{fm}）の系統の隣接ではありません")
    return errs


def load_rubric() -> dict:
    """有効なルーブリック。上書きファイルが壊れていれば既定に倒す（判定基準が消えるより安全）。"""
    if RUBRIC_FILE.exists():
        try:
            data = json.loads(RUBRIC_FILE.read_text(encoding="utf-8"))
            if isinstance(data.get("emotions"), dict) and isinstance(data.get("groups"), dict):
                return {"version": str(data.get("version") or "custom"),
                        "groups": data["groups"], "emotions": data["emotions"]}
        except (OSError, json.JSONDecodeError):
            pass
    return DEFAULT_RUBRIC


def vocab(rubric: dict | None = None) -> tuple[str, ...]:
    return tuple((rubric or load_rubric())["emotions"].keys())


def group_of(emotion: str, rubric: dict | None = None) -> str | None:
    for g, members in (rubric or load_rubric())["groups"].items():
        if emotion in members:
            return g
    return None


def adjacent(emotion: str, rubric: dict | None = None) -> frozenset[str]:
    """隣接（対称に扱う＝どちらかの側に書いてあれば隣接）。"""
    r = rubric or load_rubric()
    out = set((r["emotions"].get(emotion) or {}).get("adjacent") or [])
    for other, spec in r["emotions"].items():
        if emotion in (spec.get("adjacent") or []):
            out.add(other)
    out.discard(emotion)
    return frozenset(out)


def crosses_polarity(a: str, b: str, rubric: dict | None = None) -> bool:
    """陽↔陰をまたぐか（実害のある取り違え）。"""
    ga, gb = group_of(a, rubric), group_of(b, rubric)
    return {ga, gb} == {"positive", "negative"}


def validate_allowed(primary: str | None, allowed: list[str] | None,
                     rubric: dict | None = None) -> list[str]:
    """副感情の検査（不変条件 I3）。問題があれば理由のリスト、無ければ空。

    - 主感情が語彙内であること（空・未知の主感情に副感情は付けない）
    - 最大 MAX_SECONDARY 個・重複なし・主感情自身を含めない（主感情は暗黙に許される＝和集合）
    - 各副感情が語彙内・主感情の隣接・陽↔陰をまたがない
    """
    r = rubric or load_rubric()
    errs: list[str] = []
    allowed = list(allowed or [])
    words = vocab(r)
    if primary not in words:
        return [f"主感情が語彙にありません: {primary!r}"]
    if len(allowed) > MAX_SECONDARY:
        errs.append(f"副感情は最大 {MAX_SECONDARY} 個です（{len(allowed)} 個）")
    if len(set(allowed)) != len(allowed):
        errs.append("副感情が重複しています")
    adj = adjacent(primary, r)
    for a in allowed:
        if a == primary:
            errs.append(f"副感情に主感情 {a} を含めない（主感情は常に許される）")
        elif a not in words:
            errs.append(f"語彙に無い感情です: {a!r}")
        elif crosses_polarity(primary, a, r):
            errs.append(f"{primary} と {a} は陽↔陰をまたぐので副感情にできません")
        elif a not in adj:
            errs.append(f"{a} は {primary} の隣接ではありません（隣接: {', '.join(sorted(adj))}）")
    return errs


def as_prompt_table(rubric: dict | None = None) -> str:
    """人・視覚モデル向けの表（Markdown）。プロンプトとシートの凡例で同じ文面を使う。"""
    r = rubric or load_rubric()
    rows = ["| emotion | visual cues | often confused with | allowed secondary |", "|---|---|---|---|"]
    for e, spec in r["emotions"].items():
        rows.append("| %s | %s | %s | %s |" % (e, spec.get("cues", ""), spec.get("confused_with", ""),
                                              ", ".join(sorted(adjacent(e, r)))))
    return "\n".join(rows)
