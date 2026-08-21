"""
背景アーカイブの構造化入力 → 英語プロンプト断片のプリセット辞書。

panel_presets.py（Aロールの演技スロット）と対の存在。語彙の正本はこのファイル
（Phase 0 では Docs/BACKGROUND_ARCHIVE.md が正本だったが、Phase 1 で移設した）。
shared/imagegen/background_presets.json に外出しし、ユーザーが項目を追加・編集できる。
無ければデフォルトを書き出す（panel_presets / style_manager と同じ方針）。

背景は3系統（category）:
  location = 隠し部屋の各所（spot または motif を1つ選ぶ・排他）
  psych    = 心理背景（form）
  comic    = コミック背景（effect）

location はさらに framing（キャラのショットサイズ）で寄り引きを切り替える。
framing の id は panel_presets.json の shot と同一（Aロールのslot.shotからそのまま引ける）。

詳細な設計判断・実測に基づく罠は Docs/BACKGROUND_ARCHIVE.md を参照。
"""
import json
import os
from pathlib import Path

SHARED_DIR = Path(os.getenv("SHARED_DIR", "/shared"))
PRESETS_FILE = SHARED_DIR / "imagegen" / "background_presets.json"

# 背景用スタイル（画風）は shared/imagegen/styles.json 側に定義する（style_manager.py の
# kamishibai_bg / kamishibai_fx）。ここでは「どちらを使うか」の対応だけを持つ。
STYLE_BY_CATEGORY = {"location": "kamishibai_bg", "psych": "kamishibai_fx", "comic": "kamishibai_fx"}

# ── 部屋の署名（location 共通・必須。§6） ──────────────────────────
SIGNATURE_FULL = (
    "a secret chamber hidden behind the academy library, tall crammed bookshelves lining the "
    "stone walls, gothic stonework older than the rest of the school, ribbed vaulted ceiling "
    "with carved stone corbels, quatrefoil tracery, wrought iron gallery railing above, "
    "rose-cross and iris emblems worked into the masonry, sealed document boxes and bundled "
    "research notes of a clandestine archive club, brass fittings, spent candle stubs, "
    "deep saturated colors, aged oak and oxblood leather, tarnished brass and gilt accents"
)
SIGNATURE_SHORT = (
    "a secret chamber hidden behind the academy library, crammed bookshelves, gothic stonework, "
    "brass fittings, spent candle stubs, aged oak and oxblood leather, tarnished brass and gilt accents"
)
# フル版は天井・回廊まで描写を要求するため、至近の画角(face_closeup/bust/waist_up)で使うと
# 部屋の全景を描こうとして破綻する。full_body/wide のみフル版を使う。
SIGNATURE_BY_FRAMING = {
    "face_closeup": "short", "bust": "short", "waist_up": "short",
    "full_body": "full", "wide": "full",
}

# ── 固定フラグメント（★必ず全プロンプトの末尾に付ける。§7） ──────────────
TAIL_LOCATION = (
    "no people, no characters, empty background only, no text, no lettering, no watermark, "
    "no signature, no border or frame, image fills the frame edge to edge, the lower centre of "
    "the frame is plain and uncluttered with no large objects, nothing of interest in the middle "
    "of the frame"
)
TAIL_ABSTRACT = (
    "no people, no characters, no scenery, no text, no lettering, no watermark, no signature, "
    "no border or frame, no letterboxing"
)
# 現代の要素（era touch）専用の追記。「浮かない」ための鍵（§6「現代の要素」運用ルール）。
TAIL_ERA_TOUCH_SUFFIX = (
    "rendered in the same soft painterly style as the rest of the room, not photorealistic"
)

