// build_panel.jsx — panel_plan.json の1エントリを組み立てるだけの実行係。
//
// ★設計方針: ここに判断ロジックを置かない。全ての値は PLAN から来る。
//   Photoshop 内でのデバッグは高コストなので、考えるのは Python 側の役目。
//   唯一の例外が「テキストの実測フィット」で、これは実描画を測らないと
//   決められないため（psd-layout-has-no-rule の教訓）ここで行う。
//
// 呼び出し側（bridge.py）が先頭に `var PLAN = {...};` を注入する。

#target photoshop

function main() {
    var prevUnits = app.preferences.rulerUnits;
    var prevDialogs = app.displayDialogs;
    app.preferences.rulerUnits = Units.PIXELS;
    app.displayDialogs = DialogModes.NO;

    var result = { line_id: PLAN.line_id, ok: false, warnings: [] };
    var doc = null;
    try {
        doc = app.documents.add(
            PLAN.canvas[0], PLAN.canvas[1], 72, "panel_" + PLAN.line_id,
            NewDocumentMode.RGB, DocumentFill.TRANSPARENT
        );
        // 新規ドキュメントに必ず1枚できる空レイヤー。全部 duplicate で持ち込むので
        // 使わない。最後に消す（残すとレイヤーパネルに謎の "Layer 1" が居座る）。
        var placeholder = doc.activeLayer;

        // 重ね順（下→上）: 背景 → 色調整 → eff(集中線) → キャラ → バブル → 文字
        // eff_ は「暗い線＋透明背景」の重ね素材で、キャラの後ろ・背景の前に置く。
        placeBackground(doc, result, false);
        addNeutralAdjustment(doc, result);
        placeBackground(doc, result, true);
        placeCharacter(doc, result);
        var bubble = placeBubble(doc, result);
        var fit = placeText(doc, bubble, result);
        result.font_size = fit.size;
        result.lines = fit.lines;

        try { placeholder.remove(); } catch (eP) {}

        savePsd(doc, PLAN.out_psd);
        result.ok = true;
        result.out = PLAN.out_psd;
    } catch (e) {
        result.error = "" + e + (e.line ? (" @line " + e.line) : "");
    } finally {
        if (doc !== null) {
            try { doc.close(SaveOptions.DONOTSAVECHANGES); } catch (e2) {}
        }
        app.preferences.rulerUnits = prevUnits;
        app.displayDialogs = prevDialogs;
    }
    return toJson(result);
}

// ------------------------------------------------------------ 背景
function placeBackground(doc, result, wantOverlay) {
    var bg = PLAN.background;
    if (!bg) { if (!wantOverlay) { result.warnings.push("NO_BACKGROUND"); } return; }
    // 候補が並んでいれば全部置く。第1候補だけ表示し、残りは非表示で控えさせる
    // ＝ユーザーは目玉を切り替えるだけで差し替えられる（重ね使いも可）。
    var all = (bg.candidates && bg.candidates.length) ? bg.candidates
            : (bg.image ? [{ bg_id: bg.bg_id, image: bg.image, blur: bg.blur, visible: true }] : []);
    // wantOverlay=true なら eff_（集中線）だけ、false ならそれ以外だけを置く
    var list = [];
    for (var k = 0; k < all.length; k++) {
        if (!!all[k].overlay === !!wantOverlay) { list.push(all[k]); }
    }
    if (!list.length) {
        if (!wantOverlay) { result.warnings.push("NO_BACKGROUND"); }
        return;
    }

    var placed = 0;
    // 逆順に置く＝第1候補が最前面（＝レイヤーパネルの一番上）に来る
    for (var i = list.length - 1; i >= 0; i--) {
        var c = list[i];
        var f = new File(c.image);
        if (!f.exists) { continue; }
        var layer = importImage(doc, f, c.bg_id ? c.bg_id : "background");
        fitToCanvas(doc, layer);
        // スマートオブジェクト化してからブラー＝スマートフィルターになる。
        // ユーザーは強度バーを動かすだけで済む（これが本機能の主目的）。
        doc.activeLayer = layer;
        executeAction(stringIDToTypeID("newPlacedLayer"), undefined, DialogModes.NO);
        var so = doc.activeLayer;
        so.name = c.bg_id ? c.bg_id : "background";
        if (c.blur > 0) { so.applyGaussianBlur(c.blur); }
        if (typeof c.opacity === "number" && c.opacity !== 100) { so.opacity = c.opacity; }
        if (c.blend_mode === "multiply") { so.blendMode = BlendMode.MULTIPLY; }
        else if (c.blend_mode === "screen") { so.blendMode = BlendMode.SCREEN; }
        else if (c.blend_mode === "overlay") { so.blendMode = BlendMode.OVERLAY; }
        so.visible = (c.visible === true);
        placed++;
    }
    if (!placed && !wantOverlay) { result.warnings.push("BACKGROUND_FILE_MISSING"); }
    if (wantOverlay) { result.eff_layers = placed; }
    else { result.bg_candidates = placed; }
}

