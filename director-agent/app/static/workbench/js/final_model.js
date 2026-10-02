// 仕上がりタブ（合成チェックと再合成）の純粋なロジック（DOMを触らない・Nodeで単体テストできる）。
// Docs/LINE_WORKBENCH_PLAN.md §5-4-1（W4b-2）・§6-2。状態は director の GET .../workbench の `lines[].final` と `psassist`。

import { needsReview } from './aroll_model.js';
export const SEV_ORDER = ['blocking', 'advisory', 'restale', 'unbuilt', 'ungenerated', 'clean'];
export const SEV_LABEL = { blocking: '要対応', advisory: '助言', clean: '問題なし', unbuilt: '未合成', ungenerated: '未生成', restale: '要組み直し' };
export const SEV_MARK = { blocking: '🔴', advisory: '🟡', clean: '✓', unbuilt: '🔧', ungenerated: '○', restale: '♻️' };
export const SEV_CLASS = { blocking: 'bad', advisory: 'warn', clean: 'ok', unbuilt: 'info', ungenerated: '', restale: 'purple' };

export const CODE_LABEL = {
  BUBBLE_OVERLAP: '吹き出し重なり', TEXT_OVERFLOW: '文字はみ出し', TEXT_OFF_CANVAS: '文字が画面外', BUBBLE_ON_FACE: '顔に被る',
  FRAMING_MISMATCH: '画角不一致', LIGHT_CONFLICT: '光源が逆', EMPTY_LAYER: '空レイヤー', NO_ADJUSTMENT: '色調整なし',
  MISSING_CHARACTER: 'キャラ欠落', MISSING_BUBBLE: '吹き出し欠落', MISSING_TEXT: '文字欠落', NO_VISIBLE_BACKGROUND: '背景なし',
  EXPORT_MISSING: '書き出し未', EXPORT_SIZE: '書き出し寸法', EXPORT_STALE: '書き出しが古い', EXPORT_UNREADABLE: '書き出し不良',
  PSD_UNREADABLE: 'PSD不良',
};
export const codeLabel = (c) => CODE_LABEL[c] || c;

export const hasPanel = (l) => !!(l.aroll && l.aroll.panel);
const F = (l) => l.final || {};

/**
 * 行の合成の状態（サーバーが導出した build_state と検査の重さから）。
 * 検査していない行は build_state だけ（未合成・未生成・要組み直し）。古い合成は指摘を出さない（絵が変わっている）。
 */
export function severity(l) {
  const f = F(l);
  if (f.build_state === 'ungenerated') return 'ungenerated';
  if (f.build_state === 'restale') return 'restale';
  if (f.severity) return f.severity;
  return f.build_state === 'built' ? 'clean' : 'unbuilt';
}

/** 一覧に出す主要因（要対応を優先して1つ）。 */
export function mainIssue(l) {
  const list = F(l).issues || [];
  const it = list.find((i) => i.severity === 'blocking') || list[0];
  return it ? codeLabel(it.code) : '';
}
export const hasCode = (l, code) => (F(l).issues || []).some((i) => i.code === code);

export function counts(lines) {
  const c = { total: lines.length, blocking: 0, advisory: 0, clean: 0, unbuilt: 0, ungenerated: 0, restale: 0, edited: 0, unapproved: 0 };
  for (const l of lines) {
    c[severity(l)] = (c[severity(l)] || 0) + 1;
    if (F(l).edited) c.edited++;
    const a = l.aroll || {};
    if (a.panel && needsReview(a)) c.unapproved++;
  }
  return c;
}

/** 絞り込み。code:XXX で指摘コード別。 */
export const FILTERS = [['all', 'すべて'], ['blocking', '🔴要対応'], ['advisory', '🟡助言'], ['restale', '♻️要組み直し'], ['unbuilt', '🔧未合成'],
  ['ungenerated', '○未生成'], ['unapproved', '絵が未確認'], ['edited', '✋手直し済み']];

export function rowsFor(lines, f) {
  if (f === 'all' || !f) return lines;
  if (f.startsWith('code:')) return lines.filter((l) => hasCode(l, f.slice(5)));
  if (f === 'edited') return lines.filter((l) => F(l).edited);
  if (f === 'unapproved') return lines.filter((l) => { const a = l.aroll || {}; return a.panel && needsReview(a); });
  return lines.filter((l) => severity(l) === f);
}