DEFAULT_PRESETS = {
    "spot": [
        {"id": "main-desk", "label_ja": "大机",
         "prompt": "massive oak reading desk piled with old leather-bound tomes and loose "
                   "parchment, brass desk lamp with green glass shade, quill pen and inkwell, "
                   "worn leather chair, rose-and-iris carved wood paneling behind, flagstone "
                   "floor, red string pinned between documents"},
        {"id": "fireplace", "label_ja": "暖炉",
         "prompt": "large gothic stone fireplace with a carved pointed-arch mantel flanked by "
                   "stone columns with carved capitals, crackling fire, wrought iron fire tools, "
                   "worn leather armchairs facing the hearth, rose and iris motifs carved into "
                   "the stone lintel, older masonry patched with newer brick"},
        {"id": "stacks", "label_ja": "書架回廊",
         "prompt": "towering multi-tiered oak bookshelves receding into a vaulted corridor, "
                   "clerestory windows high along the wall, mezzanine gallery walkway above, "
                   "leaded-glass reading-nook lanterns, sliding brass ladder on a rail, dust "
                   "motes drifting in shafts of light"},
        {"id": "window-seat", "label_ja": "窓辺の出窓席",
         "prompt": "gothic bay window alcove with a cushioned window seat, stained glass panes "
                   "depicting roses and irises, leaded glass tracery, worn velvet cushions, "
                   "stacked books on the sill, pointed arch window frame, worn stone threshold"},
        {"id": "spiral-stairs", "label_ja": "螺旋階段",
         "prompt": "narrow wrought iron spiral staircase winding around a stone spiral flute "
                   "column, ascending into shadow, worn stone steps, brass handrail with a rose "
                   "finial, tall bookshelves curving around the stairwell, single hanging lantern"},
        {"id": "cabinet", "label_ja": "標本棚（珍品棚）",
         "prompt": "antique glass-fronted curiosity cabinet with brass hardware, a small "
                   "grotesque carving perched atop the frame, rows of labeled specimen jars and "
                   "strange relics, dark oak frame carved with iris motifs, a cipher wheel "
                   "tucked among the relics, dim glow from a nearby candle"},
        {"id": "map-table", "label_ja": "地図台",
         "prompt": "large oak map table with an unrolled antique parchment map, brass compass "
                   "and dividers, hanging pendant lamp with a stained-glass shade, a nearby "
                   "stone column with a carved capital, worn leather map cases stacked at the "
                   "table's edge, a wax-sealed folio resting among the map cases"},
        {"id": "hidden-door", "label_ja": "隠し扉（書架に偽装された入口）",
         "prompt": "bookshelf disguised as a hidden door, one section swung open revealing a "
                   "dark passage beyond, brass lever mechanism hidden among the book spines, "
                   "dust disturbed on the floor, pointed arch doorway beyond, a walled-up window "
                   "nearby hinting at the room's older concealment, worn stone threshold"},
        {"id": "skylight", "label_ja": "天窓下",
         "prompt": "circular stained-glass skylight overhead casting colored light down through "
                   "drifting dust, tall bookshelves ringing the round chamber below, an iron "
                   "rood screen partially encircling the space, worn wooden reading table "
                   "centered beneath the light shaft, rose window tracery"},
        {"id": "alcove", "label_ja": "祭壇跡のアルコーブ（薔薇十字の紋章・古い礼拝の名残）",
         "prompt": "small stone alcove with the faded remains of a rosicrucian altar, a "
                   "reliquary niche in the back wall, remnants of a carved choir stall to the "
                   "side, stub candles in tarnished brass holders, pointed arch niche, iris "
                   "flowers carved along the edges"},
    ],
    # motif = spotよりさらに寄った調度品1点。spotとは排他（§5）。bust/face_closeupで多用する。
    "motif": [
        {"id": "clock", "label_ja": "壁掛け時計",
         "prompt": "an old pendulum wall clock in a carved oak case mounted on the stone wall"},
        {"id": "ladder", "label_ja": "書架の梯子",
         "prompt": "a brass library ladder leaning against a bookcase"},
        {"id": "window-pillar", "label_ja": "窓と柱",
         "prompt": "the stone reveal and deep sill of a tall leaded-glass window"},
        {"id": "sconce", "label_ja": "壁の燭台",
         "prompt": "an ornate wrought iron wall sconce holding a lit candle"},
        {"id": "candelabra", "label_ja": "大燭台",
         "prompt": "a tall tarnished brass candelabra with several lit candles"},
        {"id": "iron-gate", "label_ja": "鉄格子の扉",
         "prompt": "an ornate wrought iron gate set into an archway"},
        {"id": "stained-glass", "label_ja": "ステンドグラス断片",
         "prompt": "a stained glass window fragment depicting a rose and iris pattern"},
        {"id": "tapestry", "label_ja": "タペストリー",
         "prompt": "a faded old tapestry embroidered with rose and iris motifs hanging on the "
                   "stone wall"},
        # 2026-08-21実測: globe/crest は対象物が画面に写らず失敗（2回ずつ試行）。ただし
        # OpenRouterフォールバック中(旧世代Nano Banana 1)だったため「失敗」と断定していない。
        # Nano Banana 2 直叩きでの再試行価値あり（Docs/BACKGROUND_ARCHIVE.md §6参照）。
    ],
    # era = 現代の要素。物語の舞台は現代（極秘文献調査部は現代の女子校の生徒）。
    # ⚠️ motifとは別軸: motifは「部屋に属する調度品」、eraは「キャラが持ち込む携行品」。
    # 設備（ランプ・時計・家具）は現代化しない。携行品（PC・スマホ・バッグ）だけが対象。
    "era": [
        {"id": "modern-laptop", "label_ja": "ノートPC",
         "prompt": "the corner of a massive oak reading desk, a slim closed modern laptop "
                   "resting beside stacks of old leather-bound books"},
        {"id": "modern-backpack", "label_ja": "通学バッグ",
         "prompt": "the foot of a tall antique bookshelf, a modern canvas school backpack "
                   "leaning casually against it"},
        # modern-lamp（LEDデスクランプ）は設計ミスとして2026-08-21に非推奨化した。
        # ランプは部屋の「設備」であり携行品ではない＝現代化してはいけない対象だった。
        # 既存の生成物は削除しないが、このプリセットには含めない（新規追加もしない）。
    ],
    "light": [
        {"id": "morning", "label_ja": "朝の斜光",
         "prompt": "soft morning light in warm gold shafts through leaded-glass windows, gentle "
                   "long shadows, faint dust motes drifting"},
        {"id": "noon", "label_ja": "昼の柔光",
         "prompt": "even soft daylight filling the room, diffuse and calm, muted warm tones, "
                   "minimal shadow contrast"},
        {"id": "bright-day", "label_ja": "明るい昼",
         "prompt": "clear bright daylight through tall leaded-glass windows, the room is well "
                   "lit and legible, warm sunlit highlights against deep oak and stone, "
                   "moderate contrast retained"},
        {"id": "sunset", "label_ja": "夕陽",
         "prompt": "deep orange and amber sunset light streaming low through stained glass, "
                   "long dramatic shadows, warm glowing highlights"},
        {"id": "night-lamp", "label_ja": "夜（ランプと燭台）",
         "prompt": "dim warm candlelight and brass oil lamps, deep shadows in the corners, "
                   "flickering golden pools of light"},
        {"id": "storm", "label_ja": "嵐（雷光と雨の窓）",
         "prompt": "cold blue-grey storm light, flashes of distant lightning through "
                   "rain-streaked leaded glass, rain trails on the windows, dramatic chiaroscuro"},
    ],
    # camera は framing が wide / full_body の時だけ使う（それ以外は被写界深度が支配的で
    # 構図が成立しないため null。§6「framing」表の⚠️参照）。
    "camera": [
        {"id": "eye-level-wide", "label_ja": "引き（目線）",
         "prompt": "wide establishing shot, eye-level camera, full room visible"},
        {"id": "low-angle", "label_ja": "煽り（天井を見せる）",
         "prompt": "low angle shot looking upward, ceiling and upper shelves prominent, "
                   "dramatic vertical perspective"},
        {"id": "high-angle", "label_ja": "俯瞰",
         "prompt": "high angle shot looking downward, overhead bird's-eye perspective, floor "
                   "and furniture layout visible"},
        {"id": "close-detail", "label_ja": "寄り（被写界深度浅め）",
         "prompt": "close-up detail shot, shallow depth of field, soft bokeh background, focus "
                   "on foreground textures"},
        {"id": "doorway-frame", "label_ja": "額縁構図（戸口越し）",
         "prompt": "framed through a doorway or archway in the foreground, vignette framing, "
                   "view into the room beyond"},
    ],
    # framing の id は panel_presets.json の shot と同一にする（Aロールのslot.shotから
    # そのまま引けるようにするため。mood↔emotion と同じ接続方式）。
    "framing": [
        {"id": "face_closeup", "label_ja": "顔アップの背後", "status": "untested",
         "prompt": "background plate for an extreme close-up, only {focus} is visible, "
                   "extremely blurred, almost abstract, minimal detail"},
        {"id": "bust", "label_ja": "バストアップの背後", "status": "validated",
         "prompt": "background plate for a bust-shot character, the camera is close and focused "
                   "on a person standing about one metre in front of this background, only a "
                   "small section of {focus} is visible, moderately blurred with soft depth of "
                   "field, low detail"},
        {"id": "waist_up", "label_ja": "ウエストアップの背後", "status": "validated",
         "prompt": "background plate for a waist-up shot, the camera is focused on a person "
                   "standing in front, {focus}, two to three metres behind them, noticeably "
                   "blurred with soft depth of field, moderate low detail"},
        {"id": "full_body", "label_ja": "全身の背後", "status": "untested",
         "prompt": "background plate for a full-body shot, {focus}, floor visible in the lower "
                   "frame, three to five metres away, gently blurred"},
        {"id": "wide", "label_ja": "部屋全景", "status": "validated",
         "prompt": "wide establishing view of the whole room featuring {focus}, everything in "
                   "sharp focus, no depth-of-field blur"},
    ],
    # mood はタグでありプロンプト断片ではない（生成には使わない。backgrounds.json の mood配列
    # とサジェスト照合にのみ使う。3系統共通）。
    "mood": [
        {"id": "calm", "label_ja": "静穏"}, {"id": "nostalgic", "label_ja": "郷愁"},
        {"id": "tense", "label_ja": "緊張"}, {"id": "ominous", "label_ja": "不穏"},
        {"id": "revelation", "label_ja": "発見・開示"}, {"id": "mystery", "label_ja": "神秘"},
        {"id": "warm", "label_ja": "親密"}, {"id": "melancholy", "label_ja": "憂い"},
        {"id": "urgent", "label_ja": "切迫"}, {"id": "playful", "label_ja": "軽妙"},
    ],
    "form": [
        {"id": "gradient", "label_ja": "単色グラデーション",
         "prompt": "smooth solid color gradient background, soft two-tone blend, no scenery, "
                   "flat abstract backdrop"},
        {"id": "question-marks", "label_ja": "？マーク散布",
         "prompt": "scattered translucent question mark symbols floating across a flat "
                   "gradient background, varying sizes, soft drop shadow, comic psychological "
                   "background"},
        {"id": "radial-burst", "label_ja": "放射グラデーション",
         "prompt": "radial burst gradient background, lines radiating outward from the center, "
                   "comic impact background, no scenery"},
        {"id": "starry", "label_ja": "星・きらめき",
         "prompt": "dark gradient background scattered with small sparkling stars and soft "
                   "glints of light, dreamy comic background, no scenery"},
        {"id": "flash", "label_ja": "白フラッシュ",
         "prompt": "solid bright white flash background, radiating faint soft rays, high-key "
                   "blank background, no scenery"},
        {"id": "dark-vignette", "label_ja": "暗転ビネット",
         "prompt": "dark vignette background, deep shadow at the edges fading from a dim "
                   "center, ominous flat backdrop, no scenery"},
    ],
    "effect": [
        {"id": "speedlines-radial", "label_ja": "放射集中線",
         "prompt": "bold radial speed lines converging toward the center, black and white "
                   "manga impact background, high contrast"},
        {"id": "speedlines-horizontal", "label_ja": "流線",
         "prompt": "bold horizontal motion speed lines streaking across the frame, manga "
                   "action background, dynamic blur"},
        {"id": "halftone", "label_ja": "網点",
         "prompt": "comic halftone dot pattern background, bold flat color, screentone "
                   "texture, retro comic print look"},
        {"id": "impact-flash", "label_ja": "衝撃フラッシュ",
         "prompt": "jagged comic impact flash burst background, bold spiked star shape, "
                   "bright flat color, graphic novel style"},
        {"id": "ink-splatter", "label_ja": "墨の飛沫",
         "prompt": "black ink splatter and spray texture scattered across a flat background, "
                   "dramatic comic background, no scenery"},
    ],
}

