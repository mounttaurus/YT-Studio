// 絵タブ（Aロール）の純粋なロジック（DOMを触らない・Nodeで単体テストできる）。
// Docs/LINE_WORKBENCH_PLAN.md §5-4-1（W4b-1）。状態は director の GET .../workbench の `lines[].aroll` が持つ。
// ここは「絞り込み・選択・課金の枚数・行モーダルの選択肢」を返す整形だけ。

export const AROLL_FILTERS = [['all', 'すべて'], ['ungenerated', '未生成'], ['unapproved', '未確認'],
  ['restale', '🔧要合成'], ['dup', '🔁重複'], ['narration', 'ナレーション'], ['drift', '台本とズレ']];

export const hasPanel = (l) => !!(l.aroll && l.aroll.panel);

/** 絵の下ごしらえが要る行（プロンプト・コマが無い）。「プロンプトの無い行を下ごしらえ」の対象。 */
export const needsPrep = (l) => !!l.aroll && l.aroll.has_manifest && (!l.aroll.panel || !l.aroll.has_prompt) && !!(l.text || '').trim();

// 述語は (行, 重複の索引) を受ける。重複の索引は dupMap の結果（検査していなければ undefined＝該当なし）
const PRED = {
  ungenerated: (l) => hasPanel(l) && l.aroll.status !== 'done' && !l.aroll.cutout_slot_id,
  // 生成済み・絵はあるが人がまだ「この絵でOK」を押していない
  unapproved: (l) => hasPanel(l) && needsReview(l.aroll),
  restale: (l) => hasPanel(l) && !!l.aroll.restale,
  dup: (l, dups) => !!dups && dups.has(l.id),
  narration: (l) => hasPanel(l) && !(l.aroll.characters || []).length,
  // コマが無い行も「台本とズレ」で見つけられるようにする（台本にあって絵の側に無い）
  drift: (l) => !!l.aroll && l.aroll.has_manifest && (!l.aroll.panel || (!!l.aroll.sync && l.aroll.sync !== 'ok')),
};

export function rowsFor(lines, f, dups) {
  return f === 'all' || !PRED[f] ? lines : lines.filter((l) => PRED[f](l, dups));
}
export const filterCount = (lines, f, dups) => rowsFor(lines, f, dups).length;

// ── 同じ絵の繰り返し（Docs/AROLL_DUPLICATE_CHECK_PLAN.md D3）──────────────────────
// 検査の本体は scrapping-agent（director の GET .../aroll-duplicates が ✋手直し済みを除いて中継）。
// items[] は「直す対象の行（のまとまり）」: {line_id, line_ids, kind: exact|near, with:[{line_id,kind,distance?}]}。

/** 行ID → 指摘の索引（指摘の行のまとまりに含まれる行すべて）。検査前・指摘なしは null。 */
export function dupMap(report) {
  const items = (report && report.items) || [];
  if (!items.length) return null;
  const m = new Map();
  for (const it of items) for (const id of it.line_ids || [it.line_id]) m.set(id, it);
  return m;
}

/** 一覧の行のチップ [クラス, 文言, 説明]。同じ絵は黄・似た絵は青。指摘が無い行は null。 */
export function dupChip(item) {
  if (!item) return null;
  const who = (item.with || []).map((w) => w.line_id).join('・');
  return item.kind === 'exact'
    ? ['warn', '🔁 重複', `この話数の中で同じ絵が ${who} にも使われています`]
    : ['info', '🔁 似た絵', `近い行の絵（${who}）とよく似ています`];
}

/** 「同じ絵 N行・似た絵 M行」。 */
export function dupSummaryText(report) {
  const s = (report && report.summary) || {};
  const parts = [];
  if (s.exact) parts.push(`同じ絵 ${s.exact}行`);
  if (s.near) parts.push(`似た絵 ${s.near}行`);
  return parts.join('・');
}

/** 直しの案（POST .../fix の dry-run）から、ボタンに出す数と概算。 */
export function dupFixCounts(plan) {
  const s = (plan && plan.summary) || {};
  return { reselect: s.reselect || 0, generate: s.generate || 0, skip: s.skip || 0, keep: s.keep || 0,
    cost: s.estimated_cost_usd || 0 };
}

/** 直前の「重複を直した」で、まだ戻していないもの（戻すボタンの出し分け）。 */
export const lastDupFix = (report) => {
  const f = ((report && report.fixes) || []).slice(-1)[0];
  return f && !f.undone ? f : null;
};

/** この行の案の1行（モーダルの「この行だけ直す」の可否に使う）。無ければ null。 */
export const dupPlanFor = (plan, lineId) => ((plan && plan.plan) || []).find((a) => (a.line_ids || [a.line_id]).includes(lineId)) || null;

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