// ------------------------------------------------------------ 色調整（中立）
function addNeutralAdjustment(doc, result) {
    if (!PLAN.background || !PLAN.background.add_neutral_adjustment) { return; }
    var d = new ActionDescriptor();
    var ref = new ActionReference();
    ref.putClass(stringIDToTypeID("adjustmentLayer"));
    d.putReference(stringIDToTypeID("target"), ref);
    var using = new ActionDescriptor();
    var type = new ActionDescriptor();
    type.putBoolean(stringIDToTypeID("colorize"), false);
    using.putObject(stringIDToTypeID("type"), stringIDToTypeID("hueSaturation"), type);
    d.putObject(stringIDToTypeID("using"), stringIDToTypeID("adjustmentLayer"), using);
    executeAction(stringIDToTypeID("make"), d, DialogModes.NO);
    doc.activeLayer.name = "adjust";
}

// ------------------------------------------------------------ キャラ
function placeCharacter(doc, result) {
    var ch = PLAN.character;
    var f = new File(ch.image_abs);
    if (!f.exists) { result.warnings.push("CHARACTER_FILE_MISSING"); return; }
    var layer = importImage(doc, f, "Layer 0");
    // 縮小はバブルと反対側の下角を基点にする＝画面下端への接地を保ったまま、
    // バブル側の端だけが引っ込む（ユーザーの手作業と同じ考え方）。
    if (ch.scale && ch.scale !== 1) {
        var anchor = (PLAN.bubble.side === "left")
            ? AnchorPosition.BOTTOMRIGHT : AnchorPosition.BOTTOMLEFT;
        layer.resize(ch.scale * 100, ch.scale * 100, anchor);
        result.scaled = ch.scale;
    }
    if (ch.offset && (ch.offset[0] !== 0 || ch.offset[1] !== 0)) {
        layer.translate(ch.offset[0], ch.offset[1]);
    }
}

// ------------------------------------------------------------ バブル
function placeBubble(doc, result) {
    var b = PLAN.bubble;
    var src = app.open(new File(PLAN.bubbles_psd));
    var found = null;
    for (var i = 0; i < src.artLayers.length; i++) {
        if (src.artLayers[i].name === b.layer) { found = src.artLayers[i]; break; }
    }
    if (found === null) {
        src.close(SaveOptions.DONOTSAVECHANGES);
        throw new Error("bubbles.psd にレイヤーが無い: " + b.layer);
    }
    src.activeLayer = found;
    var dup = found.duplicate(doc, ElementPlacement.PLACEATBEGINNING);
    src.close(SaveOptions.DONOTSAVECHANGES);

    doc.activeLayer = dup;
    dup.name = "bubble_" + b.layer;

    // 目標矩形へ合わせる（ベクターなので無劣化）
    var want_w = b.rect[2] - b.rect[0];
    var want_h = b.rect[3] - b.rect[1];
    var bd = dup.bounds;
    var cur_w = bd[2].as("px") - bd[0].as("px");
    var cur_h = bd[3].as("px") - bd[1].as("px");
    if (cur_w > 0 && cur_h > 0) {
        dup.resize(want_w / cur_w * 100, want_h / cur_h * 100, AnchorPosition.MIDDLECENTER);
    }
    if (b.flip_h) { dup.resize(-100, 100, AnchorPosition.MIDDLECENTER); }
    if (b.flip_v) { dup.resize(100, -100, AnchorPosition.MIDDLECENTER); }

    bd = dup.bounds;
    dup.translate(b.rect[0] - bd[0].as("px"), b.rect[1] - bd[1].as("px"));
    return dup;
}

