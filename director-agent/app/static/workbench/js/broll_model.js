// 「🎬 A/Bロール」タブの純粋なロジック（DOMを触らない・Nodeで単体テストできる）。Docs/BROLL_PLAN.md §5・B2。
// 箱（scrapping-agent の broll.json）の状態は `GET …/broll` が導出して返す。割り当ては行単位（line_id）で、
// 行の中の秒・範囲は持たない（B-D3）。ここは返ってきた状態を描くための整形と、操作を出す前の下見だけ。
import { thumbOf } from './final_model.js';

/** 取り込み時に選べるライセンス（空欄は不可＝BR4）。[値, 表示名, クレジットが要るか] */
export const LICENSES = [
  ['unknown', '不明（あとで確認する）', false],
  ['own', '自作・自分で撮った', false],
  ['pd', 'パブリックドメイン', false],
  ['cc0', 'CC0', false],
  ['cc-by', 'CC BY（クレジット表記が要る）', true],
  ['cc-by-sa', 'CC BY-SA（クレジット表記が要る）', true],
  ['licensed', '購入・許諾済み', false],
  ['pexels-free', 'Pexels', false],
  ['pixabay-content-license', 'Pixabay', false],
  ['vecteezy-free', 'Vecteezy（無料）', false],
  ['vecteezy-pro', 'Vecteezy（Pro）', false],
  ['ai-generated', 'AI生成', false],
];
export const licenseName = (v) => (LICENSES.find((x) => x[0] === v) || [v, v || '—'])[1];
export const needsCredit = (license) => !!(LICENSES.find((x) => x[0] === license) || [])[2];

export const SOURCE_LABEL = { pexels: 'Pexels', pixabay: 'Pixabay', vecteezy: 'Vecteezy', wikimedia: 'Wikimedia', ai: 'AI生成',
  user: '手元のファイル', motion: 'モーション（自作）', claude: 'Claude が作成' };
export const KIND_LABEL = { photo: '写真', video: '動画', motion: '🎬 モーション' };

/** 素材の見せ方の種類。モーション（HyperFrames・Remotion 製）は動画でも別扱い（作り直せる元がある）。 */
export function kindOf(asset) {
  if (!asset) return 'photo';
  if ((asset.source && asset.source.kind === 'motion') || /_mot$/.test(asset.asset_id || '')) return 'motion';
  return asset.media_type === 'video' ? 'video' : 'photo';
}
export const isVideoLike = (asset) => kindOf(asset) !== 'photo';
export const sourceOf = (asset) => (asset && asset.source && asset.source.kind) || '';

