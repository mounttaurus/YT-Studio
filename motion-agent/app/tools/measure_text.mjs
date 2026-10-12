// 描いた時の文字の枠を実測して、字幕の帯・安全域に掛かるものを拾う（Docs/MOTION_UNIFIED_PLAN.md §8-1・§8-2・I1）。
//
//   node measure_text.mjs <コンポジションのフォルダ> <HF_DIR> [--samples 16] [--band 0.2] [--safe 0.9] [--timeout 20000]
//
// HyperFrames の `check` が使うブラウザの口（runBrowserCheck）を、固定した版（0.8.140）の dist から借りる。
// 公式の `check` は帯を1つしか見られず、画面の端の余白（安全域）を見ないので、同じ口で文字の枠だけを自前で測る。
// 結果は標準出力の最後に `@@RESULT@@ {json}` の1行で返す（他のログと混ざっても拾える）。
import { readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const args = process.argv.slice(2);
const dir = resolve(args[0] || ".");
const hfDir = resolve(args[1] || "/app/hyperframes");
const opt = { samples: 16, band: 0.2, safe: 0.9, timeout: 20000, tol: 2 };
for (let i = 2; i < args.length; i += 2) {
  const k = (args[i] || "").replace(/^--/, "");
  if (k in opt) opt[k] = Number(args[i + 1]);
}

const dist = join(hfDir, "node_modules", "hyperframes", "dist");
const chunk = readdirSync(dist).find((f) => /^checkBrowser-.*\.js$/.test(f));
if (!chunk) {
  console.log("@@RESULT@@ " + JSON.stringify({ ok: false, error: "checkBrowser の dist が見つからない", findings: [] }));
  process.exit(0);
}
const { runBrowserCheck } = await import(pathToFileURL(join(dist, chunk)).href);

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
function box(r) {
  const x = num(r.x) ?? num(r.left) ?? 0;
  const y = num(r.y) ?? num(r.top) ?? 0;
  const w = num(r.width) ?? Math.max(0, (num(r.right) ?? x) - x);
  const h = num(r.height) ?? Math.max(0, (num(r.bottom) ?? y) - y);
  return { x0: x, y0: y, x1: x + w, y1: y + h, w, h };
}

const found = new Map();   // selector+code → 最初に見つかったもの（最悪の食い込みに更新）
let meta = { canvas: null, duration: null, samples: 0, texts: 0 };

async function runGrid(driver) {
  await driver.initialize(false);   // 監査用のスクリプトをページへ入れる（コントラストは見ない）
  const canvas = await driver.getCanvas();
  const duration = (await driver.getDuration()) || 0;
  const W = canvas.width;
  const H = canvas.height;
  const bandTop = H * (1 - opt.band);
  const mx = (W * (1 - opt.safe)) / 2;
  const my = (H * (1 - opt.safe)) / 2;
  const times = [];
  const n = Math.max(1, Math.round(opt.samples));
  for (let i = 0; i < n; i++) times.push(Math.min(duration - 1e-3, ((i + 0.5) / n) * duration));
  if (duration > 0.2) times.push(Math.max(0, duration - 0.05));   // 終わり際（最後に落ち着いた位置）
  meta = { canvas, duration, samples: times.length, texts: 0 };
  for (const t of times) {
    await driver.seek(t);
    const cands = await driver.collectGeometryCandidates(t, { text: true, media: false });
    for (const c of cands) {
      if (c.kind !== "text") continue;
      meta.texts += 1;
      const b = box(c.rect);
      if (b.w <= 0 || b.h <= 0) continue;
      const attrs = c.dataAttributes || {};
      const label = (c.text || "").trim().slice(0, 40);
      const put = (code, over, message) => {
        const key = c.selector + "|" + code;
        const prev = found.get(key);
        if (!prev || over > prev.over) {
          found.set(key, { code, level: "error", selector: c.selector, text: label, time: Math.round(t * 1000) / 1000,
                           over: Math.round(over), rect: { x: Math.round(b.x0), y: Math.round(b.y0), w: Math.round(b.w), h: Math.round(b.h) },
                           source: c.sourceFile, message, hits: (prev ? prev.hits : 0) + 1 });
        } else {
          prev.hits += 1;
        }
      };
      // 字幕の帯（下から band の割合）。DaVinci の字幕が乗るので、文字を置かない（I-D2）
      if (opt.band > 0 && b.y1 > bandTop + opt.tol) {
        put("subtitle_band", b.y1 - bandTop,
            `文字「${label}」の枠（下端 y=${Math.round(b.y1)}）が字幕の帯（y=${Math.round(bandTop)} より下）に掛かる`);
      }
      // 安全域（画面の safe の割合の枠）。上・左・右。下は帯が受け持つ
      if (opt.safe > 0 && !("data-layout-allow-overflow" in attrs)) {
        const left = mx - b.x0;
        const right = b.x1 - (W - mx);
        const top = my - b.y0;
        const bottom = opt.band > 0 ? -Infinity : b.y1 - (H - my);   // 帯を見る用途では下は帯が受け持つ
        const worst = Math.max(left, right, top, bottom);
        if (worst > opt.tol) {
          const side = worst === left ? "左" : worst === right ? "右" : worst === top ? "上" : "下";
          put("safe_area", worst, `文字「${label}」の枠が安全域（画面の${Math.round(opt.safe * 100)}%）の${side}に ${Math.round(worst)}px はみ出す`);
        }
      }
    }
  }
  return {};
}

try {
  await runBrowserCheck({ dir }, { timeout: opt.timeout, autoProxy: false }, null, runGrid);
  console.log("@@RESULT@@ " + JSON.stringify({ ok: true, ...meta, band: opt.band, safe: opt.safe, findings: [...found.values()] }));
} catch (e) {
  console.log("@@RESULT@@ " + JSON.stringify({ ok: false, error: String(e && e.message ? e.message : e).slice(0, 600), ...meta, findings: [] }));
}
process.exit(0);
