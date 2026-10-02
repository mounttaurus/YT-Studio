// 絵タブ（Aロール）の純粋なロジック（DOMを触らない・Nodeで単体テストできる）。
// Docs/LINE_WORKBENCH_PLAN.md §5-4-1（W4b-1）。状態は director の GET .../workbench の `lines[].aroll` が持つ。
// ここは「絞り込み・選択・課金の枚数・行モーダルの選択肢」を返す整形だけ。

export const AROLL_FILTERS = [['all', 'すべて'], ['ungenerated', '未生成'], ['unapproved', '未確認'],
  ['restale', '♻️要組み直し'], ['narration', 'ナレーション'], ['drift', '台本とズレ']];

export const hasPanel = (l) => !!(l.aroll && l.aroll.panel);

/** 絵の下ごしらえが要る行（プロンプト・コマが無い）。「プロンプトの無い行を下ごしらえ」の対象。 */
export const needsPrep = (l) => !!l.aroll && l.aroll.has_manifest && (!l.aroll.panel || !l.aroll.has_prompt) && !!(l.text || '').trim();

const PRED = {
  ungenerated: (l) => hasPanel(l) && l.aroll.status !== 'done' && !l.aroll.cutout_slot_id,
  // 生成済み・絵はあるが人がまだ「この絵でOK」を押していない
  unapproved: (l) => hasPanel(l) && needsReview(l.aroll),
  restale: (l) => hasPanel(l) && !!l.aroll.restale,
  narration: (l) => hasPanel(l) && !(l.aroll.characters || []).length,
  // コマが無い行も「台本とズレ」で見つけられるようにする（台本にあって絵の側に無い）
  drift: (l) => !!l.aroll && l.aroll.has_manifest && (!l.aroll.panel || (!!l.aroll.sync && l.aroll.sync !== 'ok')),
};

export function rowsFor(lines, f) {
  return f === 'all' || !PRED[f] ? lines : lines.filter(PRED[f]);
}
export const filterCount = (lines, f) => rowsFor(lines, f).length;

// ── 行の選択（絵タブ・仕上がりタブ共通）─────────────────────────────
// ⚠️ 一括操作は選択0件では押せない。`line_ids: []` を「全件」にしない（サーバーも空は対象ゼロ）。

/** 選択中の行のうち、コマがあるものだけを台本順に。 */
export const selectedIds = (lines, sel) => lines.filter((l) => sel.has(l.id) && hasPanel(l)).map((l) => l.id);

/** 見えている行だけを全選択／全解除する（見えない行は巻き込まない）。 */
export function toggleAll(sel, visible, on) {
  const next = new Set(sel);
  for (const l of visible) if (hasPanel(l)) (on ? next.add(l.id) : next.delete(l.id));
  return next;
}
export const allChecked = (visible, sel) => {
  const v = visible.filter(hasPanel);
  return v.length > 0 && v.every((l) => sel.has(l.id));
};

// ── 課金の枚数（カット単位）────────────────────────────────────────
// ⚠️ バックエンドの select_targets はカットの先頭行だけを対象にし、兄弟行は無料で埋まる（1カット1枚）。
// 行数のまま数えると実際の課金枚数より多く見せてしまう。

/** カット（同じ絵を共有する行のまとまり）を台本順に復元する。カット情報が無い行は1行1カット。 */
export function cutGroups(lines) {
  const groups = [];
  for (const l of lines.filter(hasPanel)) {
    const c = l.aroll.cut;
    if (c && c.index > 0 && groups.length) groups[groups.length - 1].push(l);
    else groups.push([l]);
  }
  return groups;
}

/** 生成の対象枚数。target: missing（未生成のみ）／failed（失敗のみ）／all（全行）。 */
export function batchTargetCount(lines, target) {
  let n = 0;
  for (const g of cutGroups(lines)) {
    const head = g[0].aroll;
    if (!head.has_prompt || !(head.characters || []).length) continue;
    if (target === 'failed') { if (g.some((m) => m.aroll.status === 'failed')) n++; }
    else if (target === 'all') n++;
    else if (!g.every((m) => m.aroll.status === 'done' || m.aroll.cutout_slot_id)) n++;
  }
  return n;
}

/** 在庫で賄える分（カット単位）を引いた課金枚数。試算前・対象が「未生成のみ」以外は null。 */
export function billableCount(lines, target, plan) {
  if (!plan || target !== 'missing') return null;
  return Math.max(0, batchTargetCount(lines, 'missing') - (plan.from_stock_cuts || 0));
}

/** 在庫で賄えない理由の内訳（多い順）。 */
export function planReasons(plan) {
  const m = {};
  for (const l of (plan && plan.lines) || []) {
    if (l.slot_id || l.decided) continue;
    const k = (l.reason || '').split('（')[0];
    m[k] = (m[k] || 0) + 1;
  }
  return Object.entries(m).sort((a, b) => b[1] - a[1]);
}

export const COST_PER_IMAGE = 0.04;
export const usd = (n) => (n * COST_PER_IMAGE).toFixed(2);

// ── 表示の整形 ───────────────────────────────────────────────────
export const SYNC_LABEL = { ok: '✔ 一致', stale: '⚠ 台本とズレ', missing: '✘ 画像なし', orphan: '🗑 行が消えた', unknown: '? 記録なし' };
export const SYNC_CLASS = { ok: 'ok', stale: 'warn', missing: 'bad', orphan: '', unknown: 'info' };