/** 一覧の状態バッジ [クラス, 文言][]（生成・確定・同期・要合成・在庫・背景）。 */
export function statusChips(l) {
  const a = l.aroll;
  if (!a || !a.has_manifest) return [];
  if (!a.panel) return [['bad', 'コマが無い']];
  // 在庫の絵を指している行は、自前の画像が無くても絵はある（「未生成」とは言わない。下の「✂️ 在庫」が出る）
  const out = a.status === 'done' ? [['ok', '✔ 生成済']] : a.status === 'failed' ? [['bad', '✘ 失敗']] : a.cutout_slot_id ? [] : [['', '未生成']];
  if (hasPicture(a)) { if (a.approved) out.push(['ok', '✓ OK済']); else if (needsReview(a)) out.push(['warn', '未確認']); }
  if (a.sync && a.sync !== 'ok' && a.sync !== 'missing') out.push([SYNC_CLASS[a.sync] || '', SYNC_LABEL[a.sync] || a.sync]);
  if (a.restale) out.push(['purple', '🔧 要合成']);
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

// ── 吹き出しの形（行ごとの上書き・Docs/BUBBLE_CHOICE_PLAN.md B3）──────────────
// 選べるのは横の7種だけ（縦の rect_v/round_v は縦書き不採用のため出さない＝Q1）。
// 本籍は aroll.json の行の `bubble_key`（無ければ自動＝話者の既定＋「！」でトゲ・「？」で雲）。
export const BUBBLE_GROUPS = [['丸', ['round_a']], ['角', ['rect_a', 'rect_b']], ['雲', ['cloud_a', 'cloud_b']], ['トゲ', ['spike_a', 'spike_b']]];
export const BUBBLE_KEYS = BUBBLE_GROUPS.flatMap(([, ks]) => ks);
const BUBBLE_KIND_LABEL = { round: '丸', rect: '角', cloud: '雲', spike: 'トゲ' };
const BUBBLE_AUTO_NOTE = { speaker_default: '話者の既定', question: '「？」があるので雲', exclaim: '「！」があるのでトゲ' };

/** 形キーの表示名（例: cloud_b → 雲B）。系統に1種だけ（丸）の時は系統名のみ。 */
export function bubbleLabel(key) {
  if (!key) return '';
  const [kind, v] = key.split('_');
  const same = BUBBLE_GROUPS.find(([, ks]) => ks.includes(key));
  return (BUBBLE_KIND_LABEL[kind] || kind) + (same && same[1].length > 1 && v ? v.toUpperCase() : '');
}

/**
 * 吹き出しの枠に出す状態。`key`＝今の形（自動なら合成プランが選んだ形・まだ無ければ null）、
 * `override`＝人が選んだか、`note`＝理由、`pending`＝選んだ形が合成にまだ反映されていない。
 */
export function bubbleView(l) {
  const a = l.aroll || {}, f = l.final || {};
  if (a.bubble_key) {
    return { key: a.bubble_key, override: true, pending: !!a.bubble_stale,
      note: a.bubble_stale ? '選んだ形（まだ合成に反映されていません）' : '選んだ形' };
  }
  if (a.bubble_stale) {       // 自動へ戻した直後（プランは前に選んだ形のまま）
    return { key: null, override: false, pending: true, note: '自動に戻しました（まだ合成に反映されていません）' };
  }
  if (f.bubble_key) {
    return { key: f.bubble_key, override: false, pending: false, note: `自動: ${BUBBLE_AUTO_NOTE[f.bubble_source] || '話者の既定'}` };
  }
  return { key: null, override: false, pending: false, note: '自動（合成すると決まります）' };
}

/** 形の簡単な図（SVG文字列）。bubbles.psd は非公開資産なので持ち出さず、系統の特徴だけを描く。 */
export function bubbleSvg(key) {
  const kind = (key || '').split('_')[0];
  const tail = key === 'rect_b' ? '<path d="M34 27 L40 35 L26 27 Z"/>' : '<path d="M14 27 L8 35 L22 27 Z"/>';
  let body;
  if (kind === 'round') body = '<ellipse cx="24" cy="15" rx="20" ry="13"/>';
  else if (kind === 'rect') body = '<rect x="4" y="3" width="40" height="24" rx="3"/>';
  else if (kind === 'cloud') body = '<path d="M13 27 C4 27 2 16 10 15 C9 6 21 3 25 9 C30 4 42 8 40 15 C47 17 45 27 36 27 Z"/>';
  else if (kind === 'spike') {
    const pts = [];
    for (let i = 0; i < 20; i++) { const r = i % 2 ? 11 : 17, t = (Math.PI * 2 * i) / 20; pts.push(`${(24 + Math.cos(t) * r * 1.2).toFixed(1)},${(15 + Math.sin(t) * r * 0.85).toFixed(1)}`); }
    body = `<polygon points="${pts.join(' ')}"/>`;
  } else return '';
  return `<svg class="bsvg" viewBox="0 0 48 36" aria-hidden="true" fill="currentColor">${body}${tail}</svg>`;
}