/** いま出ている指摘コードの一覧（多い順）。指摘コード別の絞り込みの選択肢。 */
export function codeFacets(lines) {
  const m = new Map();
  for (const l of lines) for (const c of new Set((F(l).issues || []).map((i) => i.code))) m.set(c, (m.get(c) || 0) + 1);
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
}

/** 一覧のサムネ。合成サムネ → 無ければ切り抜き／生成画像。古い合成（要組み直し）は出さない。 */
export function thumbOf(l) {
  const f = F(l);
  if (f.thumb && f.build_state !== 'restale' && f.build_state !== 'ungenerated') return { url: f.thumb, composed: true };
  const a = l.aroll || {};
  return a.thumb ? { url: a.thumb, composed: false } : null;
}

/** 納品PNGの更新が要る行（EXPORT_STALE・EXPORT_MISSING を持つ行）。 */
export const exportTargets = (lines) => lines.filter((l) => hasCode(l, 'EXPORT_STALE') || hasCode(l, 'EXPORT_MISSING')).map((l) => l.id);

/**
 * 再合成の計画。確定の運用が始まっている話数は確定済みの行だけ（サーバーも同じ規則）。
 * 手直し済みは既定で飛ばし、includeEdited の時だけ対象（退避してから上書き）。
 * `ids` が空の時は何も走らない（`lines: []` を「全件」にしない）。
 */
export function planResync(lines, ids, enabled, includeEdited) {
  const byId = new Map(lines.map((l) => [l.id, l]));
  const targets = ids.map((id) => byId.get(id)).filter((l) => l && hasPanel(l));
  const unconfirmed = enabled ? targets.filter((l) => l.confirm !== 'confirmed').map((l) => l.id) : [];
  const confirmed = targets.filter((l) => !unconfirmed.includes(l.id));
  const edited = confirmed.filter((l) => F(l).edited).map((l) => l.id);
  const noPicture = confirmed.filter((l) => !l.aroll.picture).map((l) => l.id);          // 絵が無い行は組めない
  const run = confirmed.filter((l) => l.aroll.picture && (includeEdited || !F(l).edited)).map((l) => l.id);
  return { run, unconfirmed, edited, skippedEdited: includeEdited ? [] : edited.filter((id) => !noPicture.includes(id)), includeEdited, noPicture, total: targets.length };
}

/** 1行の再合成ができない理由（空文字＝できる）。 */
export function resyncBlock(l, { enabled, alive, busy }) {
  if (!hasPanel(l)) return 'コマが無い行です';
  if (enabled && l.confirm !== 'confirmed') return '未確定の行です（先に「✓ 確定」）';
  if (!(l.aroll.picture)) return '絵がまだありません（絵タブで決めてください）';
  if (!alive) return 'ホスト工程（Photoshop）が止まっています';
  if (busy) return '別のジョブが実行中です';
  return '';
}

/** 選び直した直後に自動で再合成してよいか（D25＝確定済み ∧ 手直し無し ∧ 要組み直し ∧ ホスト工程が動いている）。 */
export function canAutoResync(l, { enabled, alive, busy }) {
  if (!hasPanel(l) || !alive || busy) return false;
  if (enabled && l.confirm !== 'confirmed') return false;
  const f = F(l);
  return !f.edited && f.build_state === 'restale';
}

/** 「✋ 手直し済み」「♻️」などの状態チップ [クラス, 文言][]。 */
export function chips(l) {
  const f = F(l), out = [];
  const s = severity(l);
  out.push([SEV_CLASS[s], `${SEV_MARK[s]} ${SEV_LABEL[s]}`]);
  if (f.edited) out.push(['edit', '✋ 手直し済み']);
  if (hasCode(l, 'EXPORT_STALE') || hasCode(l, 'EXPORT_MISSING')) out.push(['info', '📤 納品PNGが古い/無い']);
  return out;
}

/** 指摘の赤枠（PSD座標→%）。 */
export function regionBox(f, it) {
  const [cw, ch] = (f.measured && f.measured.canvas) || [1376, 768];
  const [x0, y0, x1, y1] = it.region;
  return { left: (x0 / cw) * 100, top: (y0 / ch) * 100, width: ((x1 - x0) / cw) * 100, height: ((y1 - y0) / ch) * 100 };
}

