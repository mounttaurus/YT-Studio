// 「🎬 A/Bロール」タブの描画と操作（Docs/BROLL_PLAN.md §5・B2）。
// 左: 行の表（セリフ・A=完成した絵・B=割り当てたBロール）。右: 素材の箱（引き出し）。
// 当て方は2つ: ①箱の素材を行へドラッグ（または行の「＋」から選ぶ）②行へファイルを直接ドロップ（登録してその行に当てる）。
// 書くのは scrapping-agent だけ（BR3）＝ここは API を呼ぶだけ。行の中の秒・範囲は持たない（B-D3）。
// 4工程の帯（台本・音声・絵・仕上がり）には足さない（Bロールが無い行は正常で、要対応ではない）。
import * as B_ from './broll_model.js';
import * as M from './model.js';

export function createBrollUi(ctx) {
  const { S, api, esc, toast, working, byId, pill, redrawModal } = ctx;
  const store = (() => { try { return window.localStorage; } catch { return null; } })();
  const saved = (k, d) => { try { return (store && store.getItem(k)) || d; } catch { return d; } };
  const B = {
    box: null, loading: false, error: '', filter: 'all',
    license: saved('wb-broll-license', 'unknown'), credit: '',
    d: { kind: 'all', source: 'all', unassigned: false },       // 引き出しの絞り込み
    pick: null,            // 「＋」で素材を選んでいる行
    detail: null,          // 引き出しで詳細を開いている素材
    draft: null,           // 詳細の入力中の値 {license, credit, note}
    orphTo: {},            // 行から外れた素材 → 当て直す先の行（`${asset}|${from}`）
    drag: null,            // ドラッグ中 {asset, from}
  };
  const pid = () => S.pid;
  const ep = () => S.ep;
  const lines = () => S.lines;
  const box = () => B.box || B_.boxOf(null);
  const lineName = (id) => { const l = byId(id); return l ? `${l.order}行目` : id; };

  // ── 読み込み ─────────────────────────────────────────────
  async function loadBox() {
    B.loading = true;
    try { B.box = B_.boxOf(await api.broll.get(pid(), ep())); B.error = ''; } catch (e) { B.error = e.message || String(e); }
    B.loading = false;
    if (B.detail && !(B.box.assets || []).some((a) => a.asset_id === B.detail)) { B.detail = null; B.draft = null; }
    ctx.rerender(); redrawModal();
  }
  async function enter() { if (!B.box) await loadBox(); else loadBox(); }
  const hasBusyInput = () => { const a = document.activeElement; return !!(a && a.dataset && a.dataset.a && a.dataset.a.startsWith('b-') && /INPUT|TEXTAREA|SELECT/.test(a.tagName)); };

  // ── 部品 ─────────────────────────────────────────────────
  /** 素材の縮小表示。写真は縮小JPEG、動画・モーションは <video> の先頭フレーム（サーバーで作らない）。 */
  function mediaHtml(a, { big = false } = {}) {
    const kind = B_.kindOf(a);
    if (kind === 'photo') return `<img src="${esc(big ? B_.fileUrl(pid(), ep(), a) : B_.thumbUrl(pid(), ep(), a, 320))}" loading="lazy" alt="" draggable="false">`;
    const t = a.poster_sec ? `#t=${a.poster_sec}` : '#t=0.1';
    return `<video src="${esc(B_.fileUrl(pid(), ep(), a))}${t}" ${big ? 'controls' : 'muted'} preload="metadata" playsinline draggable="false"></video>`;
  }
  const badge = (a) => {
    const k = B_.kindOf(a), d = B_.durationLabel(a.duration_sec);
    return `<span class="bk">${k === 'motion' ? '🎬' : k === 'video' ? '▶' : ''}${d ? ` ${esc(d)}` : ''}</span>`;
  };
  const assetTitle = (a) => `${(a.source && a.source.original_name) || a.asset_id}\n${B_.KIND_LABEL[B_.kindOf(a)]}・${B_.licenseName(a.license)}`;

  /** 行に当たっている素材の並び（行の表・行モーダルで同じ）。 */
  function chipsHtml(l) {
    const items = B_.placedOf(box(), l.id);
    if (!items.length) return '<span class="hint">なし</span>';
    return items.map((it, i) => `<div class="bchip" draggable="true" data-b-asset="${esc(it.asset.asset_id)}" data-b-line="${esc(l.id)}" title="${esc(assetTitle(it.asset))}">
      <div class="bmedia" data-a="b-detail" data-asset="${esc(it.asset.asset_id)}">${mediaHtml(it.asset)}${badge(it.asset)}</div>
      <div class="bctl">${i > 0 ? `<button data-a="b-move" data-line="${esc(l.id)}" data-asset="${esc(it.asset.asset_id)}" data-d="-1" title="1つ前へ">◀</button>` : ''}
        ${i < items.length - 1 ? `<button data-a="b-move" data-line="${esc(l.id)}" data-asset="${esc(it.asset.asset_id)}" data-d="1" title="1つ後ろへ">▶</button>` : ''}
        <button data-a="b-unassign" data-line="${esc(l.id)}" data-asset="${esc(it.asset.asset_id)}" title="この行から外す（箱には残ります）">×</button></div></div>`).join('');
  }

  /** 「＋」で開く選び場。未割り当てを先に、すでにこの行にあるものは除く。 */
  function pickerHtml(l) {
    const on = new Set(B_.placedOf(box(), l.id).map((it) => it.asset.asset_id));
    const placed = new Set(Object.values(box().placements || {}).flat().map((it) => it.asset_id));
    const list = B_.drawerAssets(box()).filter((a) => !on.has(a.asset_id)).sort((a, b) => Number(placed.has(a.asset_id)) - Number(placed.has(b.asset_id)));
    return `<div class="bpick"><div class="inline"><b>この行に当てる素材</b>
      <label class="btn" title="ファイルを登録して、この行に当てます（上で選んだライセンスで入ります）">ファイルを選ぶ…
        <input type="file" multiple hidden data-a="b-file" data-line="${esc(l.id)}" accept="image/png,image/jpeg,image/webp,video/mp4,video/quicktime,video/webm"></label>
      <button class="btn ghost" data-a="b-pick-close">閉じる</button></div>
      ${list.length ? `<div class="btiles">${list.map((a) => `<button class="btile" data-a="b-assign" data-line="${esc(l.id)}" data-asset="${esc(a.asset_id)}" title="${esc(assetTitle(a))}">
        <span class="bmedia">${mediaHtml(a)}${badge(a)}</span>${placed.has(a.asset_id) ? '' : '<span class="bnew">未割り当て</span>'}</button>`).join('')}</div>`
    : '<div class="hint">箱に素材がありません。ファイルを選ぶか、ドロップしてください。</div>'}</div>`;
  }

  // ── 表 ───────────────────────────────────────────────────
  function rowHtml(l) {
    const a = B_.aPicture(l), sub = l.parent_line_id && l.parent_line_id !== l.id;
    return `<div class="brow" id="r-${esc(l.id)}" data-open="${esc(l.id)}" data-b-drop="line" data-b-line="${esc(l.id)}">
      <div class="bmeta"><span class="ord mono">${sub ? '↳' : ''}${l.order}</span><span class="spk ${M.speakerClass(l, S.cast)}">${esc(l.speaker_name || '話者未選択')}</span></div>
      <div class="bbody"><div class="text">${esc(l.text) || '<span class="hint">（本文なし）</span>'}</div></div>
      <div class="bA" title="${a ? esc(a.label) : 'まだ絵がありません'}">${a ? `<img src="${esc(a.url)}" loading="lazy" alt=""><span class="bk">${esc(a.label)}</span>` : '<span class="none">—</span>'}</div>
      <div class="bB"><div class="bchips">${chipsHtml(l)}</div>
        <button class="btn" data-a="b-pick" data-line="${esc(l.id)}" aria-expanded="${B.pick === l.id}" title="箱の素材を選ぶ・ファイルを入れる">＋</button></div>
      ${B.pick === l.id ? pickerHtml(l) : ''}</div>`;
  }

  function orphansHtml() {
    const items = B_.orphanItems(box());
    if (!items.length) return '';
    const opts = (cur) => lines().map((l) => `<option value="${esc(l.id)}" ${l.id === cur ? 'selected' : ''}>${l.order}行目 ${esc((l.text || '').slice(0, 18))}</option>`).join('');
    return `<div class="borph"><h3>行から外れたBロール ${items.length}件</h3>
      <div class="hint">台本の結合・削除で行が無くなった割り当てです（消えていません。台本を元に戻すと戻ります）。別の行へ当て直すか、外してください。</div>
      ${items.map((o) => { const key = `${o.asset_id}|${o.line_id}`; const to = B.orphTo[key] || (lines()[0] || {}).id || '';
        return `<div class="borphrow"><span class="bmedia">${mediaHtml(o.asset)}${badge(o.asset)}</span>
          <div class="borphinfo"><span class="mono">${esc(o.asset_id)}</span><span class="hint">元の行 ${esc(o.line_id)}${o.line_text_hint ? ` 「${esc(o.line_text_hint)}」` : ''}</span></div>
          <select data-a="b-orph-line" data-key="${esc(key)}" aria-label="当て直す先の行">${opts(to)}</select>
          <button class="btn primary" data-a="b-orph-assign" data-key="${esc(key)}" data-asset="${esc(o.asset_id)}" data-from="${esc(o.line_id)}">当て直す</button>
          <button class="btn" data-a="b-orph-drop" data-asset="${esc(o.asset_id)}" data-from="${esc(o.line_id)}" title="割り当てを外します（素材は箱に残ります）">外す</button></div>`; }).join('')}</div>`;
  }

  // ── 引き出し（素材の箱） ──────────────────────────────────
  function detailHtml() {
    const a = (box().assets || []).find((x) => x.asset_id === B.detail);
    if (!a) return '';
    const d = B.draft || { license: a.license, credit: a.credit || '', note: a.note || '' };
    const used = B_.usedBy(box(), a), s = a.source || {};
    const lic = B_.LICENSES.map(([v, name]) => `<option value="${esc(v)}" ${v === d.license ? 'selected' : ''}>${esc(name)}</option>`).join('');
    return `<div class="bdetail"><div class="inline"><b>素材の詳細</b><span class="spacer"></span><button class="btn ghost" data-a="b-detail-close" aria-label="詳細を閉じる">✕</button></div>
      <div class="bbig">${mediaHtml(a, { big: true })}</div>
      <div class="hint mono">${esc(a.asset_id)}</div>
      <div class="hint">${esc(B_.KIND_LABEL[B_.kindOf(a)])}${a.resolution ? `・${esc(a.resolution)}` : ''}${a.duration_sec ? `・${esc(B_.durationLabel(a.duration_sec))}` : ''}
        ・${esc(B_.SOURCE_LABEL[s.kind] || s.kind || '出典なし')}${s.author ? `・${esc(s.author)}` : ''}${s.original_name ? `・${esc(s.original_name)}` : ''}</div>
      ${s.url ? `<div class="hint"><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">出典のページ</a></div>` : ''}
      ${a.derived_from && a.derived_from.length ? `<div class="hint">使った素材: ${a.derived_from.map((x) => `<span class="mono">${esc(x)}</span>`).join('、')}</div>` : ''}
      ${a.motion ? '<div class="hint">🎬 作り直せる元（コンポジションの写し）を持っています</div>' : ''}
      <div class="field"><label>ライセンス</label><select id="b-d-license" data-a="b-draft">${lic}</select></div>
      <div class="field"><label>クレジット（作者・出典）</label><input id="b-d-credit" data-a="b-draft" value="${esc(d.credit)}" placeholder="例: Photo by ○○ / Wikimedia Commons"></div>
      <div class="field"><label>メモ</label><input id="b-d-note" data-a="b-draft" value="${esc(d.note)}"></div>
      <div class="inline"><button class="btn primary" data-a="b-save" data-asset="${esc(a.asset_id)}">保存</button>
        <button class="btn danger" data-a="b-trash" data-asset="${esc(a.asset_id)}" ${used.length ? 'disabled' : ''} title="${used.length ? 'どこかの行に当たっている間は外せません（先に行から外す）' : '箱から外します（_trash へ移すだけ・消えません）'}">箱から外す</button></div>
      <div class="hint">${used.length ? `当たっている行: ${used.map((id) => `<button class="btn ghost" data-a="b-goto" data-line="${esc(id)}">${esc(lineName(id))}</button>`).join('')}` : 'どの行にも当たっていません'}</div></div>`;
  }

  function drawerHtml() {
    const bx = box(), list = B_.drawerAssets(bx, B.d), sources = B_.sourcesIn(bx);
    const sum = B_.summaryOf(lines(), bx);
    return `<aside class="bdrawer" data-b-drop="drawer" aria-label="素材の箱">
      <div class="inline"><b>素材の箱</b><span class="hint">${sum.assets}件</span><span class="spacer"></span>
        <label class="btn" title="ファイルを箱に入れます（どの行にも当てません）">ファイルを選ぶ…
          <input type="file" multiple hidden data-a="b-file" accept="image/png,image/jpeg,image/webp,video/mp4,video/quicktime,video/webm"></label></div>
      <div class="inline">${B_.DRAWER_KINDS.map(([k, n]) => `<button class="btn ghost" data-a="b-dkind" data-k="${k}" aria-pressed="${B.d.kind === k}">${n}</button>`).join('')}</div>
      <div class="inline"><label class="chk"><input type="checkbox" data-a="b-dunassigned" ${B.d.unassigned ? 'checked' : ''}> 未割り当てだけ</label>
        ${sources.length > 1 ? `<select data-a="b-dsource" aria-label="出典で絞る"><option value="all">出典: すべて</option>${sources.map((k) => `<option value="${esc(k)}" ${B.d.source === k ? 'selected' : ''}>${esc(B_.SOURCE_LABEL[k] || k)}</option>`).join('')}</select>` : ''}</div>
      ${B.detail ? detailHtml() : ''}
      <div class="btiles">${list.map((a) => {
    const used = B_.usedBy(bx, a);
    return `<div class="btile ${B.detail === a.asset_id ? 'sel' : ''}" draggable="true" data-b-asset="${esc(a.asset_id)}" data-a="b-detail" data-asset="${esc(a.asset_id)}" tabindex="0" title="${esc(assetTitle(a))}">
        <span class="bmedia">${mediaHtml(a)}${badge(a)}</span>
        ${used.length ? `<span class="bused">${used.length}行</span>` : '<span class="bnew">未割り当て</span>'}
        ${a.license === 'unknown' ? '<span class="bwarn" title="ライセンスが不明です（詳細で設定してください）">⚠</span>' : ''}</div>`;
  }).join('') || `<div class="hint">${(bx.assets || []).length ? 'この条件の素材はありません' : '素材がありません。ファイルを選ぶか、ここへドロップしてください。'}</div>`}</div>
      <div class="hint">素材を行へドラッグすると当たります。行に当てた素材をここへドラッグすると外れます（箱には残る）。</div></aside>`;
  }

  // ── タブ全体 ─────────────────────────────────────────────
  function toolbarHtml() {
    if (!lines().length) return '';
    const sum = B_.summaryOf(lines(), box()), cur = B.filter;
    const need = B_.needsCredit(B.license);
    return `<div class="abar">
      <div class="arow"><b class="alabel">A/Bロール</b>
        ${pill('info', `Bロールのある行 ${sum.rows}/${sum.lines}`)}${pill('none', `箱 ${sum.assets}件`)}
        ${sum.unassigned ? pill('warn', `未割り当て ${sum.unassigned}`) : ''}${sum.orphaned ? pill('bad', `行から外れた ${sum.orphaned}`) : ''}
        ${sum.unknown ? pill('warn', `出典・ライセンス不明 ${sum.unknown}`) : ''}
        <span class="spacer"></span>
        <div class="filter" role="group" aria-label="表示する行">${B_.FILTERS.map(([k, name]) =>
    `<button data-a="b-filter" data-f="${k}" aria-pressed="${cur === k}">${name} ${B_.rowsFor(lines(), box(), k).length}</button>`).join('')}</div>
        <button class="btn ghost" data-a="b-reload" title="箱を読み直します">🔄 再読込</button></div>
      <div class="arow"><b class="alabel">取り込み</b>
        <label class="hint" for="b-license">ライセンス</label>
        <select id="b-license" data-a="b-license">${B_.LICENSES.map(([v, name]) => `<option value="${esc(v)}" ${v === B.license ? 'selected' : ''}>${esc(name)}</option>`).join('')}</select>
        ${need ? `<input id="b-credit" data-a="b-credit" value="${esc(B.credit)}" placeholder="クレジット（作者・出典）" aria-label="クレジット">` : ''}
        <span class="hint">ファイルは行へ直接ドロップ（その行に当たります）か、右の箱へドロップ（どの行にも当てません）。空欄のライセンスは入れられません。</span></div>
      ${B.error ? `<div class="note warn">${esc(B.error)}</div>` : ''}
      ${B.loading && !B.box ? '<div class="hint">箱を読み込んでいます…</div>' : ''}</div>`;
  }

  function gridHtml() {
    const rows = B_.rowsFor(lines(), box(), B.filter);
    return `<div class="bwrap"><div class="bmain">${orphansHtml()}${rows.length ? rows.map(rowHtml).join('')
      : `<div class="note">${lines().length ? 'この条件の行はありません' : '行がありません'}</div>`}</div>${drawerHtml()}</div>`;
  }

  /** 行モーダルの「Bロール」区画（行の表のB列と同じ操作）。 */
  function modalHtml(l) {
    const a = B_.aPicture(l);
    return `<div class="box" data-b-drop="line" data-b-line="${esc(l.id)}"><span class="flabel">この行のBロール（補足の写真・動画。この行の音声の間、全画面で差し込みます）</span>
        <div class="bchips">${chipsHtml(l)}</div>
        <div class="inline"><button class="btn" data-a="b-pick" data-line="${esc(l.id)}">＋ 箱から選ぶ・ファイルを入れる</button>
          <span class="hint">この区画へファイルをドロップしてもこの行に入ります。行の中の秒・範囲は持ちません（DaVinci で調整）。</span></div>
        ${B.pick === l.id ? pickerHtml(l) : ''}</div>
      <div class="box"><span class="flabel">A（完成した絵）</span>
        ${a ? `<div class="inline"><img class="bgthumb" src="${esc(a.url)}" alt=""><span class="hint">${esc(a.label)}</span></div>` : '<span class="hint">まだ絵がありません（絵タブで決めます）</span>'}</div>
      <div class="box"><span class="flabel">ライセンス（取り込み時）</span><div class="inline">
        <select id="bm-license" data-a="b-license">${B_.LICENSES.map(([v, name]) => `<option value="${esc(v)}" ${v === B.license ? 'selected' : ''}>${esc(name)}</option>`).join('')}</select>
        ${B_.needsCredit(B.license) ? `<input id="bm-credit" data-a="b-credit" value="${esc(B.credit)}" placeholder="クレジット（作者・出典）">` : ''}</div></div>`;
  }

  // ── 操作 ─────────────────────────────────────────────────
  const done = async (fn, msg) => { await working(msg, async () => { await fn(); await loadBox(); }); };

  async function upload(files, lineId) {
    if (!files || !files.length) return;
    const block = B_.uploadBlock({ license: B.license, credit: B.credit });
    if (block) { toast(block, { bad: true }); return; }
    const form = new FormData();
    files.forEach((f) => form.append('files', f));
    form.append('license', B.license);
    form.append('source_kind', 'user');
    if (B.credit.trim()) form.append('credit', B.credit.trim());
    if (lineId) form.append('line_id', lineId);
    await working('取り込んでいます…', async () => {
      const res = await api.broll.upload(pid(), ep(), form);
      await loadBox();
      const ok = res.registered || [], err = res.errors || [], dup = ok.filter((r) => r.duplicate).length;
      const detail = err.map((e) => `${e.original_name}: ${(e.detail && (e.detail.message || e.detail.code)) || e.status}`).join(' ／ ');
      toast(`${ok.length}件を箱に入れました${lineId && ok.length ? `（${lineName(lineId)}に当てました）` : ''}${dup ? `・同じ中身が既にあった ${dup}件` : ''}`
        + `${err.length ? `。入れられなかった ${err.length}件 — ${detail}` : ''}`, { bad: !!err.length && !ok.length, ms: err.length ? 9000 : 3200 });
    });
  }

  const assign = (assetId, lineId, index) => done(() => api.broll.assign(pid(), ep(), { line_id: lineId, asset_id: assetId, ...(index === undefined ? {} : { index }) }), '当てています…');
  const unassign = (assetId, lineId) => done(() => api.broll.unassign(pid(), ep(), { line_id: lineId, asset_id: assetId }), '外しています…');
  const reorder = (lineId, ids) => done(() => api.broll.reorder(pid(), ep(), { line_id: lineId, asset_ids: ids }), '並べ替えています…');

  async function save(assetId) {
    const d = B.draft || {};
    const get = (id) => { const e = document.getElementById(id); return e ? e.value : undefined; };
    const body = { license: get('b-d-license') ?? d.license, credit: get('b-d-credit') ?? d.credit, note: get('b-d-note') ?? d.note };
    B.draft = null;
    await done(() => api.broll.patch(pid(), ep(), assetId, body), '保存しています…');
    toast('保存しました');
  }

  async function trash(assetId) {
    if (!confirm('この素材を箱から外します（broll/_trash へ移すだけで、消えません）。続行しますか？')) return;
    await done(() => api.broll.trash(pid(), ep(), assetId), '箱から外しています…');
    B.detail = null; B.draft = null; ctx.rerender();
  }

  // ── ドラッグ＆ドロップ（箱→行／行→箱／行の中の並べ替え／ファイル→行・箱） ──────────
  const hasFiles = (e) => !!(e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files'));
  const zoneOf = (e) => (e.target && e.target.closest ? e.target.closest('[data-b-drop]') : null);
  const clearOver = () => document.querySelectorAll('.bover').forEach((n) => n.classList.remove('bover'));

  function dragstart(e) {
    const el = e.target && e.target.closest ? e.target.closest('[data-b-asset]') : null;
    if (!el) return;
    B.drag = { asset: el.dataset.bAsset, from: el.dataset.bLine || null };
    if (e.dataTransfer) { e.dataTransfer.effectAllowed = 'copyMove'; try { e.dataTransfer.setData('text/plain', el.dataset.bAsset); } catch { /* 無視 */ } }
  }
  function dragover(e) {
    const z = zoneOf(e);
    if (!z) { if (hasFiles(e)) e.preventDefault(); return; }       // 枠の外へのファイルのドロップでブラウザが開かないようにする
    if (!B.drag && !hasFiles(e)) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = B.drag && B.drag.from ? 'move' : 'copy';
    if (!z.classList.contains('bover')) { clearOver(); z.classList.add('bover'); }
  }
  async function drop(e) {
    const z = zoneOf(e), files = [...((e.dataTransfer && e.dataTransfer.files) || [])], drag = B.drag;
    clearOver();
    B.drag = null;
    if (!z) { if (files.length) { e.preventDefault(); toast('行か、右の素材の箱の上にドロップしてください', { bad: true }); } return; }
    if (!files.length && !drag) return;
    e.preventDefault();
    const kind = z.dataset.bDrop, lineId = z.dataset.bLine || null;
    if (files.length) { await upload(files, kind === 'line' ? lineId : null); return; }
    if (kind === 'drawer') { if (drag.from) await unassign(drag.asset, drag.from); return; }          // 行から箱へ戻す＝外す
    if (!lineId) return;
    if (drag.from === lineId) {                                                                         // 同じ行の中: 落とした位置へ
      const tgt = e.target.closest('.bchip');
      const ids = B_.placedOf(box(), lineId).map((it) => it.asset.asset_id);
      const from = ids.indexOf(drag.asset);
      let to = tgt ? ids.indexOf(tgt.dataset.bAsset) : ids.length - 1;
      if (from < 0 || to < 0 || from === to) return;
      await reorder(lineId, B_.moveIn(ids, from, to));
      return;
    }
    await assign(drag.asset, lineId);
  }
  function dragend() { B.drag = null; clearOver(); }

  async function click(el) {
    const a = el.dataset.a, d = el.dataset;
    const act = {
      'b-filter': () => { B.filter = d.f; ctx.rerender(); },
      'b-reload': () => working('読み直しています…', loadBox),
      'b-pick': () => { B.pick = B.pick === d.line ? null : d.line; ctx.rerender(); redrawModal(); },
      'b-pick-close': () => { B.pick = null; ctx.rerender(); redrawModal(); },
      'b-assign': async () => { B.pick = null; await assign(d.asset, d.line); },
      'b-unassign': () => unassign(d.asset, d.line),
      'b-move': () => {
        const ids = B_.placedOf(box(), d.line).map((it) => it.asset.asset_id), i = ids.indexOf(d.asset);
        return i < 0 ? undefined : reorder(d.line, B_.moveIn(ids, i, i + Number(d.d)));
      },
      'b-detail': () => { B.detail = B.detail === d.asset ? null : d.asset; B.draft = null; ctx.rerender(); },
      'b-detail-close': () => { B.detail = null; B.draft = null; ctx.rerender(); },
      'b-dkind': () => { B.d.kind = d.k; ctx.rerender(); },
      'b-goto': () => { const r = document.getElementById(`r-${d.line}`); if (r) r.scrollIntoView({ block: 'center', behavior: 'smooth' }); },
      'b-save': () => save(d.asset),
      'b-trash': () => trash(d.asset),
      'b-orph-assign': async () => {
        const to = B.orphTo[d.key] || (lines()[0] || {}).id;
        if (!to) return;
        await done(async () => { await api.broll.assign(pid(), ep(), { line_id: to, asset_id: d.asset }); await api.broll.unassign(pid(), ep(), { line_id: d.from, asset_id: d.asset }); }, '当て直しています…');
      },
      'b-orph-drop': () => unassign(d.asset, d.from),
    }[a];
    if (act) await act();
  }

  function change(el) {
    const a = el.dataset.a;
    if (a === 'b-license') { B.license = el.value; try { store && store.setItem('wb-broll-license', B.license); } catch { /* 無視 */ } ctx.rerender(); redrawModal(); }
    else if (a === 'b-file') { const files = [...(el.files || [])]; el.value = ''; upload(files, el.dataset.line || null); }
    else if (a === 'b-dsource') { B.d.source = el.value; ctx.rerender(); }
    else if (a === 'b-dunassigned') { B.d.unassigned = el.checked; ctx.rerender(); }
    else if (a === 'b-orph-line') { B.orphTo[el.dataset.key] = el.value; }
    else if (a === 'b-draft') { B.draft = { license: val('b-d-license'), credit: val('b-d-credit'), note: val('b-d-note') }; }
  }
  const val = (id) => { const e = document.getElementById(id); return e ? e.value : ''; };
  function input(el) {
    if (el.dataset.a === 'b-credit') B.credit = el.value;
    else if (el.dataset.a === 'b-draft') B.draft = { license: val('b-d-license'), credit: val('b-d-credit'), note: val('b-d-note') };
  }

  return { B, enter, loadBox, hasBusyInput, toolbarHtml, gridHtml, modalHtml, click, change, input, dragstart, dragover, drop, dragend,
    seg: (l) => B_.segFor(box(), l.id), count: () => B_.summaryOf(lines(), box()).rows, ensure: () => { if (!B.box && !B.loading) loadBox(); } };
}
