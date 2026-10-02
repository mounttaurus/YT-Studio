// 行ワークベンチの純粋なロジック（DOMを触らない・Nodeで単体テストできる）。
// Docs/LINE_WORKBENCH_PLAN.md §5・W3。状態の導出は director（GET .../workbench）が済ませて返すので、
// ここは「返ってきた状態を描くための整形」と「UIで操作を出す前の下見」だけを持つ。

import { severity } from './final_model.js';

export const EMOTIONS = ['neutral', 'happy', 'excited', 'sad', 'serious', 'question', 'angry', 'surprised',
  'shy', 'whisper', 'confident', 'worried', 'gentle'];
export const EMOJI = { happy: '😊', excited: '😆', sad: '😭', serious: '📖', question: '🤔', angry: '😠',
  surprised: '😲', shy: '🫣', whisper: '👂', confident: '😎', worried: '😟', gentle: '🫶' };
export const SEG_NAMES = { script: '台本', tts: '音声', aroll: '絵', final: '仕上がり' };
export const LONG_LIMIT = 55;   // 字。これを超える行は「長い」（SUBLINE_PLAN §9・約12秒）

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// 推定秒数（SUBLINE_PLAN §0-2 の実測 秒 ≈ 0.8 + 0.2×字）
export const estSec = (text) => Math.round((0.8 + 0.2 * (text || '').length) * 10) / 10;

export const isSub = (l) => !!l.parent_line_id && l.parent_line_id !== l.id;
export const sameGroup = (a, b) => (a.parent_line_id || null) === (b.parent_line_id || null);

/** 同じサブ行グループの行（グループが無ければその行だけ）。 */
export const groupOf = (l, lines) => l.parent_line_id ? lines.filter((x) => x.parent_line_id === l.parent_line_id) : [l];

/** 話者の色クラス。配役の並び順で c0（青）・c1（緑）・c2（紫）…。話者が無い行は unset。 */
export function speakerClass(l, cast) {
  if (!l.speaker_id) return 'unset';
  const i = (cast || []).findIndex((c) => c.id === l.speaker_id);
  return i < 0 ? 'cx' : `c${i % 4}`;
}

/**
 * 1行の4工程の状態。[色クラス, ラベル]。色クラス: ok=完了 / warn=やり直し・未確定 / bad=欠けている /
 * info=途中 / ''=未着手。`enabled` は確定の運用がこの話数で始まっているか。
 */
export function segments(l, enabled) {
  const script = !enabled ? ['', '運用外']
    : l.confirm === 'confirmed' ? ['ok', '確定済み']
    : l.confirm === 'unconfirmed' ? ['warn', '未確定'] : ['', '空の行'];
  const tts = ({ done: ['ok', '音声あり'], stale: ['warn', '要再生成'], queued: ['info', '作り直し中'],
    unassigned: ['bad', '声が未割当'] })[l.tts?.state] || ['', '未生成'];
  const a = l.aroll || {};
  const aroll = !a.has_manifest ? ['', '未着手']
    : !a.panel ? ['bad', 'コマが無い']
    : a.picture ? (a.sync === 'stale' ? ['warn', '台本とズレ'] : a.sync === 'unknown' ? ['info', '記録なし'] : ['ok', '絵あり'])
    : ['', '絵なし'];
  const f = l.final || {};
  // 合成チェックの結果（サーバーが導出した build_state と検査の重さ）があればそれを使う。無ければプランの印だけ
  const sev = f.build_state && f.build_state !== 'unknown' ? severity(l) : null;
  const final = sev ? ({ blocking: ['bad', '要対応'], advisory: ['warn', '助言'], restale: ['warn', '再合成が要る'], clean: ['ok', '問題なし'],
      unbuilt: ['', '未合成'], ungenerated: ['', '絵なし'] })[sev]
    : f.status === 'needs_attention' ? ['warn', '合成を要確認'] : f.status ? ['info', '合成済み'] : ['', '未合成'];
  return { script, tts, aroll, final };
}

/** 帯（上部の4カード）の件数。 */
export function counts(lines, enabled) {
  const c = { unconfirmed: 0, long: 0, tts: {}, aroll: {}, final: {} };
  for (const l of lines) {
    const s = segments(l, enabled);
    for (const k of ['tts', 'aroll', 'final']) {
      const cls = s[k][0] || 'none';
      c[k][cls] = (c[k][cls] || 0) + 1;
    }
    if (enabled && l.confirm === 'unconfirmed') c.unconfirmed++;
    if ((l.text || '').length > LONG_LIMIT) c.long++;
  }
  return c;
}

/** 「要対応だけ」の絞り込み。 */
export function needsWork(tab, l, enabled) {
  if (tab === 'script') return (enabled && l.confirm === 'unconfirmed') || (l.text || '').length > LONG_LIMIT;
  return (segments(l, enabled)[tab]?.[0] || '') !== 'ok';
}

/** 音声が無い・古い行（「未生成・要再生成を生成」の対象）。声が未割当の行は作れないので含めない。 */
export const audioTodo = (lines) => lines.filter((l) => l.text && (l.tts.state === 'none' || l.tts.state === 'stale'));

/**
 * 操作の可否（できない理由つき。空文字＝できる）。director の窓口も同じ規則で拒否する
 * （こちらは押す前にグレーアウトして理由を出すための下見）。
 */