export function measuredRows(f) {
  const m = f.measured || {}, r = [];
  const pct = (v) => `${(v * 100).toFixed(1)}%`;
  if (m.overlap !== undefined) r.push(['吹き出し重なり', pct(m.overlap)]);
  if (m.text_outside !== undefined) r.push(['文字はみ出し', pct(m.text_outside)]);
  if (m.face_overlap !== undefined) r.push(['顔に被る', pct(m.face_overlap)]);
  if (m.heads !== undefined) r.push(['頭身', m.heads]);
  if (m.light_dx !== undefined && m.light_dx !== null) {
    r.push(['光の向き', `キャラ ${m.light_dx > 0 ? '+' : ''}${m.light_dx}${m.bg_light_dx !== undefined ? ` / 背景 ${m.bg_light_dx > 0 ? '+' : ''}${m.bg_light_dx}` : ''}`]);
  }
  if (m.shot) r.push(['ショット', m.shot]);
  if (m.background) r.push(['背景', m.background]);
  if (m.export_size) r.push(['納品PNG', m.export_size.join('×')]);
  return r;
}

/** PSD のホスト上の場所（検査レポートの episode_dir ＋ 相対パス）。Photoshop で開く時にコピーする。 */
export function psdHostPath(psassist, f) {
  if (!f.psd) return '';
  const ep = (psassist && psassist.episode_dir) || '';
  if (!ep) return f.psd;
  return `${ep.replace(/[\\/]+$/, '')}\\${f.psd.replace(/\//g, '\\')}`;
}

/** ホスト工程を起動するコマンド。`--shared` を省かない（資産を持つチェックアウトから稼働側の shared を指す）。 */
export function sharedDirOf(episodeDir) {
  if (!episodeDir) return '';
  const parts = episodeDir.replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts.length >= 4 ? parts.slice(0, -4).join('\\') : '';
}
export function startCommand(episodeDir) {
  const shared = sharedDirOf(episodeDir);
  return shared ? `.\\start-psassist-worker.ps1 -Shared "${shared}"` : '.\\start-psassist-worker.ps1 -Shared "<稼働中のsharedフォルダ>"';
}
export function hostCommands(episodeDir, epNumber) {
  const dir = episodeDir || `<エピソードのフォルダ>\\episodes\\ep${String(epNumber).padStart(2, '0')}`;
  return [
    ['検査の見張り（保存するたびに1枚だけ検査）', `python psassist/scripts/qa_check.py --episode "${dir}" --watch`],
    ['全部検査', `python psassist/scripts/qa_check.py --episode "${dir}"`],
    ['納品PNGの書き出し', `python psassist/host-bridge/export_png.py --episode "${dir}" --all --resume`],
  ];
}

/** ホスト工程の工程ボタン。director は kind の文字列だけを知る（Photoshop 固有の詳細は持たない）。 */
export const STEPS = [
  { kind: 'build_plan', label: '① プランを作る', ps: false, hint: 'aroll.json と背景アーカイブから panel_plan.json を組み立てます（Photoshop不要・数秒）' },
  { kind: 'cutout', label: '② 背景を抜く', ps: true, hint: '生成した絵からキャラを切り抜きます。在庫から選んだ行は既に抜けているので対象外（2人写りだけが対象）' },
  { kind: 'qa_check', label: '④ 検査する', ps: false, hint: '合成結果を検査してサムネと指摘を作ります（Photoshop不要・196枚で約1分）' },
];

export const JOB_LABEL = { build_plan: '① プラン', cutout: '② 背景抜き', build_panel: '③ 組版', qa_check: '④ 検査', export_png: '⑤ 納品PNG', resync: '再合成' };
export const JOB_STATUS = { running: '⏳ 実行中', done: '✔ 完了', failed: '✘ 失敗', cancelled: '⏸ 中断', queued: '待機中' };

/** ジョブの見守り。running でなくなったら done。 */
export const jobRunning = (jobs) => !!(jobs && jobs[0] && jobs[0].status === 'running');
export function jobSummary(job) {
  if (!job) return '';
  const r = job.result || {};
  const bits = [];
  const b = r.build || r;
  if (b.lines !== undefined && b.total !== undefined) bits.push(`${b.lines}/${b.total}行`);
  if ((b.skipped_edited || []).length) bits.push(`✋手直し済み ${b.skipped_edited.length}行は飛ばしました`);
  if ((b.backed_up || []).length) bits.push(`元のPSD ${b.backed_up.length}枚を退避しました`);
  return bits.join('・');
}

export const jobPercent = (job) => (job && job.progress && job.progress.total ? Math.round((job.progress.done / job.progress.total) * 100) : 0);
