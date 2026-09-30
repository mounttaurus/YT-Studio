// 行ワークベンチ（Docs/LINE_WORKBENCH_PLAN.md §5・W3）。台本・TTS の2タブ＋行モーダル＋4工程の帯＋確定＋Undo＋全行再生。
// 状態は director の GET .../workbench が導出して返す。台本を変える操作は director の窓口
// （POST .../lines/{op}）だけを通す（I1）。ここは描画とイベントだけ。ロジックは model.js・api.js・player.js。
import { createApi } from './api.js';
import * as M from './model.js';
import { Player } from './player.js';
import { createArollUi } from './aroll_ui.js';
import * as R from './aroll_model.js';
import { createFinalUi } from './final_ui.js';
import * as FM from './final_model.js';

const $ = (id) => document.getElementById(id);
const esc = M.esc;
const api = createApi();
const params = new URLSearchParams(location.search);

const storage = (() => { try { return window.localStorage; } catch { return null; } })();
const store = {
  get: (k) => { try { return storage && storage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { storage && storage.setItem(k, v); } catch { /* ignore */ } },
};

const TABS = ['script', 'tts', 'aroll', 'final'];
const S = {
  pid: params.get('project'), ep: Number(params.get('episode')) || null,
  view: null, lines: [], cast: [], enabled: false, tpending: null,
  tab: TABS.includes(params.get('tab')) ? params.get('tab') : (TABS.includes(store.get('wb-tab')) ? store.get('wb-tab') : 'tts'),
  filter: 'all', afilter: 'all', ffilter: 'all', sel: new Set(), modal: null, drawer: null, notice: [], ver: 1, audioSig: '',
  queued: new Set(), poll: null, idle: 0, working: 0,
};
const byId = (id) => S.lines.find((l) => l.id === id);
const idx = (l) => S.lines.indexOf(l);

// ── 小さな道具 ───────────────────────────────────────────
let toastTimer;
function toast(msg, { bad = false, ms = 3200 } = {}) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast' + (bad ? ' bad' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}
const fail = (e) => toast(e && e.message ? e.message : String(e), { bad: true, ms: 7000 });

/** 時間のかかる操作の間、押し直しを防ぎ、状況を出す。 */
async function working(msg, fn) {
  if (S.working) return undefined;
  S.working++;
  toast(msg, { ms: 0 });
  document.body.style.cursor = 'progress';
  try { return await fn(); } catch (e) { fail(e); return undefined; } finally {
    S.working--;
    document.body.style.cursor = '';
    if (!S.working) {
      $('toast').hidden = true;
      // 操作中に描いた画面はボタンが無効のまま（押し直し防止）。終わったら描き直して有効に戻す
      if (S.view) { render(); if (S.modal && !S.modal.edited && !S.modal.pending && !S.drawer) drawModal(); }
    }
  }
}

const speakerName = (l) => l.speaker_name || '話者未選択';
const tagName = { auto: '自動', later: 'あとで', keep: 'そのまま' };
const LANES = [['script', '台本', '本籍'], ['tts', '音声', 'TTS'], ['aroll', '絵', 'Aロール'], ['final', '仕上がり', '合成']];

// ── 読み込み ───────────────────────────────────────────
async function load() {
  const v = await api.view(S.pid, S.ep);
  S.view = v; S.lines = v.lines; S.cast = v.cast; S.enabled = v.confirmation.enabled;
  const sig = v.lines.map((l) => `${l.id}:${l.tts.state}:${l.tts.duration_sec}`).join('|');
  if (sig !== S.audioSig) { S.audioSig = sig; S.ver++; }   // 音声が変わったらURLを変えてキャッシュを外す
  render();
  if (S.modal && !S.modal.edited && !S.modal.pending) drawModal();
  else if (S.modal && !byId(S.modal.id)) closeModal();
  if (v.tts_progress && v.tts_progress.running) startFollow();
}

// ── 描画: 全体 ────────────────────────────────────────────
function render() {
  const v = S.view;
  $('title').textContent = `第${v.episode.number}話 行ワークベンチ`;
  $('subtitle').innerHTML = `<span class="mono">${esc(v.project.id)}</span> ／ ${esc(v.project.title)}${v.episode.title ? ` ／ ${esc(v.episode.title)}` : ''}`;
  document.title = `第${v.episode.number}話 行ワークベンチ`;
  const u = $('undo');
  u.disabled = !v.undoable;
  u.textContent = v.undoable ? `↶ 元に戻す（${v.undoable}）` : '↶ 元に戻す';
  renderBanner(); renderHealth(); renderTabs(); renderToolbar(); renderList(); renderFloat();
}

function renderBanner() {
  const v = S.view, msgs = [];
  if (!v.has_script) msgs.push(`この話数には正本の台本がまだありません。${v.proposal_pending ? '「LLMの案を見る」から、ドラフトを最初の台本として採用してください。' : 'director で台本を生成してください。'}`);
  if (!v.services.tts) msgs.push('tts-agent に繋がりません。音声の状態は記録の有無だけで表示しています。');
  if (!v.services.aroll && v.lines.some((l) => l.aroll.has_manifest)) msgs.push('scrapping-agent に繋がりません。絵の「古い」判定は出せません。');
  const el = $('banner');
  const parts = [...msgs.map((m) => `<div class="banner">${esc(m)}</div>`),
    ...(S.notice.length ? [`<div class="banner">直前の操作の注意<ul>${S.notice.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>
      <button class="btn ghost" data-act="dismiss-notice">閉じる</button></div>`] : [])];
  el.innerHTML = parts.join('');
  el.hidden = !parts.length;
}

const pill = (cls, text) => `<span class="pill ${cls || 'none'}">${esc(text)}</span>`;
const uiCtx = {
  S, api, esc, toast, fail, working, load, byId, pill, rerender: () => { if (S.view) render(); },
  redrawModal: () => { if (S.modal && !S.drawer) drawModal(); },
};
const FI = createFinalUi(uiCtx);
const AR = createArollUi({ ...uiCtx, autoResync: (id) => FI.autoResync(id) });
const pills = (o, map) => map.map(([k, cls, lbl]) => (o[k] ? pill(cls, `${o[k]} ${lbl}`) : '')).join('') || pill('none', '—');

function renderHealth() {
  const c = M.counts(S.lines, S.enabled), v = S.view;
  let script;
  if (!v.has_script) script = pill('bad', '正本なし');
  else if (!S.enabled) script = `${pill('none', '確定の運用は未開始')}<button class="btn" data-act="startconf" title="今の台本を全行「確定済み」として記録して始めます">確定の運用を始める</button>`;
  else script = c.unconfirmed
    ? `${pill('warn', `未確定 ${c.unconfirmed}行`)}<button class="btn ok" data-act="confirmall">すべて確定</button>`
    : pill('ok', 'すべて確定済み');
  const proposal = v.proposal_pending
    ? `<button class="btn" data-act="proposal" title="LLMが書いた下書き（全文生成・行の書き直し）を、差分を見て正本へ採用します">${pill('warn', 'LLMの案あり')} 差分を見る</button>`
    : pill('none', 'LLMの案 なし');
  const prog = v.tts_progress && v.tts_progress.running ? ` ${pill('info', `⏳ 生成中 ${v.tts_progress.done ?? 0}/${v.tts_progress.total ?? '?'}`)}` : '';
  const hasAroll = S.lines.some((l) => l.aroll.has_manifest);
  $('health').innerHTML = `
    <div class="hcard"><div class="lbl">台本（すべての本籍）</div><div class="val">${script} ${proposal}</div></div>
    <div class="hcard"><div class="lbl">音声</div><div class="val">${pills(c.tts, [['ok', 'ok', '生成済み'], ['warn', 'warn', '要再生成'], ['info', 'info', '作り直し中'], ['bad', 'bad', '声未割当'], ['none', 'none', '未生成']])}${prog}</div></div>
    <div class="hcard"><div class="lbl">絵</div><div class="val">${hasAroll ? pills(c.aroll, [['bad', 'bad', 'コマ無し'], ['warn', 'warn', '絵が古い'], ['info', 'info', '記録なし'], ['ok', 'ok', '絵あり'], ['none', 'none', '絵なし']]) : pill('none', 'Aロール未着手')}</div></div>
    <div class="hcard"><div class="lbl">仕上がり（合成）</div><div class="val">${pills(c.final, [['bad', 'bad', '要対応'], ['warn', 'warn', '助言・要組み直し'], ['ok', 'ok', '問題なし'], ['info', 'info', '合成済み'], ['none', 'none', '未合成']])}</div></div>`;
}

function renderTabs() {
  const c = M.counts(S.lines, S.enabled);
  const todo = M.audioTodo(S.lines).length;
  const tab = (k, label, n, disabledTitle) => `<button class="tab" role="tab" data-tab="${k}" aria-selected="${S.tab === k}" ${disabledTitle ? `disabled title="${disabledTitle}"` : ''}>${label}${n ? `<span class="n">${n}</span>` : ''}</button>`;
  $('tabs').innerHTML = tab('script', '📝 台本', S.enabled ? `${c.unconfirmed}件未確定` : '')
    + tab('tts', '🎙 TTS', todo ? `${todo}件` : '')
    + tab('aroll', '🖼 Aロール', R.filterCount(S.lines, 'ungenerated') ? `${R.filterCount(S.lines, 'ungenerated')}件未生成` : '')
    + tab('final', '✅ 仕上がり', (() => { const n = FM.counts(S.lines); return n.blocking + n.restale ? `${n.blocking + n.restale}件` : ''; })(),
      S.view && S.view.psassist && !S.view.psassist.available ? 'この環境ではPhotoshop連携（ホスト工程）を使いません' : '');
}

function renderToolbar() {
  if (S.tab !== 'script') S.tpending = null;
  if (S.tab === 'final') { $('toolbar').innerHTML = FI.toolbarHtml(); return; }
  if (S.tab === 'aroll') {
    if (AR.hasBusyInput()) return;                       // 追加指示を入力中は描き直さない（入力が消える）
    $('toolbar').innerHTML = AR.toolbarHtml();
    return;
  }
  const c = M.counts(S.lines, S.enabled), todo = M.audioTodo(S.lines).length;
  const t = S.tab === 'script'
    ? `<button class="btn" data-act="autosplit" ${c.long && !S.working ? '' : 'disabled'} title="${c.long ? `${M.LONG_LIMIT}字を超える行を、句点・読点で自動で区切ります（影響を確認してから実行）` : `${M.LONG_LIMIT}字を超える行はありません`}">✂ 長い行を自動で区切る<span class="n">${c.long}</span></button>
       <span class="hint">校正は Claude Code から（MCP の update_script_line で1行ずつ直す）</span>`
    : `<button class="btn primary" data-act="genall" ${todo ? '' : 'disabled'}>🎙 未生成・要再生成を生成<span class="n">${todo}</span></button>
       <button class="btn" data-act="playall" ${playlist().length ? '' : 'disabled'}>▶ 全行再生</button>`;
  $('toolbar').innerHTML = `${t}<span class="spacer"></span>
    <div class="filter" role="group" aria-label="表示する行"><button data-act="filter" data-f="all" aria-pressed="${S.filter === 'all'}">すべて</button>
    <button data-act="filter" data-f="work" aria-pressed="${S.filter === 'work'}">要対応だけ</button></div>
    ${S.tab === 'script' && S.tpending ? tpendingHtml() : ''}`;
}

/** 帯から始める操作（話数全体の自動区切り）の確認欄。中身は窓口の dry_run（実行と同じ検証を通した結果）。 */
function tpendingHtml() {
  const p = S.tpending;
  let body;
  if (p.loading) body = '<div class="hint">影響を調べています…</div>';
  else if (p.error) body = `<div class="note warn">${esc(p.error)}</div>`;
  else if (p.dry && p.dry.ok === false) body = `<div class="note warn">この操作はできません: ${esc(p.dry.reason)}</div>`;
  else if (p.dry && p.dry.changed === false) body = '<div class="note">区切れる行はありません（上限内、または句読点の無い長い1文）</div>';
  else body = lanesHtml(p.dry.impact) + `<span class="hint">新しい行 ${(p.dry.new_line_ids || []).length}行ができます。読点で切った行は「区切りを要確認」の印が付きます</span>`;
  const ok = !p.loading && !p.error && p.dry && p.dry.ok !== false && p.dry.changed !== false;
  return `<div class="confirm" id="t-confirm" style="flex-basis:100%"><h3>「${M.OP_NAMES[p.op]}」でこうなります（どのタブから押しても同じ）</h3>${body}
    <div class="inline"><span class="hint">費用 0円（画像の生成は走りません）</span><span class="spacer"></span>
    <button class="btn ghost" data-act="tcancel">やめる</button><button class="btn primary" data-act="trun" ${ok ? '' : 'disabled'}>${M.OP_NAMES[p.op]}を実行</button></div></div>`;
}

async function startAutoSplit() {
  const pending = { op: 'split-apply-all', dry: null, loading: true, error: '' };
  S.tpending = pending;
  renderToolbar();
  try { pending.dry = await api.lineOp(S.pid, S.ep, 'split-apply-all', {}, { dryRun: true }); } catch (e) { pending.error = e.message; }
  pending.loading = false;
  if (S.tpending === pending) renderToolbar();
}

async function runAutoSplit() {
  const p = S.tpending;
  if (!p || p.loading || S.working) return;
  await working('実行しています…', async () => {
    const res = await api.lineOp(S.pid, S.ep, p.op, {});
    S.tpending = null;
    await load();
    notify(res.warnings);
    toast(res.changed ? `${(res.new_line_ids || []).length}行の新しいサブ行ができました：台本・音声・コマに反映しました${res.warnings.length ? '（注意あり）' : ''}` : '区切れる行はありませんでした');
  });
}

// ── 描画: 行一覧 ──────────────────────────────────────────
function detailHtml(l) {
  if (S.tab === 'script') {
    const n = (l.text || '').length;
    return `<div class="detail"><span class="chip">${M.EMOJI[l.emotion] || ''} ${esc(l.emotion || 'neutral')}</span>
      <span class="mono">${n}字・推定${M.estSec(l.text)}秒</span>${n > M.LONG_LIMIT ? '<span class="chip warn">長い（55字超）</span>' : ''}
      ${l.split_review ? '<span class="chip warn" title="読点などで区切った行。人が見たら外れます">区切りを要確認</span>' : ''}</div>`;
  }
  if (S.tab === 'aroll') return AR.detailHtml(l);
  const st = { done: '<span class="chip ok">✓ 生成済み</span>', stale: '<span class="chip warn">⚠ 要再生成</span>',
    queued: '<span class="chip">⏳ 作り直し中</span>', unassigned: '<span class="chip bad">声が未割当</span>' }[l.tts.state] || '<span class="chip none">未生成</span>';
  return `<div class="detail">${st}${l.tts.duration_sec ? `<span class="mono">${l.tts.duration_sec}s</span>` : ''}
    ${l.tts.duration_sec ? `<button class="btn ghost" data-act="play" data-id="${esc(l.id)}" title="この行から再生">▶</button>` : ''}</div>`;
}

const stripHtml = (seg) => `<div class="strip">${Object.keys(seg).map((k) => `<div class="seg ${seg[k][0]} ${S.tab === k ? 'cur' : ''}" title="${M.SEG_NAMES[k]}: ${esc(seg[k][1])}"><i></i><span>${M.SEG_NAMES[k]}</span></div>`).join('')}</div>`;

function rowHtml(l) {
  const seg = M.segments(l, S.enabled), reason = M.can('confirm', l, S.lines, S.enabled);
  const sub = M.isSub(l);
  return `<div class="row ${sub ? 'sub' : ''} ${S.enabled && l.confirm === 'unconfirmed' ? 'dirty' : ''} ${player.id === l.id ? 'playing' : ''}" id="r-${esc(l.id)}" tabindex="0" data-open="${esc(l.id)}">
    <div class="meta">${S.tab === 'aroll' ? AR.rowSelHtml(l) : ''}<span class="ord mono">${sub ? '<span class="arrow">↳</span>' : ''}${l.order}</span><span class="spk ${M.speakerClass(l, S.cast)}">${esc(speakerName(l))}</span></div>
    <div class="body"><div class="text">${esc(l.text) || '<span style="color:var(--faint)">（本文なし）</span>'}</div>${detailHtml(l)}</div>
    <div class="side">${stripHtml(seg)}<button class="btn ok" data-act="confirm" data-id="${esc(l.id)}" ${reason ? `disabled title="${esc(reason)}"` : 'title="この行の変更を確定（音声は自動で作り直し・再合成の対象になる）"'}>✓ 確定</button></div></div>`;
}

function renderList() {
  if (S.tab === 'final') { $('list').className = ''; $('list').innerHTML = FI.gridHtml(); return; }
  const rows = S.tab === 'aroll' ? R.rowsFor(S.lines, S.afilter) : S.lines.filter((l) => S.filter === 'all' || M.needsWork(S.tab, l, S.enabled));
  $('list').className = 'list';
  $('list').innerHTML = rows.map(rowHtml).join('') || `<div class="note">${S.lines.length ? 'この条件の行はありません' : '行がありません'}</div>`;
}

// ── 行モーダル ────────────────────────────────────────────
const scrim = $('scrim'), modal = $('modal');
const MSEC = [['script', '台本'], ['tts', '音声'], ['aroll', '絵'], ['final', '仕上がり']];

function openModal(id, section) {
  S.drawer = null;
  S.modal = { id, section: section || ({ tts: 'tts', aroll: 'aroll', final: 'final' }[S.tab] || 'script'), pending: null, edited: false };
  scrim.hidden = false;
  drawModal();
  prepareSection();
  modal.focus();
}
/** 絵の区画を開いた時に、語彙・キャラ一覧を読む（冪等）。 */
function prepareSection() {
  if (S.modal && S.modal.section === 'aroll') Promise.all([AR.enter(), AR.loadChars()]).then(() => AR.A && drawIfModal());
  if (S.modal && S.modal.section === 'final') FI.enter().then(drawIfModal);
}
function drawIfModal() { if (S.modal && !S.drawer) drawModal(); }
function closeModal() { S.modal = null; S.drawer = null; scrim.hidden = true; }

function opBtn(op, l, label, hint, cls = '') {
  const r = M.can(op, l, S.lines, S.enabled);
  return `<button class="op ${cls}" data-op="${op}" ${r ? 'disabled' : ''}><b>${label}</b><small>${esc(r || hint)}</small></button>`;
}

const lanesHtml = (impact) => LANES.map(([k, who, sub]) => {
  const items = (impact || {})[k] || [];
  return items.length ? `<div class="lane"><div class="who2">${who}<small>${sub}</small></div><ul>${items.map((i) =>
    `<li><span class="tag ${i.kind}">${tagName[i.kind] || i.kind}</span><span>${esc(i.text)}</span></li>`).join('')}</ul></div>` : '';
}).join('');

/** 実行前の確認欄。中身は director の dry_run（実行と同じ検証を通した結果）。 */
function pendingHtml(l) {
  const p = S.modal.pending, name = M.OP_NAMES[p.op];
  let body;
  if (p.loading) body = '<div class="hint">影響を調べています…</div>';
  else if (p.error) body = `<div class="note warn">${esc(p.error)}</div>`;
  else if (p.dry && p.dry.ok === false) body = `<div class="note warn">この操作はできません: ${esc(p.dry.reason)}</div>`;
  else if (p.dry && p.dry.changed === false) body = '<div class="note">変更はありません</div>';
  else {
    const st = p.dry.state || {};
    body = lanesHtml(p.dry.impact)
      + (st.confirmation_enabled ? `<span class="hint">この操作のあと、未確定の行は ${(st.unconfirmed_line_ids || []).length} 行になります</span>` : '');
  }
  const form = p.op === 'split' ? `<div class="field"><label for="m-pos">分ける位置（句読点に吸着）</label>
      <input id="m-pos" type="range" min="1" max="${Math.max(1, l.text.length - 1)}" value="${p.params.pos}">
      <div class="splitprev"><div><b>前半 ${esc(l.id)}</b><span id="pv-f">${esc(l.text.slice(0, p.params.pos))}</span></div>
      <div><b>後半（新しいサブ行）</b><span id="pv-b">${esc(l.text.slice(p.params.pos))}</span></div></div></div>` : '';
  const ok = !p.loading && !p.error && p.dry && p.dry.ok !== false && p.dry.changed !== false;
  return `<div class="confirm" id="m-confirm"><h3>「${name}」でこうなります（どのタブから押しても同じ）</h3>${form}${body}
    <div class="inline"><span class="hint">費用 0円（画像の生成は走りません）</span><span class="spacer"></span>
      <button class="btn ghost" data-act="cancelop">やめる</button><button class="btn primary" data-act="runop" ${ok ? '' : 'disabled'}>${name}を実行</button></div></div>`;
}

function drawModal() {
  if (S.drawer) { drawDrawer(); return; }
  const M_ = S.modal, l = M_ && byId(M_.id);
  if (!M_ || !l) { closeModal(); return; }
  const i = idx(l), seg = M.segments(l, S.enabled);
  const castOpts = S.cast.filter((c) => c.assignable);
  const secBody = {
    script: () => `
      <div class="field"><label for="m-text">本文</label><textarea id="m-text">${esc(l.text)}</textarea>
        <div class="inline"><button class="btn" data-op="text">本文を保存</button><span class="hint mono">${(l.text || '').length}字・推定${M.estSec(l.text)}秒</span></div></div>
      <div class="pair">
        <div class="box"><span class="flabel">話者（まれな操作）</span><div class="inline"><select id="m-spk">
          ${l.speaker_id ? '' : '<option value="">（未選択）</option>'}${castOpts.map((c) => `<option value="${esc(c.id)}" ${c.id === l.speaker_id ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select>
          <button class="btn" data-op="speaker">変更</button></div>
          ${M.groupOf(l, S.lines).length > 1 ? `<span class="hint">サブ行のグループ ${M.groupOf(l, S.lines).length} 行すべてが変わります</span>` : ''}</div>
        <div class="box"><span class="flabel">声の感情（TTSの演技にだけ効く）</span><div class="inline"><select id="m-emo">
          ${M.EMOTIONS.map((e) => `<option ${e === (l.emotion || 'neutral') ? 'selected' : ''}>${e}</option>`).join('')}</select>
          <button class="btn" data-op="emotion">変更</button></div></div></div>
      <div class="field"><span class="flabel">行の構造</span><div class="ops">
        ${opBtn('split', l, '✂ ここで分ける', '後半を新しいサブ行に')}${opBtn('merge', l, '⤓ 次の行と結合', '次の行の本文をつなぐ')}
        ${opBtn('split-apply', l, '✂ 自動で区切る', '句点・読点で最少分割')}
        ${opBtn('addsub', l, '＋ サブ行を追加', '同じ話者の続きを足す')}${opBtn('insert', l, '＋ 下に行を挿入', '別の話者の行を足す')}
        ${opBtn('up', l, '↑ 上へ移動', '1つ上と入れ替え')}${opBtn('down', l, '↓ 下へ移動', '1つ下と入れ替え')}
        ${opBtn('delete', l, '🗑 削除', '音声とコマは保管（元に戻せる）', 'danger')}</div></div>`,
    tts: () => `<div class="box"><div class="inline">${pill(seg.tts[0], seg.tts[1])}${l.tts.duration_sec ? `<span class="mono">${l.tts.duration_sec}s</span>` : ''}
        <span class="hint">声の感情: ${M.EMOJI[l.emotion] || ''} ${esc(l.emotion || 'neutral')}</span></div>
        <div class="inline">${l.tts.duration_sec ? `<button class="btn" data-act="play" data-id="${esc(l.id)}">▶ 再生</button>` : ''}
        <button class="btn" data-act="regen" data-id="${esc(l.id)}" ${l.text && (l.tts.state === 'none' || l.tts.state === 'stale') ? '' : 'disabled'}>🎙 この行を生成</button></div>
        <span class="note">変更した行は「✓ 確定」を押すと、音声が自動で作り直されます（ローカルGPU・無料）。エンジンが止まっている時は「作り直し待ち」のまま残り、起動後に「未生成・要再生成を生成」で作れます。</span></div>`,
    aroll: () => AR.modalHtml(l),
    final: () => FI.modalHtml(l),
  };
  const conf = M_.pending ? pendingHtml(l) : '';
  const rc = M.can('confirm', l, S.lines, S.enabled), sub = M.isSub(l);
  const confirmPill = !S.enabled ? pill('none', '運用外') : l.confirm === 'confirmed' ? pill('ok', '確定済み') : l.confirm === 'unconfirmed' ? pill('warn', '未確定') : pill('none', '空の行');
  modal.innerHTML = `
    <div class="mhead"><div class="who"><span class="ord mono">${sub ? '↳ サブ行 ' : '第'}${l.order}${sub ? '' : '行'}</span><span class="spk ${M.speakerClass(l, S.cast)}">${esc(speakerName(l))}</span>
      <span class="id mono">${esc(l.id)}</span>${confirmPill}</div>
      <span class="spacer"></span>
      <button class="btn ghost" data-act="nav" data-d="-1" ${i ? '' : 'disabled'} title="前の行（←）">←</button>
      <button class="btn ghost" data-act="nav" data-d="1" ${i < S.lines.length - 1 ? '' : 'disabled'} title="次の行（→）">→</button>
      <button class="btn ok" data-act="confirm" data-id="${esc(l.id)}" ${rc ? `disabled title="${esc(rc)}"` : ''}>✓ この行を確定</button>
      <button class="btn ghost" data-act="close" aria-label="閉じる">✕</button></div>
    <div class="mtabs" role="tablist">${MSEC.map(([k, n]) => `<button role="tab" data-sec="${k}" aria-selected="${M_.section === k}"><span class="dot ${seg[k][0]}"></span>${n}</button>`).join('')}</div>
    <div class="mbody">${conf}${secBody[M_.section]()}</div>
    <div class="mfoot"><span class="hint">← → で前後の行 ／ Esc で閉じる ／ Ctrl+Z で元に戻す</span></div>`;
  const pos = modal.querySelector('#m-pos');
  if (pos) pos.addEventListener('input', (e) => {
    M_.pending.params.pos = M.snap(l.text, +e.target.value);
    M_.pending.api.body.position = M_.pending.params.pos;
    modal.querySelector('#pv-f').textContent = l.text.slice(0, M_.pending.params.pos);
    modal.querySelector('#pv-b').textContent = l.text.slice(M_.pending.params.pos);
  });
  if (M_.pending) { const c = modal.querySelector('#m-confirm'); if (c && c.scrollIntoView) c.scrollIntoView({ block: 'nearest' }); }
}

// ── 行の操作（確認欄＝dry_run → 実行） ───────────────────────
async function startOp(op) {
  const l = byId(S.modal.id);
  let p = {};
  if (op === 'text') { const v = modal.querySelector('#m-text').value; if (v === l.text) return toast('本文は変わっていません'); p = { text: v }; }
  else if (op === 'emotion') { const v = modal.querySelector('#m-emo').value; if (v === (l.emotion || 'neutral')) return toast('感情は変わっていません'); p = { emotion: v }; }
  else if (op === 'speaker') {
    const v = modal.querySelector('#m-spk').value;
    if (!v) return toast('話者を選んでください');
    if (v === l.speaker_id) return toast('話者は変わっていません');
    p = { speaker_id: v, speaker_name: (S.cast.find((c) => c.id === v) || {}).name };
  } else if (op === 'split') p = { pos: M.snap(l.text, Math.floor(l.text.length / 2)) };
  const a = M.toApi(op, l, p);
  const pending = { op, params: p, api: { apiOp: a.op, body: a.body }, dry: null, loading: true, error: '' };
  S.modal.pending = pending;
  drawModal();
  try { pending.dry = await api.lineOp(S.pid, S.ep, a.op, a.body, { dryRun: true }); } catch (e) { pending.error = e.message; }
  pending.loading = false;
  if (S.modal && S.modal.pending === pending) drawModal();        // 閉じた・別の操作に替えた後の返事は捨てる
}

async function runOp() {
  const M_ = S.modal, p = M_.pending, l = byId(M_.id);
  if (!p || p.loading || S.working) return;
  const focus = (res) => M.focusAfter(p.op, l, S.lines, res);
  await working('実行しています…', async () => {
    const res = await api.lineOp(S.pid, S.ep, p.api.apiOp, p.api.body);
    const target = focus(res);           // 再読込の前に、モーダルを合わせる先を決める（削除で行が消えても落ちない）
    M_.pending = null; M_.edited = false;
    if (target) M_.id = target;
    await load();
    if (!res.changed) { toast('変更はありませんでした'); if (S.modal) drawModal(); return; }
    if (!target || !byId(target)) closeModal(); else if (S.modal) { drawModal(); flash(target); }
    notify(res.warnings);
    toast(`${M.OP_NAMES[p.op]}：台本・音声・コマに反映しました${res.warnings.length ? '（注意あり）' : ''}`);
  });
}

function notify(warnings) {
  S.notice = warnings || [];
  renderBanner();
}

function flash(id) {
  const el = $(`r-${id}`);
  if (el) { el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); if (el.scrollIntoView) el.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
}

// ── 確定・Undo・確定の運用の開始 ─────────────────────────────
async function confirmLines(ids) {
  if (!S.enabled) return toast('確定の運用が始まっていません', { bad: true });
  await working('確定しています…（プロンプトの用意などで数分かかることがあります）', async () => {
    const res = await api.confirm(S.pid, S.ep, ids);
    await load();
    notify(res.warnings);
    const q = (res.applied && res.applied.tts && res.applied.tts.queued) || [];
    if (q.length) { S.queued = new Set(q); startFollow(); }
    toast(res.confirmed.length ? `${res.confirmed.length}行を確定：${q.length ? `音声 ${q.length}行を作り直し中・` : ''}再合成の対象に入りました${res.warnings.length ? '（注意あり）' : ''}`
      : '確定する行はありませんでした');
    if (S.modal) drawModal();
  });
}

async function startConfirmations() {
  if (!confirm('この話数の「確定の運用」を始めます。\n\n今の台本の全行を「確定済み」として記録します。以後は、直した行だけが「未確定」になり、確定すると音声が作り直され、一括の組版は確定済みの行だけが対象になります。\n\n始めますか？')) return;
  await working('始めています…', async () => {
    await api.startConfirmations(S.pid, S.ep);
    await load();
    toast('確定の運用を始めました（今の台本は全行確定済みです）');
  });
}

async function undo() {
  if (!S.view || !S.view.undoable || S.working) return;
  await working('元に戻しています…', async () => {
    let res;
    try { res = await api.undo(S.pid, S.ep); } catch (e) {
      if (e.status === 409 && confirm(`${e.message}\n\n強制的に戻しますか？`)) res = await api.undo(S.pid, S.ep, { force: true });
      else throw e;
    }
    await load();
    notify(res.warnings);
    if (S.modal) { S.modal.pending = null; if (!byId(S.modal.id)) closeModal(); else drawModal(); }
    toast('台本の変更を1つ戻しました（音声とコマも一緒に戻ります）');
  });
}

// ── 音声（生成・見守り・再生） ────────────────────────────────
async function generate(ids) {
  await working('音声の作り直しを頼んでいます…', async () => {
    const res = await api.runLines(S.pid, S.ep, ids);
    if (res.unassigned && res.unassigned.length) toast(`声が未割当の行があります（${res.unassigned.length}行）。キャラタブで配役してください`, { bad: true });
    else if (!res.queued.length) toast('作り直す行はありません（音声は最新です）');
    else toast(`${res.queued.length}行の音声を作り直しています`);
    if (res.queued.length) { S.queued = new Set(res.queued); startFollow(); }
    await load();
  });
}

function startFollow() {
  if (S.poll) return;
  S.idle = 0;
  S.poll = setInterval(followTick, 2500);
}
function stopFollow() { clearInterval(S.poll); S.poll = null; }
async function followTick() {
  try { await load(); } catch { return; }
  const running = !!(S.view.tts_progress && S.view.tts_progress.running);
  const pending = [...S.queued].filter((id) => { const l = byId(id); return l && ['stale', 'none', 'queued'].includes(l.tts.state); });
  const d = M.pollDecision({ running, pending, idlePolls: S.idle });
  S.idle = d.idlePolls;
  if (d.action === 'continue') return;
  stopFollow();
  const had = S.queued.size;
  S.queued = new Set();
  if (d.action === 'stuck') toast('音声を作れませんでした（エンジンが止まっている可能性）。起動したら「未生成・要再生成を生成」で作れます', { bad: true, ms: 9000 });
  else if (had) toast('音声の作り直しが終わりました');
}

const playlist = () => S.lines.filter((l) => l.tts.duration_sec).map((l) => ({ id: l.id, url: api.audioUrl(S.pid, S.ep, l.id, S.ver) }));
const player = new Player({
  createAudio: (url) => new Audio(url),
  getItems: playlist,
  onChange: () => { renderFloat(); markPlaying(); },
  storage, storageKey: `wb-last-${S.pid}-${S.ep}`,
});

function markPlaying() {
  document.querySelectorAll('.row.playing').forEach((el) => el.classList.remove('playing'));
  if (player.id) { const el = $(`r-${player.id}`); if (el) el.classList.add('playing'); }
}

function renderFloat() {
  const bar = $('floatbar'), items = playlist();
  const show = S.tab === 'tts' && items.length > 0;
  bar.hidden = !show;
  document.body.classList.toggle('has-floatbar', show);
  if (!show) return;
  const playing = player.status !== 'idle';
  bar.innerHTML = playing
    ? `<button class="btn ghost" data-act="pprev" title="前の行">⏮</button>
       <button class="btn ghost" data-act="ptoggle" title="一時停止／再開（Space）">${player.status === 'playing' ? '⏸' : '▶'}</button>
       <button class="btn ghost" data-act="pnext" title="次の行">⏭</button>
       <button class="btn ghost" data-act="pstop" title="停止">⏹</button>
       <span class="lbl mono">${player.index + 1}/${player.total}</span>
       <button class="btn ghost" data-act="pjump" title="再生中の行へスクロール">🎯</button>`
    : `<button class="btn primary" data-act="playall">▶ 全行再生</button>
       ${player.last && items.some((x) => x.id === player.last) ? '<button class="btn" data-act="pcontinue" title="最後に鳴らした行から（Space）">⏯ 続きから</button>' : ''}
       <span class="lbl mono">${items.length}行</span>`;
}

// ── LLMの案（差分 → 採用） ───────────────────────────────────
async function openProposal() {
  S.modal = null;
  S.drawer = { loading: true, error: '', proposal: null, selected: new Set(), ack: false, pending: null };
  scrim.hidden = false;
  drawDrawer();
  try {
    const p = await api.proposal(S.pid, S.ep);
    S.drawer.proposal = p;
    S.drawer.selected = new Set(p.lines.map((r) => r.line_id));
  } catch (e) { S.drawer.error = e.message; }
  S.drawer.loading = false;
  drawDrawer();
}

const CHANGE = { changed: ['warn', '変更'], new: ['ok', '新規'], removed: ['bad', '削除'] };
function drawDrawer() {
  const D = S.drawer;
  if (!D) return;
  const p = D.proposal;
  let body;
  if (D.loading) body = '<div class="hint">読み込んでいます…</div>';
  else if (D.error) body = `<div class="note warn">${esc(D.error)}</div>`;
  else if (p.kind === 'none') body = '<div class="note">LLMの案はありません（ドラフトと正本は同じです）</div>';
  else {
    const head = { initial: '正本がまだありません。ドラフトを最初の台本として採用します（この採用は元に戻せません）。',
      lines: `LLMが書き直した行の差分です（変更 ${p.counts.changed}・新規 ${p.counts.new}・削除 ${p.counts.removed}）。採用する行を選んでください。採用した行は「未確定」になります。`,
      replace: '全文の再生成の案です。行の並びと内容がまるごと入れ替わります。' }[p.kind];
    const warn = (p.warnings || []).map((w) => `<div class="note warn">${esc(w)}</div>`).join('');
    const rows = p.kind === 'lines' ? p.lines.map((r) => {
      const [cls, lbl] = CHANGE[r.change] || ['none', r.change];
      const b = r.before || {}, a = r.after || {};
      const fields = (r.fields || []).filter((f) => f !== 'text').map((f) => `<span class="chip">${esc(f)}: ${esc(b[f] ?? '—')} → ${esc(a[f] ?? '—')}</span>`).join('');
      return `<label class="diffrow"><input type="checkbox" data-sel="${esc(r.line_id)}" ${D.selected.has(r.line_id) ? 'checked' : ''}>
        <div><div class="head"><span class="mono">${esc(r.line_id)}</span>${pill(cls, lbl)}${fields}</div>
        ${r.change !== 'new' ? `<div class="old">${esc(b.text || '')}</div>` : ''}${r.change !== 'removed' ? `<div class="new">${esc(a.text || '')}</div>` : ''}</div></label>`;
    }).join('') : '';
    const ack = p.kind === 'replace' ? `<label class="inline"><input type="checkbox" data-ack ${D.ack ? 'checked' : ''}>
      <span>音声・絵の紐付けがすべて切れることを理解した</span></label>` : '';
    body = `<div class="note">${esc(head)}</div>${warn}${rows}${ack}`;
  }
  const dp = D.pending;
  const conf = dp ? `<div class="confirm"><h3>「LLMの案を採用」でこうなります</h3>${
    dp.loading ? '<div class="hint">影響を調べています…</div>'
      : dp.error ? `<div class="note warn">${esc(dp.error)}</div>`
      : dp.dry.ok === false ? `<div class="note warn">採用できません: ${esc(dp.dry.reason)}</div>`
      : `${lanesHtml(dp.dry.impact)}${dp.dry.starts_confirmations ? '<div class="note">この話数は採用で「確定の運用」が始まります。採用した行だけが未確定になります。</div>' : ''}`
  }<div class="inline"><span class="hint">費用 0円</span><span class="spacer"></span>
      <button class="btn ghost" data-act="adopt-cancel">やめる</button>
      <button class="btn primary" data-act="adopt-run" ${dp.loading || dp.error || dp.dry.ok === false || dp.dry.changed === false ? 'disabled' : ''}>採用を実行</button></div></div>` : '';
  const can = p && p.kind !== 'none' && !D.loading && !D.error && (p.kind === 'lines' ? D.selected.size > 0 : p.kind === 'initial' || D.ack);
  modal.innerHTML = `
    <div class="mhead"><div class="who"><b>LLMの案（ドラフト → 正本）</b></div><span class="spacer"></span>
      <button class="btn primary" data-act="adopt-check" ${can && !dp ? '' : 'disabled'}>採用の内容を確認</button>
      <button class="btn ghost" data-act="close" aria-label="閉じる">✕</button></div>
    <div class="mbody">${conf}${body}</div>
    <div class="mfoot"><span class="hint">採用も「元に戻す」で戻せます（最初の採用を除く）。ドラフトは残るので、もう一度採用できます。</span></div>`;
}

function adoptBody() {
  const D = S.drawer, p = D.proposal;
  if (p.kind === 'replace') return { replace_all: true };
  if (p.kind === 'initial') return {};
  return D.selected.size === p.lines.length ? {} : { line_ids: [...D.selected] };
}

async function adoptCheck() {
  const D = S.drawer;
  const pending = { dry: null, loading: true, error: '' };
  D.pending = pending;
  drawDrawer();
  try { pending.dry = await api.adopt(S.pid, S.ep, adoptBody(), { dryRun: true }); } catch (e) { pending.error = e.message; }
  pending.loading = false;
  if (S.drawer && S.drawer.pending === pending) drawDrawer();
}

async function adoptRun() {
  const D = S.drawer;
  await working('採用しています…', async () => {
    const res = await api.adopt(S.pid, S.ep, adoptBody());
    closeModal();
    await load();
    notify(res.warnings);
    const n = res.adoption ? (res.adoption.adopted || []).length + (res.adoption.inserted || []).length + (res.adoption.removed || []).length : 0;
    toast(res.changed ? `LLMの案を採用しました${n ? `（${n}行）` : ''}。採用した行は「未確定」です` : '採用する差はありませんでした');
  });
}

// ── イベント ─────────────────────────────────────────────
document.addEventListener('click', (e) => {
  const ab = e.target.closest('[data-a]');                // 絵タブ（aroll_ui.js）・仕上がりタブ（final_ui.js＝f- 始まり）の操作
  if (ab && !ab.matches('input,select,textarea,details')) { if (!ab.disabled) (ab.dataset.a.startsWith('f-') ? FI : AR).click(ab); return; }
  if (e.target.closest('input,select,textarea,label,summary')) return;   // 行の選択・設定の操作で行モーダルを開かない
  const b = e.target.closest('[data-act],[data-op],[data-tab],[data-sec],[data-open]');
  if (!b) { if (e.target === scrim) closeModal(); return; }
  if (b.dataset.tab) { if (b.disabled) return; S.tab = b.dataset.tab; store.set('wb-tab', S.tab); render(); if (S.tab === 'aroll') AR.enter(); else if (S.tab === 'final') FI.enter(); return; }
  if (b.dataset.sec) { S.modal.section = b.dataset.sec; S.modal.pending = null; drawModal(); prepareSection(); return; }
  if (b.dataset.op) { if (!b.disabled) startOp(b.dataset.op); return; }
  if (b.dataset.open && !e.target.closest('button')) { openModal(b.dataset.open); return; }
  const a = b.dataset.act, id = b.dataset.id;
  if (b.disabled) return;
  const act = {
    close: closeModal,
    nav: () => { const l = byId(S.modal.id), n = S.lines[idx(l) + +b.dataset.d]; if (n) { S.modal.id = n.id; S.modal.pending = null; S.modal.edited = false; drawModal(); flash(n.id); } },
    cancelop: () => { S.modal.pending = null; drawModal(); },
    runop: runOp,
    confirm: () => confirmLines([id]),
    confirmall: () => confirmLines(null),
    startconf: startConfirmations,
    autosplit: startAutoSplit,
    tcancel: () => { S.tpending = null; renderToolbar(); },
    trun: runAutoSplit,
    filter: () => { S.filter = b.dataset.f; render(); },
    undo,
    genall: () => generate(M.audioTodo(S.lines).map((l) => l.id)),
    regen: () => generate([id]),
    play: () => player.playFrom(id),
    playall: () => player.playAll(),
    pcontinue: () => player.continueFromLast(),
    ptoggle: () => player.toggle(),
    pnext: () => player.next(),
    pprev: () => player.prev(),
    pstop: () => player.stop(),
    pjump: () => flash(player.id),
    proposal: openProposal,
    'adopt-check': adoptCheck,
    'adopt-cancel': () => { S.drawer.pending = null; drawDrawer(); },
    'adopt-run': adoptRun,
    'dismiss-notice': () => notify([]),
  }[a];
  if (act) act();
});

document.addEventListener('change', (e) => {
  if (e.target.dataset && e.target.dataset.a) { (e.target.dataset.a.startsWith('f-') ? FI : AR).change(e.target); return; }
  if (e.target.dataset && e.target.dataset.sel && S.drawer) {
    e.target.checked ? S.drawer.selected.add(e.target.dataset.sel) : S.drawer.selected.delete(e.target.dataset.sel);
    S.drawer.pending = null; drawDrawer();
  } else if (e.target.dataset && 'ack' in e.target.dataset && S.drawer) { S.drawer.ack = e.target.checked; S.drawer.pending = null; drawDrawer(); }
});

// モーダルの入力を触ったら、ポーリングの再描画で入力中の本文を消さない
document.addEventListener('input', (e) => {
  if (e.target.dataset && e.target.dataset.a && !e.target.dataset.a.startsWith('f-')) AR.input(e.target);
  if (S.modal && modal.contains(e.target)) S.modal.edited = true;
});
document.addEventListener('toggle', (e) => { if (e.target.dataset && e.target.dataset.a) (e.target.dataset.a.startsWith('f-') ? FI : AR).toggle(e.target); }, true);

document.addEventListener('keydown', (e) => {
  const typing = /TEXTAREA|INPUT|SELECT/.test((document.activeElement || {}).tagName || '');
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z' && !typing) { e.preventDefault(); undo(); return; }
  if (!S.modal && !S.drawer) {
    if (e.key === 'Enter' && e.target.dataset && e.target.dataset.open) openModal(e.target.dataset.open);
    else if (e.key === ' ' && S.tab === 'tts' && !typing && !/^(BUTTON|A)$/.test((e.target || {}).tagName || '') && playlist().length) { e.preventDefault(); player.toggle(); }
    return;
  }
  if (e.key === 'Escape') {
    if (S.drawer) { if (S.drawer.pending) { S.drawer.pending = null; drawDrawer(); } else closeModal(); }
    else if (S.modal.pending) { S.modal.pending = null; drawModal(); } else closeModal();
  } else if (S.modal && !typing && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    const l = byId(S.modal.id), n = S.lines[idx(l) + (e.key === 'ArrowLeft' ? -1 : 1)];
    if (n) { e.preventDefault(); S.modal.id = n.id; S.modal.pending = null; S.modal.edited = false; drawModal(); flash(n.id); }
  }
});

// ── 起動 ─────────────────────────────────────────────────
async function picker() {
  $('title').textContent = '行ワークベンチ';
  $('subtitle').textContent = 'director で台本を作ったあと、1話ずつここで仕上げます。話数を選んでください。';
  ['health', 'tabs', 'toolbar', 'list'].forEach((id) => { $(id).innerHTML = ''; });
  document.querySelector('.legend').hidden = true;
  $('undo').hidden = true;
  const box = $('list');
  box.className = 'picker';
  try {
    if (!S.pid) {
      const { projects } = await api.projects();
      box.innerHTML = (projects || []).map((p) => `<a class="pitem" href="?project=${encodeURIComponent(p.id)}"><span class="t">${esc(p.title)}</span><span class="mono hint">${esc(p.id)}</span><span class="hint">${(p.episodes || []).length}話</span></a>`).join('')
        || '<div class="note">プロジェクトがありません</div>';
    } else {
      const { episodes } = await api.episodes(S.pid);
      $('subtitle').textContent = `${S.pid} の話数を選んでください`;
      box.innerHTML = (episodes || []).map((e) => `<a class="pitem" href="?project=${encodeURIComponent(S.pid)}&episode=${e.number}">
        <span class="t">第${e.number}話</span><span>${esc(e.title || '')}</span><span class="hint">${e.line_count || 0}行</span>
        ${e.confirmation && e.confirmation.enabled ? (e.confirmation.unconfirmed ? pill('warn', `未確定 ${e.confirmation.unconfirmed}`) : pill('ok', '確定済み')) : pill('none', '確定の運用 未開始')}
        ${e.proposal_pending ? pill('warn', 'LLMの案あり') : ''}</a>`).join('') || '<div class="note">話数がありません</div>';
    }
  } catch (e) { fail(e); }
}

(async function boot() {
  if (!S.pid || !S.ep) { await picker(); return; }
  $('back').href = '/';
  try { await load(); if (S.tab === 'aroll') AR.enter(); else if (S.tab === 'final') FI.enter(); } catch (e) {
    $('banner').hidden = false;
    $('banner').innerHTML = `<div class="banner bad">この話数を読み込めませんでした: ${esc(e.message)}</div>`;
  }
})();