GROUPS = ("spot", "motif", "era", "light", "camera", "framing", "mood", "form", "effect")
# camera が意味を持つ framing（それ以外は被写界深度が支配的なため camera を使わない）
CAMERA_FRAMINGS = ("wide", "full_body")

# Aロールのslot.emotion（panel_presets.py）→本アーカイブのmoodタグへのサジェスト表。
# 元はDocs/BACKGROUND_ARCHIVE.md §12にのみ記載されていたが、行単位の背景自動割当
# （aroll_manager.auto_assign_backgrounds）がコードから参照する必要があるため移設した
# （1事実1ホーム。Docは参照のみに変更）。あくまでサジェストであり自動確定ではないが、
# 自動割当の初期値としてはそのまま使う。
EMOTION_TO_MOOD = {
    "neutral":    ["calm"],
    "happy":      ["warm", "playful"],
    "sad":        ["melancholy", "nostalgic"],
    "excited":    ["urgent", "playful"],
    "serious":    ["tense", "calm"],
    "question":   ["mystery", "tense"],
    "angry":      ["tense", "urgent"],
    "surprised":  ["revelation", "urgent"],
    "shy":        ["warm", "playful"],
    "troubled":   ["tense", "ominous"],
    "thoughtful": ["nostalgic", "melancholy", "calm"],
}


