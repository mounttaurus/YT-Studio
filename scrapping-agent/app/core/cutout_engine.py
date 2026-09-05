"""キャラ画像から背景を除去して透過PNGにする（Python完結・Photoshop非依存）.

なぜ2系統あるか:
  - ``flat``  今後の生成は「平らなパステル背景のキャラ単体」に固定した
              （``panel_library_manager.BACKGROUND_FRAGMENT``）。**背景そのものを
              推定できる**ので、AIモデルより正確に抜けて色の混入まで消せる。
  - ``ai``    描き込みのある旧Aロールや、背景が平らでなかった時の保険。
              rembg(ONNX) を使う。モデルは初回のみ自動DL（``REMBG_HOME``）。
  - ``auto``（既定）背景が平らなら flat、そうでなければ ai。

⚠️ ``hybrid``（flat に ai の拾い直しを重ねる）は**既定にしない**。背景モデルが甘かった
うちは救済として効いていたが、背景の推定を直したあとは**誤爆の方が残った** ── 腕と胴の
間に見えている背景のポケットを ai が前景として足す。拾い直しが働くのは
「flat が背景色だと判断した画素」だけなので、そこで ai を信じると背景を混ぜ込む側に倒れる。
方式は残すが opt-in。

⚠️ 背景推定は必ずロバストに行う。外周の帯には**キャラ自身が入り込む**
（腕が左右下の角に届く構図が実在する）。素朴に外周の平均を採ると背景モデルが
汚染され、許容幅が暴走してキャラが溶ける。ここは実測で作り直した箇所。

出力の透過PNGは psassist の Photoshop 工程（``batch_cutout.py``）と同じ意味を持ち、
``analyze_alpha()`` は ``mask_stats.json`` と同じキーを返す。指紋（``fingerprint.py`` の
``shape_rel``）はアルファマスクから作るので、ここが通れば生成物の受け入れ検査
（``cutout_selector.nearest_in_stock``）も動かせる。
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np
from PIL import Image

try:  # scipy は連結成分ラベリングにだけ使う
    from scipy import ndimage as _ndi
except ImportError:  # pragma: no cover
    _ndi = None

# rembg は重い（onnxruntime＋モデルDL）ので、ai/hybrid が呼ばれるまで import しない
_SESSIONS: dict[str, Any] = {}

DEFAULT_AI_MODEL = os.getenv("CUTOUT_AI_MODEL", "isnet-anime")
METHODS = ("auto", "flat", "ai", "hybrid")

_MAX_BG_COLORS = 4          # 背景の色数の上限（左右で色が違う背景まで想定）
_FLAT_RESIDUAL = 14.0       # この残差までなら「平ら」と見なす（0-255 のユークリッド）
_FLAT_COVERAGE = 0.15       # 背景がこれだけ取れなければ平らとは見なさない
_PERIM_FIRST = 0.20         # 1色目に要求する外周の占有率
_PERIM_MORE = 0.25          # 2色目以降に要求する外周の占有率（下記⚠️）
_FG_BLOB = 0.55             # 前景の最大塊が前景全体に占める割合の下限（健全性）
_HOLE_RATIO = 0.01          # 縁と繋がらなくても背景として拾う塊の上限（画面比）
# 穴開き（顔が欠ける）と抜け残り（髪の間が白く残る）の交換比率を70枚で掃引して決めた。
# 残りは手で消せるが穴は描き直しになるので、残る側へ倒す方針。
#   ring/frac  穴総     残総     穴@肌≒背景の顔  穴@髪の隙間
#   3.0/0.80   179298   104143   1116            7681
#   4.0/0.85   140779   142662    126            6736  ← 採用（膝）
#   5.0/0.90    94471   188970     96            4541
#   6.0/0.95    41838   241603      0             808  ← 髪の隙間が抜けなくなる
# 4.0 を超えると顔はほぼ改善しないのに髪の除去だけ落ちる。
_HOLE_RING = 4.0            # 同・塊を囲む前景に要求する正規化距離
_HOLE_RING_FRAC = 0.85      # 同・その条件を満たすべき輪の割合
_ENCLOSED_MAX = 0.03        # 囲まれた背景色の面積がこれを超えたら flat を信用しない


# ─── 連結成分 ───────────────────────────────────────────────

def _label(mask: np.ndarray) -> tuple[np.ndarray, int]:
    if _ndi is None:  # pragma: no cover
        raise RuntimeError("scipy が必要です（連結成分ラベリング）")
    lbl, n = _ndi.label(mask)
    return lbl, int(n)


def _edge_connected(mask: np.ndarray, d: np.ndarray | None = None) -> np.ndarray:
    """画面の縁に繋がっている領域を残す。囲まれた白シャツを背景と誤らないため。

    ``d``（正規化距離）を渡すと、**縁に繋がっていない塊も条件付きで背景として拾う**。
    髪の束の間に閉じ込められた背景がこれ（実測: これが「髪まわりの抜け残り」の正体）。

    ⚠️ 判定は大きさでも細さでもない。どちらで線を引いても、肌と背景がほぼ同色の
    画像で**顔が穴だらけになった**（囲まれた肌の断片が次々と背景扱いされる）。
    効くのは**その塊が何に囲まれているか**:

    - 髪の隙間は、背景から遠い色（黒髪）に囲まれている → 背景で間違いない
    - 顔の断片は、背景に近い色（肌）に囲まれている → キャラの一部

    塊の外側3〜7pxにある前景の色を見て決める。物理的に意味のある軸なので、
    画像ごとの閾値調整が要らない。
    """
    if not mask.any():
        return mask
    lbl, n = _label(mask)
    if n == 0:
        return mask
    edge = np.concatenate([lbl[0], lbl[-1], lbl[:, 0], lbl[:, -1]])
    keep = set(np.unique(edge[edge > 0]).tolist())
    if d is None:
        return np.isin(lbl, sorted(keep))

    areas = np.bincount(lbl.ravel(), minlength=n + 1)
    limit = mask.size * _HOLE_RATIO
    objs = _ndi.find_objects(lbl)
    for i in range(1, n + 1):
        if i in keep or areas[i] > limit or objs[i - 1] is None:
            continue
        sl = tuple(slice(max(0, s.start - 9), min(m, s.stop + 9))
                   for s, m in zip(objs[i - 1], mask.shape))
        # 輪の位置が肝。塊のすぐ隣はアンチエイリアスの中間色なので、少し外側を見る
        hole = lbl[sl] == i
        ring = (_ndi.binary_dilation(hole, iterations=7)
                & ~_ndi.binary_dilation(hole, iterations=2) & ~mask[sl])
        # ⚠️ 中央値ではなく「輪の大半が遠いこと」を要求する。中央値だと、目や口の
        # 縁など一部だけ暗い場所で条件を満たしてしまい、肌に穴が開いた
        if ring.any() and float((d[sl][ring] >= _HOLE_RING).mean()) >= _HOLE_RING_FRAC:
            keep.add(i)
    return np.isin(lbl, sorted(keep))


# ─── 背景モデル ─────────────────────────────────────────────

def _border_mask(h: int, w: int, frame: int = 8, inset: int = 2) -> np.ndarray:
    """外周の帯。inset で額縁状のアーティファクトを避ける。"""
    f = max(1, min(frame, h // 8, w // 8))
    i = max(0, min(inset, f))
    m = np.zeros((h, w), bool)
    m[i : i + f, :] = True
    m[h - i - f : (h - i) or None, :] = True
    m[:, i : i + f] = True
    m[:, w - i - f : (w - i) or None] = True
    return m


def _modal_color(pts: np.ndarray) -> np.ndarray:
    """量子化した最頻ビンの重心。平均と違い、混入した少数派に引っ張られない。"""
    q = (pts // 24).astype(np.int32)
    keys = q[:, 0] * 4096 + q[:, 1] * 64 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    return pts[keys == vals[int(counts.argmax())]].mean(0)


def _robust_tol(res: np.ndarray) -> float:
    """中央値＋4MAD。外れ値（帯に混じったキャラ）に釣られない許容幅。"""
    med = float(np.median(res))
    mad = float(np.median(np.abs(res - med))) * 1.4826
    return float(np.clip(med + 4.0 * mad + 3.0, 8.0, 40.0))


def _poly_basis(h: int, w: int) -> np.ndarray:
    """2次の2D多項式基底。中心が明るいグラデーション背景を表せる。"""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    x = x / max(1, w - 1) * 2 - 1
    y = y / max(1, h - 1) * 2 - 1
    return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], -1)


def _grow(rgb: np.ndarray, seed: np.ndarray, taken: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """1色ぶんの背景領域を育てる。種→領域→種を数回往復させて安定させる。"""
    tol = 20.0
    region = np.zeros(rgb.shape[:2], bool)
    d = np.linalg.norm(rgb - seed, axis=2)
    for _ in range(4):
        region = _edge_connected((d < tol) & ~taken)
        if not region.any():
            break
        seed = np.median(rgb[region], axis=0)
        d = np.linalg.norm(rgb - seed, axis=2)
        tol = _robust_tol(d[region])
    return region, seed, tol


def _corner_mask(h: int, w: int) -> np.ndarray:
    """四隅のパッチ。背景の種はここから採る（キャラが最も来にくい場所）。"""
    s = max(24, int(min(h, w) * 0.04))
    m = np.zeros((h, w), bool)
    m[:s, :s] = m[:s, -s:] = m[-s:, :s] = m[-s:, -s:] = True
    return m


def _perimeter_share(region: np.ndarray) -> float:
    """画面の最外周1画素のうち、この領域が占める割合。"""
    ring = np.concatenate([region[0], region[-1], region[1:-1, 0], region[1:-1, -1]])
    return float(ring.mean())


def estimate_background(rgb: np.ndarray) -> dict[str, Any]:
    """四隅を種に背景領域と背景色を推定する。色数ぶん繰り返して育てる。

    ⚠️ 「縁に繋がる平らな塊」だけでは背景と決められない。画面下端に接した黒髪や
    紺のブレザーがその条件を満たしてしまい、実際にキャラが消えた。背景と認めるには
    **四隅のどれかを含み、かつ外周を十分に占めている**ことを要求する。

    ⚠️ 2色目の条件が本当に効く場所。左右に伸ばした素肌の腕が下の両隅に届く構図では、
    肌色が2色目の背景として採用されて腕が丸ごと消えた（実測: 背景ピンク
    (252,199,207) に対し採用された2色目が (251,222,211)＝腕の肌そのもの）。
    腕は外周の13%しか占めないのに対し、左右で色が違う本物の2色背景は各50%を占める。
    **外周占有率で分かれる**ので、2色目には25%を要求する。取りこぼして背景が残る方が、
    キャラを削るより安全（残りは目視で分かる）。
    """
    h, w, _ = rgb.shape
    corners = _corner_mask(h, w)
    taken = np.zeros((h, w), bool)
    centroids: list[np.ndarray] = []
    tols: list[float] = []

    for k in range(_MAX_BG_COLORS):
        pts = rgb[corners & ~taken]
        if len(pts) < corners.sum() * 0.10:
            break
        region, seed, tol = _grow(rgb, _modal_color(pts), taken)
        if not region.any():
            break
        # 四隅のどれも含まない＝画面の外周を囲っていない＝背景ではない
        if not (region & corners).any():
            break
        if _perimeter_share(region) < (_PERIM_FIRST if k == 0 else _PERIM_MORE):
            break
        # 平らでない塊（＝キャラの一部）は背景として採らない
        if float(np.percentile(np.linalg.norm(rgb[region] - seed, axis=1), 90)) > 30.0:
            break
        centroids.append(seed)
        tols.append(tol)
        taken |= region

    if not centroids:  # 縁が全部キャラ等。平らではないと申告して ai に任せる
        return {"model": "none", "region": taken, "flat": False,
                "residual_p95": 999.0, "coverage": 0.0, "colors": 0}

    # 距離は**代表色ごとの許容幅で正規化**する（1.0＝その色の境界）。
    # 全色で最大の許容幅を共用すると、緩い1色に引きずられて肌が背景に飲まれる。
    cent = np.asarray(centroids, np.float32)
    raw = np.stack([np.linalg.norm(rgb - c, axis=2) for c in cent], -1)
    d = (raw / np.asarray(tols, np.float32)).min(-1)
    raw_min = raw.min(-1)

    out: dict[str, Any] = {
        "model": "cluster", "coef": None, "centroids": cent, "tols": tols,
        "region": taken, "colors": len(centroids),
    }
    out["residual_p95"] = round(float(np.percentile(raw_min[taken], 95)), 2)

    # グラデーション背景は多項式1本の方が締まる（クラスタに割れると許容幅が緩む）
    if taken.mean() > 0.05:
        basis = _poly_basis(h, w)
        coef, *_ = np.linalg.lstsq(basis[taken], rgb[taken], rcond=None)
        raw_p = np.linalg.norm(rgb - (basis @ coef), axis=2)
        tol_p = _robust_tol(raw_p[taken])
        if float(np.percentile(raw_p[taken], 95)) < out["residual_p95"] * 0.9:
            out |= {"model": "poly", "coef": coef, "tol_poly": tol_p,
                    "residual_p95": round(float(np.percentile(raw_p[taken], 95)), 2)}
            d = raw_p / tol_p

    out["distance"] = d
    out["coverage"] = round(float(taken.mean()), 4)
    # 健全性: 背景を引いた残り（＝キャラ）が1つの塊にならないなら背景モデルを疑う。
    # 髪や上着を背景と誤ると、残るのは輪郭線だけのバラバラな図形になる（実際に起きた）
    fg = ~taken
    blob = 0.0
    if fg.any():
        lbl, n = _label(fg)
        if n:
            areas = np.bincount(lbl.ravel())
            areas[0] = 0
            blob = float(areas.max() / fg.sum())
    out["fg_blob"] = round(blob, 3)

    # 背景と同じ色がキャラの内側に大量にある＝背景色とキャラの色が衝突している。
    # 実測: 肌と背景がほぼ同色の画像で顔の断片が背景扱いされた。こうなると flat は
    # 原理的に分離できないので、判定を降ろして ai に任せる
    cand = d < 1.0
    enclosed = float((cand & ~_edge_connected(cand)).mean())
    out["enclosed"] = round(enclosed, 4)

    out["flat"] = (out["residual_p95"] < _FLAT_RESIDUAL
                   and out["coverage"] >= _FLAT_COVERAGE
                   and blob >= _FG_BLOB
                   and enclosed < _ENCLOSED_MAX)
    return out


def _bg_predict(bg: dict[str, Any], rgb: np.ndarray) -> np.ndarray:
    """各画素における背景色の推定値（デコンタミネーション用）。"""
    h, w, _ = rgb.shape
    if bg["model"] == "poly":
        return (_poly_basis(h, w) @ bg["coef"]).astype(np.float32)
    cent = bg["centroids"]
    idx = np.argmin(np.stack([np.linalg.norm(rgb - c, axis=2) for c in cent], -1), -1)
    return cent[idx]


# ─── アルファの生成 ─────────────────────────────────────────

def flat_alpha(rgb: np.ndarray, bg: dict[str, Any], band_px: int = 3) -> np.ndarray:
    """境界の帯だけ ``C = aF + (1-a)B`` を a について解く（本来の matting）。

    ⚠️ 距離を背景の許容幅で正規化した値をそのままアルファに使ってはいけない。
    許容幅は背景のノイズ幅（≒19）だが、髪と背景の実際の色差は 200 前後ある。
    比が合っていないので、髪が1割混じっただけの画素が即座に完全不透明になり、
    髪の間に**背景色の抜け残り**として見える（実測: 中間アルファを持つ画素が
    全体の 0.09% しかない＝事実上の2値マスクだった）。

    そこで境界の帯では、背景色 B と**近傍の確信できる前景色 F** から
    a = |C-B| / |F-B| を解く。F は空間的に最も近い「帯の外側の画素」から採るので、
    囲まれた白シャツのような明るい領域を巻き込まない（帯は背景から3px以内だけ）。
    """
    if bg["model"] == "none":
        return np.ones(rgb.shape[:2], np.float32)
    region = _edge_connected(bg["distance"] < 1.0, bg["distance"])
    zone = _ndi.binary_dilation(region, iterations=band_px)
    solid = ~zone
    if not solid.any():
        return np.ones(rgb.shape[:2], np.float32)

    b = _bg_predict(bg, rgb)
    d_raw = np.linalg.norm(rgb - b, axis=2)
    iy, ix = _ndi.distance_transform_edt(zone, return_indices=True)[1]
    f_local = rgb[iy, ix]
    # 分母の下限。F と B が近い時（金髪×桃色背景など）にノイズを増幅させない
    denom = np.maximum(np.linalg.norm(f_local - b, axis=2), 25.0)
    a = np.clip(d_raw / denom, 0.0, 1.0)
    a[a < 0.03] = 0.0
    return np.where(zone, a, 1.0).astype(np.float32)


def ai_alpha(img: Image.Image, model: str = DEFAULT_AI_MODEL) -> np.ndarray:
    """rembg(ONNX) のマスク。初回はモデルをDLする。"""
    from rembg import new_session, remove  # 遅延 import（重い）

    if model not in _SESSIONS:
        _SESSIONS[model] = new_session(model)
    mask = remove(img.convert("RGB"), session=_SESSIONS[model], only_mask=True)
    return np.asarray(mask, np.float32) / 255.0


def _combine(a_flat: np.ndarray, a_ai: np.ndarray) -> np.ndarray:
    """flat を主、ai は**拾い直しにだけ**使う（訂正は片方向）。

    ai が前景だと言い切る(>=0.5)のに flat が消している画素を拾い直す。
    背景色に近い肌・金髪が食われる事故の手当て。

    ⚠️ 逆向き（ai が背景だと言う画素を落とす）は**やらない**。背景が平らだと確認できた
    時点で flat の「残す」判断は背景色との距離という確かな根拠を持つ一方、
    ai は 320px 級に縮めて推論するので細い腕を丸ごと見落とす。
    落とす方向に効かせると、正しく残った部位が消える事故になる。
    背景側の残骸は ``_drop_specks`` が担当する。
    """
    a = a_flat.copy()
    pick_up = (a_ai >= 0.5) & (a_flat < a_ai)
    a[pick_up] = a_ai[pick_up]
    return a, float(pick_up.mean())


def _drop_specks(alpha: np.ndarray, min_ratio: float = 0.01) -> np.ndarray:
    """最大の塊に対して極端に小さい島を捨てる（抜き残りのゴミ）。"""
    solid = alpha > 0.5
    if not solid.any():
        return alpha
    lbl, n = _label(solid)
    if n <= 1:
        return alpha
    areas = np.bincount(lbl.ravel())
    areas[0] = 0
    keep = np.where(areas >= areas.max() * min_ratio)[0]
    return np.where(np.isin(lbl, keep), alpha, 0.0).astype(np.float32)


def decontaminate(rgb: np.ndarray, alpha: np.ndarray, bg: dict[str, Any]) -> np.ndarray:
    """半透明画素に混ざった背景色を引き算する（合成時の色フチを消す）。

    観測 C = a*F + (1-a)*B を F について解く。B が既知なのは flat 背景ならでは。
    """
    if bg["model"] == "none":
        return rgb
    band = (alpha > 0.05) & (alpha < 0.95)
    if not band.any():
        return rgb
    b = _bg_predict(bg, rgb)
    out = rgb.copy()
    a = alpha[band][:, None]
    out[band] = np.clip((rgb[band] - (1.0 - a) * b[band]) / a, 0.0, 255.0)
    return out


# ─── 入口 ───────────────────────────────────────────────────

def cut_out(
    img: Image.Image,
    method: str = "auto",
    ai_model: str = DEFAULT_AI_MODEL,
    decontam: bool = True,
    drop_specks: bool = True,
) -> tuple[Image.Image, dict[str, Any]]:
    """背景を除去して RGBA を返す。info には判断材料（背景モデル・残差）を入れる。"""
    if method not in METHODS:
        raise ValueError(f"method は {METHODS} のいずれか: {method!r}")
    rgb = np.asarray(img.convert("RGB"), np.float32)
    info: dict[str, Any] = {"method": method}

    bg = estimate_background(rgb) if method != "ai" else {"model": "none"}
    if method != "ai":
        info |= {
            "bg_model": bg["model"], "bg_colors": bg["colors"],
            "bg_residual_p95": bg["residual_p95"], "bg_coverage": bg["coverage"],
            "bg_flat": bg["flat"],
            "bg_tol": (round(bg["tol_poly"], 1) if bg["model"] == "poly"
                       else [round(t, 1) for t in bg.get("tols", [])]),
            "fg_blob": bg.get("fg_blob"), "enclosed": bg.get("enclosed"),
        }

    if method == "ai" or (method in ("auto", "hybrid") and not bg["flat"]):
        alpha = ai_alpha(img, ai_model)
        info |= {"ai_model": ai_model, "effective": "ai"}
        decontam = False
    elif method in ("auto", "flat"):
        alpha = flat_alpha(rgb, bg)
        info["effective"] = "flat"
    else:
        alpha, picked = _combine(flat_alpha(rgb, bg), ai_alpha(img, ai_model))
        info |= {"ai_model": ai_model, "effective": "hybrid", "picked_up": round(picked, 4)}

    if drop_specks:
        alpha = _drop_specks(alpha)
    if decontam:
        rgb = decontaminate(rgb, alpha, bg)

    out = np.dstack([rgb, alpha * 255.0]).astype(np.uint8)
    info["coverage"] = round(float((alpha > 0.5).mean()), 4)
    return Image.fromarray(out, "RGBA"), info


def analyze_alpha(rgba: Image.Image) -> dict[str, Any]:
    """psassist の ``mask_stats.json`` と同じキーを返す（下流の互換のため）。"""
    a = np.asarray(rgba.convert("RGBA"))[:, :, 3]
    h, w = a.shape
    on = a > 128
    if not on.any():
        return {"empty": True}
    ys, xs = np.where(on)
    col = on.mean(0)
    solid = col >= 0.02
    left = int(np.argmax(solid))
    right = w - 1 - int(np.argmax(solid[::-1]))
    top_h = max(1, int((ys.max() - ys.min()) * 0.30))
    head = on[ys.min() : ys.min() + top_h]
    hx = np.where(head.any(0))[0]
    return {
        "empty": False,
        "canvas": [w, h],
        "bbox": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())],
        "coverage": round(float(on.mean()), 4),
        "free_left": left,
        "free_right": w - 1 - right,
        "center_x": round(float(xs.mean()) / w, 4),
        "head_center_x": round(float(hx.mean()) / w, 4) if len(hx) else None,
        "head_bbox_x": [int(hx.min()), int(hx.max())] if len(hx) else None,
    }