export function can(op, l, lines, enabled) {
  const i = lines.indexOf(l), next = lines[i + 1], prev = lines[i - 1];
  switch (op) {
    case 'merge':
      if (!next) return '最後の行です';
      if (next.speaker_id !== l.speaker_id) return '次の行と話者が違います';
      if (next.section !== l.section) return '次の行とセクションが違います';
      if (!sameGroup(l, next)) return '次の行と別のグループです';
      return '';
    case 'up':
      if (!prev) return '先頭の行です';
      if (l.section !== prev.section) return 'セクションをまたいで動かせません';
      if ((l.parent_line_id || prev.parent_line_id) && !sameGroup(l, prev)) return 'サブ行のグループをまたいで動かせません';
      return '';
    case 'down':
      if (!next) return '最後の行です';
      if (l.section !== next.section) return 'セクションをまたいで動かせません';
      if ((l.parent_line_id || next.parent_line_id) && !sameGroup(l, next)) return 'サブ行のグループをまたいで動かせません';
      return '';
    case 'split': return (l.text || '').length < 2 ? '本文が短すぎます' : '';
    case 'split-apply': return (l.text || '').length <= LONG_LIMIT ? `上限（${LONG_LIMIT}字）以内です` : '';
    case 'confirm':
      if (!enabled) return '確定の運用が始まっていません（帯の「確定の運用を始める」）';
      if (l.confirm === 'empty') return '本文が空です';
      return l.confirm === 'confirmed' ? '変更はありません' : '';
    default: return '';
  }
}

/** 分割位置を近くの句読点の直後へ吸着させる（無ければそのまま）。 */
/** 本文から改行を取り除く（本文に改行は意味が無い。TTS・字幕・吹き出しへ流れるのを防ぐ）。 */
export function oneLine(text) {
  return (text || '').replace(/[\r\n]+/g, '');
}

export function snap(text, pos) {
  const c = [];
  for (let i = 1; i < text.length; i++) if ('。！？、」』'.includes(text[i - 1])) c.push(i);
  return c.length ? c.reduce((a, b) => (Math.abs(b - pos) < Math.abs(a - pos) ? b : a)) : pos;
}

export const OP_NAMES = { text: '本文を保存', emotion: '声の感情を変更', speaker: '話者を変更', split: 'ここで分ける',
  merge: '次の行と結合', addsub: 'サブ行を追加', insert: '下に行を挿入', up: '上へ移動', down: '下へ移動', delete: '削除',
  'split-apply': '長い行を自動で区切る', 'split-apply-all': '長い行をすべて自動で区切る', timing: '速度・間を保存' };

/** 画面の操作名 → 窓口（director の lines/{op}）の op と本文。 */
export function toApi(uiOp, l, p = {}) {
  switch (uiOp) {
    case 'text': return { op: 'edit', body: { line_id: l.id, text: p.text } };
    case 'emotion': return { op: 'emotion', body: { line_id: l.id, emotion: p.emotion } };
    case 'speaker': return { op: 'speaker', body: { line_id: l.id, speaker_id: p.speaker_id, speaker_name: p.speaker_name } };
    case 'split': return { op: 'split', body: { line_id: l.id, position: p.pos } };
    case 'merge': return { op: 'merge', body: { line_id: l.id } };
    case 'addsub': return { op: 'add-subline', body: { line_id: l.id, text: '' } };
    case 'insert': return { op: 'insert', body: { after_line_id: l.id, text: '' } };
    case 'up': return { op: 'move', body: { line_id: l.id, direction: 'up' } };
    case 'down': return { op: 'move', body: { line_id: l.id, direction: 'down' } };
    case 'delete': return { op: 'delete', body: { line_id: l.id } };
    case 'timing': return { op: 'edit', body: { line_id: l.id, speed: p.speed, pause_after_sec: p.pause_after_sec } };
    case 'split-apply': return { op: 'split-apply', body: { line_id: l.id } };
    case 'split-apply-all': return { op: 'split-apply-all', body: {} };
    default: throw new Error(`unknown op: ${uiOp}`);
  }
}

/**
 * 操作のあと、行モーダルをどの行に合わせるか。追加は新しい行へ、削除は隣へ、それ以外はそのまま。
 * 戻り値 null＝閉じる。
 */
export function focusAfter(uiOp, l, lines, res) {
  if (uiOp === 'addsub' || uiOp === 'insert') return (res.new_line_ids || [])[0] || l.id;
  if (uiOp === 'delete') {
    const i = lines.indexOf(l);
    return (lines[i + 1] || lines[i - 1] || {}).id || null;
  }
  return l.id;
}

/**
 * 「実行して確定」で確定する行＝**この操作が触れた行のうち、操作後に未確定の行**。
 * 窓口の応答の `state`（dry_run は見込み・実行後は正本）の `touched_line_ids` × `unconfirmed_line_ids`。
 * 触れていない未確定の行（別の直しかけ・他の画面や MCP の変更）は巻き込まない。運用外の話数は空。
 */
export function confirmTargets(state) {
  if (!state || !state.confirmation_enabled) return [];
  const touched = new Set(state.touched_line_ids || []);
  return (state.unconfirmed_line_ids || []).filter((id) => touched.has(id));
}

/**
 * 確定後の音声の作り直しを見守るポーリングの判断。
 * running＝tts-agent が今どれかの行を生成中。pending＝まだ最新でない、頼んだ行のID。
 *   continue … もう少し待つ ／ done … 全部できた ／ stuck … 生成が動いていないのに残っている（エンジン停止など）
 */
export function pollDecision({ running, pending, idlePolls }, maxIdle = 3) {
  if (running) return { action: 'continue', idlePolls: 0 };
  if (!pending.length) return { action: 'done', idlePolls: 0 };
  return idlePolls + 1 >= maxIdle ? { action: 'stuck', idlePolls: idlePolls + 1 } : { action: 'continue', idlePolls: idlePolls + 1 };
}