/** 絵がある行（自前の画像ができた、または在庫の絵を指している）。「この絵でOK」の対象になる。 */
export const hasPicture = (a) => !!a && ((a.status === 'done' && !!a.has_image) || !!a.cutout_slot_id);

/** 人が見て確認するべき絵（未確認）。生成した絵（自前の画像）と、台本とズレた行。
 *  在庫の絵はシステムがルールで選んだ時点で採用済みとみなし、印を出さない（150行ほぼ全部に付くと意味が無いため）。
 *  承認そのもの（「この絵でOK」）は在庫の絵にも押せる。何かの条件になってはいない（書き出し・組版・確定は見ない）。 */
export const needsReview = (a) => hasPicture(a) && !a.approved && ((a.status === 'done' && !!a.has_image) || a.sync === 'stale');

/** 一覧の状態バッジ [クラス, 文言][]（生成・確定・同期・要組み直し・在庫・背景）。 */
export function statusChips(l) {
  const a = l.aroll;
  if (!a || !a.has_manifest) return [];
  if (!a.panel) return [['bad', 'コマが無い']];
  // 在庫の絵を指している行は、自前の画像が無くても絵はある（「未生成」とは言わない。下の「✂️ 在庫」が出る）
  const out = a.status === 'done' ? [['ok', '✔ 生成済']] : a.status === 'failed' ? [['bad', '✘ 失敗']] : a.cutout_slot_id ? [] : [['', '未生成']];
  if (hasPicture(a)) { if (a.approved) out.push(['ok', '✓ OK済']); else if (needsReview(a)) out.push(['warn', '未確認']); }
  if (a.sync && a.sync !== 'ok' && a.sync !== 'missing') out.push([SYNC_CLASS[a.sync] || '', SYNC_LABEL[a.sync] || a.sync]);
  if (a.restale) out.push(['purple', '♻️ 再合成が要る']);
  if (a.cutout_slot_id) out.push(['cyan', '✂️ 在庫']);
  if (a.background_id) out.push(['', '🏞️ 背景あり']);
  return out;
}

/** プリセットの語彙 id → 日本語ラベル。 */
export const presetLabel = (presets, axis, id) => !id ? '' : ((presets || {})[axis] || []).find((o) => o.id === id)?.label_ja || id;

/** 実際に使われている絵のタグ（希望ラベルではない）。在庫の絵だけ。 */
export function usedSlotSummary(a, presets) {
  if (!a || !a.cutout_slot_id) return '';
  const u = a.used_slot;
  if (!u) return '（在庫の情報を取得できません）';
  const parts = [['emotion', '表情'], ['shot', 'ショット'], ['angle', 'アングル'], ['pose', 'ポーズ']]
    .map(([k, label]) => (u[k] ? `${label}:${presetLabel(presets, k, u[k])}` : null)).filter(Boolean);
  return parts.join(' / ') || '（タグ無し）';
}

/** 「前の絵を使う」の状態。'join'＝押すと前の行につなげる／'split'＝押すと別の絵にする／null＝最初の行など。 */
export function cutAction(l, lines) {
  if (!hasPanel(l)) return null;
  const i = lines.indexOf(l);
  if (l.aroll.cut && l.aroll.cut.index > 0) return 'split';
  return i > 0 ? 'join' : null;
}

/** 声の感情と同じ語彙が絵の表情にあるか（あれば「絵の表情を声に合わせる」が使える）。 */
export const voiceEmotionInPresets = (l, presets) => {
  const e = l.emotion || 'neutral';
  return ((presets || {}).emotion || []).some((o) => o.id === e) ? e : '';
};

/** 2人目に足せるキャラ（uses_images が false のキャラは出さない・すでにいる人は除く）。 */
export const addableChars = (allChars, a) =>
  (allChars || []).filter((c) => c.uses_images !== false && !(a.characters || []).some((x) => x.id === c.char_id));

// ── 背景ピッカー ────────────────────────────────────────────────
// framing は location の背景にだけ意味を持つ軸（psych/comic 等は常に空）。全件取得して location にだけ適用する。
export function bgFiltered(items, cat, framing) {
  return (items || []).filter((b) => {
    if (cat !== 'all' && b.category !== cat) return false;
    if (b.category === 'location' && framing && b.framing !== framing) return false;
    return true;
  });
}
export function bgFacets(items) {
  const m = new Map();
  for (const b of items || []) if (b.category) m.set(b.category, (m.get(b.category) || 0) + 1);
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
}

/** 在庫ピッカーの向きの1文字（facing 未設定は正面）。 */
export const FACING_GLYPH = { front: '正', left_3q: '↖', left_profile: '←', right_3q: '↗', right_profile: '→', back: '背' };
export const facingGlyph = (e) => FACING_GLYPH[e.facing || 'front'] || '正';

/** 生成バッチの進捗（%）。 */
export const jobPercent = (job) => (job && job.total ? Math.round(((job.done + job.failed) / job.total) * 100) : 0);

/** 生成バッチの見守り。running＝サーバーが動いている。 */
export function arollPollDecision({ running, wasRunning }) {
  if (running) return 'continue';
  return wasRunning ? 'finished' : 'idle';
}

/** 2つのセリフの違い（共通の頭・尻を除いた真ん中）。変わった語句を強調するための単純な比較。 */
export function diffParts(before, after) {
  const a = before || '', b = after || '';
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  let j = 0;
  while (j < a.length - i && j < b.length - i && a[a.length - 1 - j] === b[b.length - 1 - j]) j++;
  return { pre: a.slice(0, i), before: a.slice(i, a.length - j), after: b.slice(i, b.length - j), suf: a.slice(a.length - j) };
}
