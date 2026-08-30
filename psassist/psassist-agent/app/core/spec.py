"""パネル組版の実測仕様（正本）.

すべての数値は**既存の手作業PSD 129枚から実測**したもの（中央値）。推測値は含めない。

⚠️ **ここを憶測で書き換えないこと。** 仕様を変える時は calibration を取り直す
（`scripts/harvest.py` → `scripts/analyze.py`）。数値は較正したチャンネルの作風に
紐づくので、別のチャンネルで使うなら取り直す。

話者ごとの既定はチャンネル固有なのでコードに持たず、
`assets/speaker_defaults.json`（任意・無くても動く）に置く。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# spec.py → core → app → psassist-agent → psassist
ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))),
    "assets",
)

# ---------------------------------------------------------------- キャンバス
# 129/129 が 1376x768（NanoBanana の素の出力）。1920x1080 化は最終段の
# バッチ（キャンバス拡張＋背景差し替え）で行うため、ここでは素のまま扱う。
CANVAS_W = 1376
CANVAS_H = 768


# ---------------------------------------------------------------- バブル
@dataclass(frozen=True)
class BubbleShape:
    """bubbles.psd 内のシェイプレイヤー1つ。"""

    key: str  # 論理名（プランJSONで使う）
    layer: str  # bubbles.psd 上のレイヤー名（複製元。末尾の " 1" まで含む）
    kind: str  # rect / round / cloud / spike
    direction: str  # horizontal / vertical
    uses: int  # 129枚での実使用回数（既定値選びの根拠）
    size: tuple[int, int]  # bubbles.psd 上の素の寸法
    tail_dx: float  # 正位置での尻尾の左右オフセット（本体中心比・実測）


# bubbles.psd（1920x1080・9レイヤー）から実測。尻尾は9枚とも下向きで、
# 既存129枚の「下向き97%」と一致した。
#
# ★重要な構造: 各系統に「中央尻尾＝無指向」と「右寄り尻尾＝指向あり」の
#   2種がある。既定に選んだ Talk 10 / Talk 1 はどちらも中央寄りなので、
#   既定運用では左右反転がほぼ効かない＝反転を意識しなくてよい。
#   左右を明示したい時だけ指向ありの変化形を選び、必要なら反転する。
BUBBLES: tuple[BubbleShape, ...] = (
    # 四角＝説明役に多い（較正時は説明役の 67/67 が四角）
    BubbleShape("rect_a", "Talk 10 1", "rect", "horizontal", 34, (548, 471), -0.143),
    BubbleShape("rect_b", "Talk 9 1", "rect", "horizontal", 28, (594, 435), +0.178),
    BubbleShape("rect_v", "Talk 11 1", "rect", "vertical", 5, (390, 658), +0.123),
    # 丸＝相槌・納得
    BubbleShape("round_a", "Talk 1 1", "round", "horizontal", 29, (602, 426), -0.167),
    BubbleShape("round_v", "Talk 6 1", "round", "vertical", 2, (412, 623), -0.206),
    # 雲＝強い疑問
    BubbleShape("cloud_a", "Talk 3 1", "cloud", "horizontal", 16, (648, 468), +0.067),
    BubbleShape("cloud_b", "Talk 4 1", "cloud", "horizontal", 4, (616, 438), +0.151),
    # スパイク＝激しい反応
    BubbleShape("spike_a", "Talk 8 1", "spike", "horizontal", 9, (565, 459), +0.021),
    BubbleShape("spike_b", "Talk 7 1", "spike", "horizontal", 0, (645, 404), +0.074),
)

# ⚠️ tail_dx は**尻尾の先端**で測ること（2026-08-23 修正）。
#    当初は尻尾領域全体の重心で測っており、本体に埋もれて Talk 1=-0.006 /
#    Talk 10=-0.041＝「中央」と誤判定していた。その結果 flip をかけず、
#    尻尾がキャラと反対を向いた出力になった（ユーザー指摘で発覚）。
#    先端で測り直すと Talk 1=-0.167 / Talk 10=-0.143 と明確に左向きだった。
#    実画像で検証済み: 私の出力 -0.165 → ユーザー修正 +0.165（左右反転のみ）。
TAIL_NEUTRAL = 0.03

BUBBLE_BY_KEY = {b.key: b for b in BUBBLES}
BUBBLE_BY_LAYER = {b.layer: b for b in BUBBLES}

# 縦書きは不採用（2026-08-22 決定）。台本の 46/196 行（23%）に英数字が
# 含まれ、USA / SNS / DNA / .30-06 のような3文字以上のトークンは縦中横に
# 収まらないため。雲・スパイクの縦書きシェイプ自体も存在しない。
# 将来 direction="vertical" を使う場合はシェイプ2種の追加が先に必要。
DEFAULT_DIRECTION = "horizontal"


# ---------------------------------------------------------------- 話者既定
@dataclass(frozen=True)
class SpeakerDefault:
    bubble_key: str
    side: str  # left / right
    note: str


FALLBACK_DEFAULT = SpeakerDefault("rect_a", "left", "話者不明時")


def load_speaker_defaults(path: str | None = None) -> dict[str, SpeakerDefault]:
    """話者ごとの既定バブル（`assets/speaker_defaults.json`）。無ければ空。

    ⚠️ **中身はチャンネル固有**（話者名も、最頻値もそのチャンネルの実測）。
    だから `spec.py` に直書きしない ── `assets/background_overrides.json` と同じ扱いで、
    コードは配布し、データはユーザーが持つ（`assets/README.md`）。

    **空でも壊れない。** 未知の話者は `FALLBACK_DEFAULT` に落ち、`UNKNOWN_SPEAKER`
    警告が出るだけ。しかも左右は `side_from_mask`（実測82%）が優先されるので、
    このテーブルが効くのはマスクが無い行の左右と、バブル形状の初期値だけ。

    形状は**そもそも当てられない**ことが実証済み（129枚: emotion を使っても約60%で、
    最頻値決め打ち47%と大差ない）。当てにいかず、既定を置いて人が直す方針。
    """
    if path is None:
        path = os.environ.get("PSA_SPEAKER_DEFAULTS") or os.path.join(
            ASSETS_DIR, "speaker_defaults.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh).get("speakers", {})
    out: dict[str, SpeakerDefault] = {}
    for name, v in raw.items():
        key = v.get("bubble_key") or FALLBACK_DEFAULT.bubble_key
        if key not in BUBBLE_BY_KEY:
            continue  # 知らないシェイプ名は黙って捨てる（既定へ落ちる）
        out[name] = SpeakerDefault(key, v.get("side") or FALLBACK_DEFAULT.side,
                                   v.get("note", ""))
    return out


SPEAKER_DEFAULTS: dict[str, SpeakerDefault] = {}  # load_speaker_defaults() で読み込む


# --- バブルを隠さないためのキャラ移動 -------------------------------------
# 「吹き出しは必ず読めなければならない」というのが最優先の制約。196枚を実測すると
# 顔と反対側ルールでも 73枚(37%) がバブル本体に重なっていた。
# AI も画像理解も要らない: Select Subject 済みの透過マスクがあるので、
# 「バブル矩形 ∩ キャラのアルファ」は厳密な画素演算で出る。
OVERLAP_TOL = 0.02  # これ以下の重なりは無視する
# 移動でキャラがこれ以上見切れるなら、移動では解決しない → 縮小へ切り替える
MAX_CROP_RATIO = 0.15
# ★キャラの縮小は不採用（2026-08-23 決定）。
#   理由: 画角より大きく生成されたキャラを縮小すると、画面外で切れていた断面が
#   画面内へ露出する（実例 line_097: 頭頂部が平らに欠ける）。辺チェックで危険な
#   ものを弾いた後の2枚も、実機で見ると「やはり不自然」だった。
#   キャラの大きさが他パネルと揃わないこと自体が違和感の源なので、手段ごと落とす。
#   重なりは ①移動 → ②バブル縮小 の2段階で処理し、残りはユーザーへ回す。
ENABLE_CHARACTER_SCALE = False
MIN_SCALE = 0.62  # ENABLE_CHARACTER_SCALE を戻す時のための下限（現在未使用）


def character_offset(mask: dict | None, rect: list[int], side: str) -> tuple[int, dict]:
    """バブルからキャラを追い出すのに必要な水平移動量(px)と診断情報。

    実測（196枚）:
        重なりなし              123枚 (63%)
        移動だけで解消（見切れ<15%） 51枚 → 合計 174枚 (89%) が自動で解決
        縮小が要る                22枚 → ユーザーへ回す
    """
    info: dict = {"overlap": 0.0, "resolved": True}
    grid = (mask or {}).get("grid")
    if not grid:
        return 0, info

    w, h = mask["canvas"]
    rows, cols = len(grid), len(grid[0])
    px_x, px_y = w / cols, h / rows
    # ★バブルが占める y 帯だけを見る。全身で見ると肩幅・胴体を拾って
    #   「重なっている」と過剰判定する（バブルは上から1/3にしかない）。
    r0 = max(0, int(rect[1] / px_y))
    r1 = min(rows, int(rect[3] / px_y) + 1)
    c0 = max(0, int(rect[0] / px_x))
    c1 = min(cols, int(rect[2] / px_x) + 1)
    if r1 <= r0 or c1 <= c0:
        return 0, info

    band = [max(grid[r][c] for r in range(r0, r1)) for c in range(cols)]
    info["overlap"] = round(sum(band[c0:c1]) / (c1 - c0), 3)
    if info["overlap"] <= OVERLAP_TOL:
        return 0, info

    occupied = [i for i, v in enumerate(band) if v > 0.02]
    if not occupied:
        return 0, info
    lo, hi = occupied[0] * px_x, (occupied[-1] + 1) * px_x
    shift = int(rect[2] - lo) if side == "left" else -int(hi - rect[0])

    char_w = hi - lo
    crop = max(0.0, -(lo + shift)) + max(0.0, (hi + shift) - w)
    info["crop_ratio"] = round(crop / char_w, 3) if char_w else 0.0
    if info["crop_ratio"] < MAX_CROP_RATIO:
        return shift, info

    # --- 移動だけでは見切れが大きすぎる → 以前は縮小で逃がしていた -----------
    if not ENABLE_CHARACTER_SCALE:
        info["resolved"] = False
        return 0, info

    # レイヤー自身の bbox の「バブルと反対側の角」を基点に縮める
    # （JSX 側の AnchorPosition.BOTTOMRIGHT / BOTTOMLEFT と対応）。
    # バブル左: 右端を固定して左端を右へ寄せる  new_lo = hi - (hi-lo)*s >= rect[2]
    # バブル右: 左端を固定して右端を左へ寄せる  new_hi = lo + (hi-lo)*s <= rect[0]
    # ⚠️ 生成時にキャンバス外まで描かれたキャラは、その辺で切れている。縮小すると
    #    **切断面が画面内へ移動して露出する**（実例 line_097: 頭頂部が平らに欠ける）。
    #    基点側へ寄る辺（下辺・基点のある左右）は動かないので問題ない。
    #    動くのは上辺と、基点と反対側の左右。そこが切れていたら縮小は使えない。
    e = (mask or {}).get("edges") or {}
    inward = "left" if side == "left" else "right"
    if e.get("top") or e.get(inward):
        info["resolved"] = False
        info["scale_unsafe"] = "切れている辺（%s）が縮小で露出する" % (
            ",".join(k for k in ("top", inward) if e.get(k))
        )
        return 0, info

    # ⚠️ 基点は JSX の AnchorPosition と一致させる＝**レイヤー全体の bbox の角**。
    #    クリアさせたいのは「バブルのy帯における端」なので、両者を混ぜないこと。
    full = (mask or {}).get("bbox")
    if char_w <= 0 or not full:
        info["resolved"] = False
        return 0, info
    full_lo, full_hi = full[0], full[2]
    if side == "left":
        span = full_hi - lo  # 右端を固定して lo を右へ寄せる
        s = (full_hi - rect[2]) / span if span > 0 else 1.0
    else:
        span = hi - full_lo  # 左端を固定して hi を左へ寄せる
        s = (rect[0] - full_lo) / span if span > 0 else 1.0
    s = round(min(1.0, s), 3)
    info["scale_needed"] = s
    if s >= MIN_SCALE:
        info["scale"] = s
        return 0, info

    # 下限より小さくしないと収まらない＝絵として無理がある。下限で止めて知らせる。
    info["resolved"] = False
    info["scale"] = MIN_SCALE
    return 0, info


# --- 背景の画角（framing）とキャラのショット（shot）を合わせる ---------------
# `backgrounds.json` の location は `framing` を持ち、これは Aロールの
# `slot.shot`（panel_presets.json）と**同じ語彙**（Docs/BACKGROUND_ARCHIVE.md §5）。
# 突き合わせないと「寄りのキャラに引きの背景」のような不整合が出て不自然になる。
#
# ⚠️ 在庫の偏り（2026-08-23 実測）: 背景は bust 31 / waist_up 20 / wide 10 で
#    **face_closeup が1枚も無い**。一方で台本は face_closeup が59行ある。
#    そのため完全一致は不可能で、近い画角へ寄せる優先順を持つ。
FRAMING_FALLBACK: dict[str, tuple[str, ...]] = {
    "face_closeup": ("bust", "waist_up", "wide"),  # 在庫が無いので bust で代用
    "bust": ("bust", "waist_up", "wide"),
    "waist_up": ("waist_up", "bust", "wide"),
    # knee は背景の語彙に無いショット（実測で13枚存在）。引き側の waist_up が最も近い
    "knee": ("waist_up", "wide", "bust"),
    "wide": ("wide", "waist_up", "bust"),
    "full_body": ("wide", "waist_up", "bust"),
}


def framing_rank(shot: str | None, framing: str | None) -> int:
    """その shot にとっての framing の近さ。小さいほど良い（0=最良）。"""
    order = FRAMING_FALLBACK.get(shot or "", ("bust", "waist_up", "wide"))
    try:
        return order.index(framing or "")
    except ValueError:
        return len(order)


def side_from_mask(mask: dict | None) -> str | None:
    """キャラのマスクからバブルの左右を決める。判定できなければ None。

    規則は「**バブルは顔と反対側に置く**」＝顔を覆わない。129枚の正解で採点した
    結果、これが最良だった:

        opposite_head  82%   ← 採用（頭部＝マスク上端から30%の帯の重心）
        opposite_body  79%
        free_space     77%
        opposite_bbox  76%
        speaker        64%   ← マスクが無い時のフォールバック

    ⚠️ free_space は参考4枚では 2/4 に見えたが全数では 77% だった。
       少数標本での"発見"は両方向に誤らせる（[[psd-layout-has-no-rule]]）。
    """
    if not mask or mask.get("error") or mask.get("empty"):
        return None
    head = mask.get("head_center_x")
    if head is None:
        return None
    return "left" if head > 0.5 else "right"


# ---------------------------------------------------------------- レイアウト
# バブルの寸法。文字数との相関は r=0.28 とほぼ無相関で、実質固定サイズ。
# 四角は30字で幅520、100字でも幅606（+17%）にしかならない。
BUBBLE_W = 600
BUBBLE_H = 450
# 顔に被らないよう縮める時の下限。ここを下回るなら諦めて被りを受け入れる
# （文字が読めなくなる方が害が大きい）。
BUBBLE_W_MIN = 440
# 顔（頭部bbox）とのあいだに空ける余白
HEAD_MARGIN = 24


def bubble_width_for(mask: dict | None, side: str) -> tuple[int, bool]:
    """顔を覆わない最大のバブル幅を返す。(幅, 顔に被るか)。

    実測（196枚）: 顔までの使える幅は中央値587px。600pxがそのまま入るのは37%、
    下限440pxまで許せば87%が顔に被らずに収まる。バブル全体をキャラと重ねない
    のは非現実的（余裕があるのは14%だけ）なので、**顔だけは覆わない**を基準にする。
    """
    if not mask or mask.get("error") or mask.get("empty") or not mask.get("head_bbox_x"):
        return BUBBLE_W, False
    canvas_w = mask["canvas"][0]
    h0, h1 = mask["head_bbox_x"]
    avail = (h0 - HEAD_MARGIN) if side == "left" else (canvas_w - 1 - h1 - HEAD_MARGIN)
    avail -= BUBBLE_MARGIN_X
    if avail >= BUBBLE_W:
        return BUBBLE_W, False
    if avail >= BUBBLE_W_MIN:
        return int(avail), False
    # 下限まで縮めても顔に被る＝縮めても得るものが無い（文字が小さくなるだけ）。
    # 素直に既定幅へ戻し、読みやすさを取って被りは警告で知らせる。
    return BUBBLE_W, True
# バブル中心のY。129枚の中央値254（10-90%点が197-332）＝上から約1/3。
BUBBLE_CENTER_Y = 254
# 画面端からの余白。バブルは端に寄せて置かれる（実測で負値もあるが0止め）。
BUBBLE_MARGIN_X = 40


@dataclass(frozen=True)
class Inset:
    """バブル枠に対するテキストの内側余白（実測中央値）。"""

    left: int
    top: int
    right: int
    bottom: int  # しっぽの分だけ常に最大になる
    wrap_w: int  # 折り返し幅の実測中央値


# 形状ごとに内側の使える面積が違う（丸・雲は角が死ぬ）
INSETS: dict[str, Inset] = {
    "rect": Inset(40, 45, 36, 153, 495),
    "round": Inset(82, 103, 68, 181, 441),
    "cloud": Inset(96, 138, 89, 157, 437),
    "spike": Inset(97, 149, 89, 185, 397),
}


# ---------------------------------------------------------------- テキスト
# 4枚の参考PSDすべてで完全一致していた値。
FONT_POSTSCRIPT = "DNPShueiNShogoMinStd-Hv"  # DNP秀英丸ゴシック Std Heavy
FONT_SIZE = 50.0
FONT_COLOR_RGB = (0, 0, 0)
JUSTIFICATION = "left"
TRACKING = 0
# Photoshop の禁則は使わない（弱いため）。行分割は kinsoku.py で確定させ、
# 明示改行を入れた点テキストとして流し込む。
USE_PHOTOSHOP_KINSOKU = False

# --- 組版は「予測」ではなく「Photoshopで実測して合わせる」 ------------------
# 既存129枚を解析した結果、文字数とバブル寸法の相関は r=0.23 しかなく、
# 100字超のバブルが 70-100字より小さいなど、**規則は存在しない**（手作業の
# ばらつき）。したがってオフラインで寸法式を立てるのは誤り。
#
# 代わりに JSX 側で次のループを回す:
#   1. 既定サイズのバブルを置く
#   2. FONT_SIZE でテキストを作る
#   3. レイヤーの実描画境界を読み返す
#   4. バブル内側の可用矩形に収まらなければ FONT_STEP 刻みで縮小して再測
#   5. FONT_MIN でも収まらなければ needs_split を立ててユーザーに回す
# これなら書体・字送り・禁則の実挙動に依存せず必ず収まる。
FONT_MIN = 30.0
FONT_STEP = 2.0
FIT_MAX_ITER = 12

# 1つの吹き出しに収まる文字数の実測上限（第1話196枚の合成結果PSDから逆算）。
# FONT_MIN(30px) で既定サイズのバブルに収まった最大が 151字（line_121・はみ出し0.02%＝
# 事実上ギリギリ）。次点は 146字・135字と続き、収まらなかったのは 403字の1件だけ。
# 余裕を見て 140 を「分割の目安」に採る。
#
# ⚠️ **これは分割数の提案にだけ使う値**。最終的な収まりは JSX の実測フィットが決める
# （文字数と寸法に相関は無い＝psd-layout-has-no-rule）。
SPLIT_MAX_CHARS = 140

# 初期見積り用の実測値（12枚の「段落=1行が保証される」標本の中央値）。
# あくまでプラン段階の目安で、最終的な収まりは上記の実測ループが決める。
EST_CHAR_W = 40.7  # 全角1文字の描画幅 @ FONT_SIZE=50
EST_LINE_PITCH = 56.0  # 行送り


def est_chars_per_line(kind: str, font_size: float = FONT_SIZE) -> float:
    """初期見積り: その形状のバブルで1行に入る全角文字数。"""
    scale = font_size / FONT_SIZE
    return INSETS[kind].wrap_w / (EST_CHAR_W * scale)


def inner_rect(kind: str, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
    """バブル矩形 → テキストを置ける内側矩形。"""
    ins = INSETS[kind]
    return (x0 + ins.left, y0 + ins.top, x1 - ins.right, y1 - ins.bottom)


# ---------------------------------------------------------------- 尻尾・反転
# 既存129枚から40枚を実測（シェイプのアルファを描画して尻尾を検出）:
#   尻尾の上下      : bottom 32 / top 1  → 97% が下向き
#   尻尾の左右      : バブルが右にある時 左寄り6 / 右寄り6 と**完全に半々**
# つまり左右反転は規則で決まらない（キャラの顔位置ごとの判断）。
#
# 一方 ExtendScript での反転は `layer.resize(-100, 100, ...)` の1行で無劣化。
# よって bubbles.psd には正位置9枚だけを置き、反転はプランのパラメータにする。
# 反転版を資産に持つと 9×4=36枚になり、作る手間が増えるだけで得るものが無い。
FLIP_V_DEFAULT = False  # 上下反転。97%不要なので既定OFF


def default_flip_h(bubble_key: str, side: str) -> bool:
    """正位置の尻尾向きと、バブルの画面位置から左右反転の既定を決める。

    **尻尾はキャラの方を向く。** バブルはキャラと反対側に置かれるので:
      バブルが画面左 → キャラは右 → 尻尾は右向きが欲しい
      バブルが画面右 → キャラは左 → 尻尾は左向きが欲しい

    実画像で検証済み（2026-08-23）: バブル左・キャラ右の3枚で、私の出力
    （尻尾-0.165＝左向き）をユーザーが左右反転（+0.165＝右向き）に直していた。
    バブル枠は1pxも動かしていない＝**反転だけが誤り**だった。
    """
    shape = BUBBLE_BY_KEY[bubble_key]
    if abs(shape.tail_dx) <= TAIL_NEUTRAL:
        return False  # 本当に無指向なシェイプだけ（現状 Talk 8 のみ）
    want_right = side == "left"
    has_right = shape.tail_dx > 0
    return want_right != has_right


# ---------------------------------------------------------------- 時間帯
# 「隠し部屋でも陽光の入る不思議な構造」という設定なので昼背景は必要。
# プロジェクトごとに選び、組み合わせもできる（例 ["day", "dusk"]）。
# 値は backgrounds.json の location の `light` を束ねたもの。
TIME_OF_DAY: dict[str, tuple[str, ...]] = {
    "day": ("morning", "noon", "bright-day"),  # 実データ 32件
    "dusk": ("sunset",),  # 4件
    "night": ("night-lamp",),  # 20件
    "storm": ("storm",),  # 5件（昼だが暗い。独立させて明示的に選ばせる）
}
DEFAULT_TIME_OF_DAY = ("day",)


def lights_for(time_of_day: list[str] | tuple[str, ...]) -> set[str]:
    """選ばれた時間帯に対応する light の集合。空なら全許可。"""
    out: set[str] = set()
    for t in time_of_day or ():
        out |= set(TIME_OF_DAY.get(t, ()))
    return out


# --- 光源の向きの整合 -------------------------------------------------------
# キャラ生成時に稀に乗る照明エフェクトは立体感が出て好ましいが、背景の光源と
# 逆向きだと途端に違和感が出る（実例 line_014: キャラ +0.296＝右から光、
# 背景 -0.516＝左が明るい）。左右3分割の輝度差で両方測れば機械的に検出できる。
# 実測: キャラ196枚中156枚(80%)、背景69枚中53枚(77%)に明確な方向性がある。
LIGHT_DIR_MIN = 0.08  # これ未満は「方向性なし」とみなす


def light_agreement(char_dx: float | None, bg_dx: float | None) -> float:
    """光源の向きの整合スコア。1.0=一致 / 0.0=判定不能 / -1.0=逆向き。"""
    if char_dx is None or bg_dx is None:
        return 0.0
    if abs(char_dx) < LIGHT_DIR_MIN or abs(bg_dx) < LIGHT_DIR_MIN:
        return 0.0  # どちらかが平坦なら矛盾は起きない
    return 1.0 if char_dx * bg_dx > 0 else -1.0


# ---------------------------------------------------------------- レイヤー
# 129枚で一貫していた重ね順（下→上）。JSX はこの順で組む。
LAYER_ORDER = (
    "background",  # SmartObject（スマートフィルターでブラー）
    "adjust",  # Hue/Saturation（中立値で置く＝ユーザーはスライダーだけ）
    "character",  # キャラPNG＋Select Subject のレイヤーマスク
    "bubble",  # bubbles.psd から複製したシェイプ
    "text",  # セリフ
)

# 背景に既定で乗せるスマートフィルター。ユーザーは強度バーだけ触ればよい。
DEFAULT_BLUR_RADIUS = 8.0

# ★全カテゴリにブラーを乗せる（2026-08-23）。
#   スマートオブジェクト＋ガウスぼかしのスマートフィルター＋Hue/Saturation の
#   組み合わせで「背景のコントロールに必要な全て」が揃う、というユーザー要望。
#   0 だとフィルター自体が作られずスライダーが出ないので、fx 系も小さな非ゼロ値を置く。
BLUR_BY_CATEGORY = {
    "location": 8.0,  # 部屋背景。キャラを立たせるため既定で強め
    "psych": 2.0,
    "comic": 2.0,
    "backdrop": 2.0,
    "effect": 1.0,  # 集中線。ぼかしすぎると線が死ぬので控えめ
}


def blur_for(category: str | None) -> float:
    return BLUR_BY_CATEGORY.get(category or "", 2.0)
