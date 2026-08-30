// export_png.jsx — 合成済みPSDを 1920×1080 の PNG として書き出す実行係。
//
// ★なぜ Photoshop で拡大するか（DaVinci ではなく）:
//   バブルとテキストは**ベクター**なので、Image Size なら新しい解像度で
//   描き直される＝無劣化。ラスター拡大しかできない DaVinci 経路では文字が甘くなる。
//   背景はガウスぼかしが乗っているので拡大しても見た目が変わらない。
//
// ⚠️ **マスターPSDは 1376×768 のまま保つ。** 実測した定数（バブル600×450・
//    中心Y=254・INSETS）は全て1376×768基準で、マスターを1920にすると全部合わなくなる。
//    ここは「開く → Image Size → PNG保存 → **保存せずに閉じる**」だけを行う。
//
// ⚠️ アスペクト比が一致しない: 1376:768 = 1.7917 / 1920:1080 = 1.7778。
//    そのまま伸ばすと 0.8% の横歪みが出るので、**高さ基準で拡大してから
//    幅を中央クロップ**する（1376×768 → 1935×1080 → 1920×1080・左右7.5pxずつ）。
//
// 呼び出し側（host-bridge/export_png.py）が先頭に `var JOB = {...};` を注入する。

#target photoshop

function main() {
    var prevUnits = app.preferences.rulerUnits;
    var prevDialogs = app.displayDialogs;
    app.preferences.rulerUnits = Units.PIXELS;
    app.displayDialogs = DialogModes.NO;

    var result = { line_id: JOB.line_id, ok: false };
    var doc = null;
    try {
        var src = new File(JOB.in_psd);
        if (!src.exists) { throw new Error("PSD が無い: " + JOB.in_psd); }
        doc = app.open(src);

        var w0 = doc.width.as("px");
        var h0 = doc.height.as("px");
        result.src_size = w0 + "x" + h0;

        // 高さ基準で拡大 → 幅を中央クロップ（歪ませない）
        var scale = JOB.height / h0;
        var midW = Math.round(w0 * scale);
        // BICUBICSMOOTHER = 拡大向け。ベクター（シェイプ・テキスト）はここで再描画される。
        doc.resizeImage(
            UnitValue(midW, "px"), UnitValue(JOB.height, "px"), doc.resolution,
            ResampleMethod.BICUBICSMOOTHER
        );
        if (midW !== JOB.width) {
            doc.resizeCanvas(
                UnitValue(JOB.width, "px"), UnitValue(JOB.height, "px"),
                AnchorPosition.MIDDLECENTER
            );
        }
        result.out_size = doc.width.as("px") + "x" + doc.height.as("px");

        var f = new File(JOB.out_png);
        f.parent.create();
        var opts = new PNGSaveOptions();
        opts.interlaced = false;
        try { opts.compression = 6; } catch (eC) {}
        doc.saveAs(f, opts, true, Extension.LOWERCASE);

        result.ok = true;
        result.out = JOB.out_png;
    } catch (e) {
        result.error = "" + e + (e.line ? (" @line " + e.line) : "");
    } finally {
        // ★必ず保存せずに閉じる。マスターPSDを 1920 にしてはいけない。
        if (doc !== null) {
            try { doc.close(SaveOptions.DONOTSAVECHANGES); } catch (e2) {}
        }
        app.preferences.rulerUnits = prevUnits;
        app.displayDialogs = prevDialogs;
    }
    return toJson(result);
}

function toJson(o) {
    var parts = [];
    for (var k in o) {
        var v = o[k];
        if (v === null || v === undefined) { parts.push('"' + k + '":null'); }
        else if (typeof v === "boolean" || typeof v === "number") { parts.push('"' + k + '":' + v); }
        else { parts.push('"' + k + '":"' + ("" + v).replace(/\\/g, "\\\\").replace(/"/g, '\\"') + '"'); }
    }
    return "{" + parts.join(",") + "}";
}

main();
