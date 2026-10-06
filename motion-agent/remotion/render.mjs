// 描画の実行役（motion-agent の Python から子プロセスで呼ばれる）。
//   node render.mjs <spec.json>   spec = {mode: "video"|"stills", composition, props, out, frames?, scale?, concurrency?}
//   node render.mjs --bundle       バンドルだけ作る（イメージのビルド時）
// 標準出力に1行1JSONで進捗を出す: {type: "bundling"|"progress"|"still"|"done"|"error", ...}
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {bundle} from '@remotion/bundler';
import {renderMedia, renderStill, selectComposition} from '@remotion/renderer';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const BUILD = path.join(ROOT, 'build');
const STAMP = path.join(BUILD, '.stamp.json');

const emit = (o) => process.stdout.write(JSON.stringify(o) + '\n');

const newestMtime = (dir) => {
  let newest = 0;
  if (!fs.existsSync(dir)) return newest;
  for (const e of fs.readdirSync(dir, {withFileTypes: true})) {
    const p = path.join(dir, e.name);
    newest = Math.max(newest, e.isDirectory() ? newestMtime(p) : fs.statSync(p).mtimeMs);
  }
  return newest;
};

// src/ と public/ が前回のバンドルより新しければ作り直す（dev のホットリロード用）
const ensureBundle = async () => {
  const srcMtime = Math.max(newestMtime(path.join(ROOT, 'src')), newestMtime(path.join(ROOT, 'public')));
  if (fs.existsSync(STAMP)) {
    const s = JSON.parse(fs.readFileSync(STAMP, 'utf8'));
    if (s.src_mtime >= srcMtime) return BUILD;
  }
  emit({type: 'bundling'});
  await bundle({
    entryPoint: path.join(ROOT, 'src', 'index.ts'),
    outDir: BUILD,
    publicDir: path.join(ROOT, 'public'),
  });
  fs.writeFileSync(STAMP, JSON.stringify({src_mtime: srcMtime, bundled_at: new Date().toISOString()}));
  return BUILD;
};

const main = async () => {
  if (process.argv[2] === '--bundle') {
    await ensureBundle();
    emit({type: 'done'});
    return;
  }
  const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const serveUrl = await ensureBundle();
  const inputProps = JSON.parse(fs.readFileSync(spec.props, 'utf8'));
  const composition = await selectComposition({serveUrl, id: spec.composition, inputProps});

  if (spec.mode === 'video') {
    let last = -1;
    await renderMedia({
      composition,
      serveUrl,
      codec: 'h264',
      outputLocation: spec.out,
      inputProps,
      concurrency: spec.concurrency ?? 4,
      muted: true,
      onProgress: ({progress}) => {
        const pct = Math.floor(progress * 50) * 2; // 2%刻み
        if (pct !== last) {
          last = pct;
          emit({type: 'progress', progress: pct / 100});
        }
      },
    });
    emit({type: 'done', out: spec.out, frames: composition.durationInFrames});
  } else if (spec.mode === 'stills') {
    fs.mkdirSync(spec.out, {recursive: true});
    for (const f of spec.frames) {
      const out = path.join(spec.out, `${f.name}.png`);
      await renderStill({composition, serveUrl, output: out, frame: f.frame, inputProps, imageFormat: 'png', scale: spec.scale ?? 1});
      emit({type: 'still', name: f.name, frame: f.frame, out});
    }
    emit({type: 'done'});
  } else {
    throw new Error(`unknown mode: ${spec.mode}`);
  }
};

main().catch((e) => {
  emit({type: 'error', message: String(e?.stack ?? e)});
  process.exit(1);
});