// ------------------------------------------------------------ テキスト（実測フィット）
function placeText(doc, bubbleLayer, result) {
    var t = PLAN.text;
    var box = t.box; // [x0, y0, x1, y1] バブル内側の可用矩形
    var boxW = box[2] - box[0];
    var boxH = box[3] - box[1];

    var layer = doc.artLayers.add();
    layer.kind = LayerKind.TEXT;
    layer.name = "text";
    var ti = layer.textItem;
    ti.kind = TextType.POINTTEXT;
    ti.font = t.font;
    ti.justification = Justification.LEFT;
    var c = new SolidColor();
    c.rgb.red = t.color[0]; c.rgb.green = t.color[1]; c.rgb.blue = t.color[2];
    ti.color = c;
    ti.contents = t.lines.join("\r");

    // 実描画を測って収める。予測式は使わない（手作業PSDに規則は無かった）。
    var size = t.size;
    var fitted = false;
    for (var i = 0; i < PLAN.fit.max_iter; i++) {
        ti.size = new UnitValue(size, "px");
        ti.position = [new UnitValue(box[0], "px"), new UnitValue(box[1] + size, "px")];
        var b = layer.bounds;
        var w = b[2].as("px") - b[0].as("px");
        var h = b[3].as("px") - b[1].as("px");
        if (w <= boxW && h <= boxH) { fitted = true; break; }
        size -= PLAN.fit.step;
        if (size < PLAN.fit.min) { size = PLAN.fit.min; ti.size = new UnitValue(size, "px"); break; }
    }
    if (!fitted) { result.warnings.push("TEXT_OVERFLOW"); }

    // バブルに対して水平中央・やや上寄せ（129枚の実測: dx≈0 / dy≈-3〜-50）
    var bb = layer.bounds;
    var tw = bb[2].as("px") - bb[0].as("px");
    var th = bb[3].as("px") - bb[1].as("px");
    layer.translate(
        (box[0] + boxW / 2) - (bb[0].as("px") + tw / 2),
        (box[1] + boxH / 2) - (bb[1].as("px") + th / 2)
    );
    return { size: size, lines: t.lines.length, fitted: fitted };
}

// ------------------------------------------------------------ 補助
function importImage(doc, file, name) {
    // ⚠️ copy/paste は使わない。**doc.paste() は内容をキャンバス中央に置く**ため、
    //    元画像の座標が失われる（実害: 抜き済みキャラが 704px → 397px へ約307px
    //    左へずれ、吹き出しに被った。2026-08-23 に発覚）。
    //    duplicate() はドキュメント座標を保つので、全面PNGの位置がそのまま残る。
    var src = app.open(file);
    var layer;
    try {
        var sl = src.activeLayer;
        if (sl.isBackgroundLayer) { sl.isBackgroundLayer = false; }
        layer = sl.duplicate(doc, ElementPlacement.PLACEATBEGINNING);
    } finally {
        src.close(SaveOptions.DONOTSAVECHANGES);
    }
    doc.activeLayer = layer;
    layer.name = name;
    return layer;
}

function fitToCanvas(doc, layer) {
    var b = layer.bounds;
    var w = b[2].as("px") - b[0].as("px");
    var h = b[3].as("px") - b[1].as("px");
    if (w <= 0 || h <= 0) { return; }
    var s = Math.max(doc.width.as("px") / w, doc.height.as("px") / h) * 100;
    if (s !== 100) { layer.resize(s, s, AnchorPosition.MIDDLECENTER); }
    b = layer.bounds;
    layer.translate(
        doc.width.as("px") / 2 - (b[0].as("px") + (b[2].as("px") - b[0].as("px")) / 2),
        doc.height.as("px") / 2 - (b[1].as("px") + (b[3].as("px") - b[1].as("px")) / 2)
    );
}

function savePsd(doc, path) {
    var f = new File(path);
    f.parent.create();
    var opts = new PhotoshopSaveOptions();
    opts.embedColorProfile = true;
    opts.layers = true;
    doc.saveAs(f, opts, true, Extension.LOWERCASE);
}

function toJson(o) {
    var parts = [];
    for (var k in o) {
        var v = o[k];
        if (v === null || v === undefined) { parts.push('"' + k + '":null'); }
        else if (typeof v === "boolean" || typeof v === "number") { parts.push('"' + k + '":' + v); }
        else if (v instanceof Array) { parts.push('"' + k + '":["' + v.join('","') + '"]'); }
        else { parts.push('"' + k + '":"' + ("" + v).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"'); }
    }
    return "{" + parts.join(",") + "}";
}

main();