/** 尺の表示（4.5s・1:05）。測れなかった素材は空。 */
export function durationLabel(sec) {
  if (sec === null || sec === undefined || Number.isNaN(Number(sec))) return '';
  const s = Math.round(Number(sec) * 10) / 10;
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.round(s - m * 60)).padStart(2, '0')}`;
}

const enc = encodeURIComponent;
const base = (pid, ep) => `/api/scrapping/projects/${enc(pid)}/episodes/${ep}/broll`;
export const fileName = (asset) => String((asset && asset.file) || '').split('/').pop();
export const fileUrl = (pid, ep, asset) => `${base(pid, ep)}/file/${enc(fileName(asset))}`;
export const thumbUrl = (pid, ep, asset, w = 320) => `${base(pid, ep)}/thumb/${enc(asset.asset_id)}?w=${w}`;

// ── 箱の見え方 ────────────────────────────────────────────

export const EMPTY_BOX = { exists: false, assets: [], placements: {}, orphans: [], trashed: [],
  summary: { assets: 0, lines_with_broll: 0, unassigned: 0, orphaned: 0, license_unknown: 0 } };
export const boxOf = (res) => ({ ...EMPTY_BOX, ...(res || {}), assets: (res && res.assets) || [], placements: (res && res.placements) || {},
  orphans: (res && res.orphans) || [] });

const assetMap = (box) => new Map((box.assets || []).map((a) => [a.asset_id, a]));

/** 行に当たっている素材（並び順どおり・箱に無い ID は読み飛ばす）。 */
export function placedOf(box, lineId) {
  const m = assetMap(box);
  return ((box.placements || {})[lineId] || []).map((it) => ({ ...it, asset: m.get(it.asset_id) })).filter((it) => it.asset);
}
export const placedCount = (box, lineId) => (((box.placements || {})[lineId]) || []).length;

export const FILTERS = [['all', 'すべて'], ['has', 'Bロールあり'], ['none', 'Bロールなし']];
export function rowsFor(lines, box, filter) {
  if (filter === 'has') return lines.filter((l) => placedCount(box, l.id) > 0);
  if (filter === 'none') return lines.filter((l) => placedCount(box, l.id) === 0);
  return lines;
}

/** 上部の帯。行に当たっていない素材・行から外れた割り当て・出典が不明の素材を数える。 */
export function summaryOf(lines, box) {
  const used = new Set();
  let rows = 0;
  for (const l of lines) {
    const n = placedCount(box, l.id);
    if (n) { rows += 1; for (const it of box.placements[l.id]) used.add(it.asset_id); }
  }
  const orphanIds = new Set((box.orphans || []).map((o) => o.asset_id));
  const placedAnywhere = new Set(Object.values(box.placements || {}).flat().map((it) => it.asset_id));
  return {
    lines: lines.length, rows, assets: (box.assets || []).length,
    unassigned: (box.assets || []).filter((a) => !placedAnywhere.has(a.asset_id)).length,
    orphaned: orphanIds.size ? (box.orphans || []).length : 0,
    unknown: (box.assets || []).filter((a) => a.license === 'unknown').length,
    used: used.size,
  };
}

export const DRAWER_KINDS = [['all', 'すべて'], ['photo', '写真'], ['video', '動画'], ['motion', 'モーション']];
/** 引き出しの絞り込み。新しく入れたものが上（added_at の降順）。 */
export function drawerAssets(box, { kind = 'all', source = 'all', unassigned = false } = {}) {
  const placed = new Set(Object.values(box.placements || {}).flat().map((it) => it.asset_id));
  return (box.assets || [])
    .filter((a) => (kind === 'all' || kindOf(a) === kind) && (source === 'all' || sourceOf(a) === source)
      && (!unassigned || !placed.has(a.asset_id)))
    .sort((a, b) => String(b.added_at || '').localeCompare(String(a.added_at || '')));
}
export const sourcesIn = (box) => [...new Set((box.assets || []).map(sourceOf).filter(Boolean))];

/** 行から外れた割り当て（結合・削除で外れたもの）。素材が箱に無い項目は出さない。 */
export function orphanItems(box) {
  const m = assetMap(box);
  return (box.orphans || []).map((o) => ({ ...o, asset: m.get(o.asset_id) })).filter((o) => o.asset);
}

/** この素材が当たっている行（used_by は箱が導出する。無ければ placements から数える）。 */
export function usedBy(box, asset) {
  if (Array.isArray(asset.used_by)) return asset.used_by;
  return Object.entries(box.placements || {}).filter(([, items]) => items.some((it) => it.asset_id === asset.asset_id)).map(([k]) => k);
}

// ── A（完成した絵）の引き方 ─────────────────────────────────

/** A列の絵。納品PNG（`final.export`）があればそれ、無ければ合成のプレビュー、無ければコマ画像（editing-agent と同じ優先順位）。 */
export function aPicture(l) {
  const f = l.final || {};
  if (f.export) return { url: f.export, kind: 'export', label: '納品PNG' };
  const t = thumbOf(l);
  if (t) return { url: t.url, kind: t.composed ? 'preview' : 'panel', label: t.composed ? '合成プレビュー' : 'コマ' };
  return null;
}

// ── 操作の下見 ────────────────────────────────────────────

/** 取り込みの前の検査（サーバーも同じ規則）。理由を返す（空なら進める）。 */
export function uploadBlock({ license, credit }) {
  if (!license) return 'ライセンスを選んでください（空欄は入れられません）';
  if (needsCredit(license) && !String(credit || '').trim()) return `${licenseName(license)} はクレジット（作者・出典）の入力が要ります`;
  return '';
}

/** ドラッグした素材を、行の並びのどこへ置くか（from → to）。範囲外は端に丸める。 */
export function moveIn(ids, from, to) {
  const out = ids.slice();
  if (from < 0 || from >= out.length) return out;
  const [x] = out.splice(from, 1);
  out.splice(Math.max(0, Math.min(out.length, to)), 0, x);
  return out;
}

/** 行モーダルの区画の色（4工程の帯には足さない＝Bロールが無い行は正常）。 */
export function segFor(box, lineId) {
  const n = placedCount(box, lineId);
  return n ? ['ok', `${n}件`] : ['', 'なし'];
}