def load_presets() -> dict:
    if not PRESETS_FILE.exists():
        save_presets(DEFAULT_PRESETS)
        return dict(DEFAULT_PRESETS)
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        changed = False
        for g in GROUPS:
            if g not in data:
                data[g] = DEFAULT_PRESETS[g]
                changed = True
        for g in GROUPS:
            existing_ids = {item.get("id") for item in data.get(g, [])}
            for item in DEFAULT_PRESETS.get(g, []):
                if item["id"] not in existing_ids:
                    data[g].append(item)
                    changed = True
        if changed:
            save_presets(data)
        return data
    except Exception:
        return dict(DEFAULT_PRESETS)


def save_presets(presets: dict) -> None:
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRESETS_FILE.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")


def fragment(group: str, item_id: str) -> str:
    """group内のidに対応する英語プロンプト断片を返す（無ければ空文字）。"""
    if not item_id:
        return ""
    for item in load_presets().get(group, []):
        if item["id"] == item_id:
            return item.get("prompt", "")
    return ""


def label_ja(group: str, item_id: str) -> str:
    if not item_id:
        return ""
    for item in load_presets().get(group, []):
        if item["id"] == item_id:
            return item.get("label_ja", item_id)
    return item_id


def build_background_prompt(
    *, category: str, style_prefix: str,
    spot: str = "", motif: str = "", era: str = "",
    light: str = "", camera: str = "", framing: str = "",
    form: str = "", effect: str = "",
) -> str:
    """構造化入力を1本の英語プロンプトに組み立てる（Docs/BACKGROUND_ARCHIVE.md §8の順序）。

    location: スタイル接頭辞 → framing → 部屋の署名(full/short) → focus(spot|motif|era)
              → light → camera(wide/full_bodyのみ) → 固定フラグメント(location版)
    psych/comic: スタイル接頭辞 → form|effect → 固定フラグメント(抽象版)

    focus は spot/motif/era のうち非空のものを使う（spot と motif は本来排他。
    era は「1点だけ」の運用ルールにより通常は spot/motif のどちらかと併用しない）。
    """
    style_prefix = style_prefix.strip().rstrip(",")
    if category != "location":
        frag = fragment("form", form) if category == "psych" else fragment("effect", effect)
        parts = [style_prefix, frag, TAIL_ABSTRACT]
        return ", ".join(p for p in parts if p)

    # era（現代の携行品）は断片自体が部屋の文脈を内包する自己完結型のため最優先。
    # spot/motifと同時指定された場合はeraが勝つ（「小物は必ず1点だけ」の運用ルール §6）。
    focus = fragment("era", era) or fragment("spot", spot) or fragment("motif", motif)
    framing_id = framing or "bust"
    framing_tmpl = ""
    for item in load_presets().get("framing", []):
        if item["id"] == framing_id:
            framing_tmpl = item.get("prompt", "")
            break
    framing_frag = framing_tmpl.format(focus=focus or "the scene") if framing_tmpl else ""

    sig_kind = SIGNATURE_BY_FRAMING.get(framing_id, "short")
    signature = SIGNATURE_FULL if sig_kind == "full" else SIGNATURE_SHORT

    camera_frag = fragment("camera", camera) if framing_id in CAMERA_FRAMINGS else ""
    light_frag = fragment("light", light)

    tail = TAIL_LOCATION
    if era:
        tail = f"{TAIL_LOCATION}, {TAIL_ERA_TOUCH_SUFFIX}"

    parts = [style_prefix, framing_frag, signature, light_frag, camera_frag, tail]
    return ", ".join(p for p in parts if p)


def slug(*ids: str) -> str:
    """bg_id生成用の短いスラッグ。"""
    return "-".join(i for i in ids if i) or "bg"
