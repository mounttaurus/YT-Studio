// 仕上がりタブ（合成チェックと合成）の描画と操作（Docs/LINE_WORKBENCH_PLAN.md §5-4-1・§6-2・W4b-2）。
// 合成＝Photoshop で1行分の合成を最初から作り直す。director はホスト常駐（host_worker）へジョブをキューに
// 書くだけ（HTTPでは繋がない）。**手直しの保護はホスト側が持つ**: 手直し済みの行は既定で飛ばし、
// `include_edited` を明示した時だけ、元の PSD を退避してから上書きする。ここはその確認と結果の表示。
// 確認欄は窓口の dry_run と同じ形（レーン＋自動／あとで／そのまま）。課金は無い（画像は生成しない）。
import * as R from './final_model.js';
import { selectedIds, toggleAll, allChecked } from './aroll_model.js';

export function createFinalUi(ctx) {
  const { S, api, esc, toast, fail, working, load, byId, pill, redrawModal } = ctx;
  const F = {
    jobs: [], timer: null, wasRunning: false, confirm: null, psPending: null, msg: '', cmdOpen: false, hostOpen: false,
    ver: 1, cutoutPending: null,
  };
  const pid = () => S.pid;
  const ep = () => S.ep;
  const lines = () => S.lines;
  const meta = () => (S.view && S.view.psassist) || { available: false, alive: false };
  const busy = () => R.jobRunning(F.jobs);
  const visible = () => R.rowsFor(lines(), S.ffilter);
  const chip = (cls, t, title) => `<span class="chip ${cls || ''}"${title ? ` title="${esc(title)}"` : ''}>${esc(t)}</span>`;
  const url = (u) => (u ? `${u}?v=${F.ver}` : '');

  // ── 読み込み・ジョブの見守り ─────────────────────────────
  async function enter() {
    await loadJobs();
    if (R.jobRunning(F.jobs)) startTimer();
    loadCutoutPending();
    ctx.rerender();
  }
  async function loadJobs() {
    try { F.jobs = (await api.psassist.jobs(pid(), ep())).jobs || []; } catch { F.jobs = []; }
  }
  /** PS切り抜きの未処理（この話数が使うキャラ分の合算）。worker が PS本線化に対応していない環境では取らない。 */
  async function loadCutoutPending() {
    F.cutoutPending = null;
    if (!(meta().capabilities || []).includes('library_cutout')) return;
    const ids = [...new Set(lines().map((l) => l.aroll && l.aroll.cutout_char_id).filter(Boolean))];
    if (!ids.length) { F.cutoutPending = 0; return; }
    try {
      const rs = await Promise.all(ids.map((id) => api.psassist.pendingCutouts(id).catch(() => null)));
      F.cutoutPending = rs.reduce((sum, r) => sum + ((r && r.pending) || 0), 0);
    } catch { F.cutoutPending = null; }
    ctx.rerender();
  }
  function startTimer() { if (!F.timer) F.timer = setInterval(tick, 3000); }
  function stopTimer() { clearInterval(F.timer); F.timer = null; }
  async function tick() {
    const was = F.wasRunning || busy();
    await loadJobs();
    const running = busy();
    F.wasRunning = running;
    if (running) { ctx.rerender(); return; }
    stopTimer();
    if (was) {
      const job = F.jobs[0];
      F.ver++;
      await load();                                   // 合成・検査・プランが動いたので状態を読み直す
      const sum = R.jobSummary(job);
      toast(`${R.JOB_LABEL[job && job.kind] || 'ジョブ'}: ${R.JOB_STATUS[job && job.status] || ''}${sum ? `（${sum}）` : ''}${job && job.status === 'failed' ? ' — ログを確認してください' : ''}`,
        { bad: !!job && job.status === 'failed', ms: 8000 });
    }
    ctx.rerender(); redrawModal();
  }

  /** ジョブを1件キューへ。PS切り抜きが未処理なら 409 で返るので、待つ／このまま進めるを選ばせる（C10）。 */
  async function post(kind, ids, { includeEdited = false, force = false } = {}) {
    const body = { kind };
    if (ids) body.lines = ids;
    if (includeEdited) body.args = { include_edited: true };
    if (force) body.force = true;
    try {
      const res = await api.psassist.createJob(pid(), ep(), body);
      F.psPending = null;
      await loadJobs();
      F.wasRunning = true;
      startTimer();
      if ((res.skipped_unconfirmed || []).length) toast(`未確定の ${res.skipped_unconfirmed.length}行は飛ばしました`);
      return true;
    } catch (e) {
      const d = e.detail;
      if (e.status === 409 && d && Array.isArray(d.ps_pending_lines)) { F.psPending = { n: d.ps_pending_lines.length, alive: d.worker_alive, kind, ids, includeEdited }; return false; }
      throw e;
    }
  }

  // ── 確認欄（窓口の dry_run と同じ形） ────────────────────────
  const lane = (who, sub, items) => (items.length ? `<div class="lane"><div class="who2">${who}<small>${sub}</small></div><ul>${items.map(([k, t]) =>
    `<li><span class="tag ${k}">${{ auto: '自動', later: 'あとで', keep: 'そのまま' }[k]}</span><span>${esc(t)}</span></li>`).join('')}</ul></div>` : '');

  function confirmHtml(origin) {
    const C = F.confirm;
    if (!C || C.origin !== origin) return '';
    let title, body, ok, runOk;
    if (C.kind === 'resync') {
      const p = R.planResync(lines(), C.ids, S.enabled, C.includeEdited);
      title = '「合成」でこうなります';
      const fin = [];
      if (p.run.length) fin.push(['auto', `${p.run.length}行を Photoshop で最初から合成し直します（背景→キャラ→吹き出し→本文→保存→検査→納品PNG）。Photoshop を占有します`]);
      if (p.unconfirmed.length) fin.push(['keep', `未確定の ${p.unconfirmed.length}行は飛ばします（先に「✓ 確定」）`]);
      if (p.noPicture.length) fin.push(['keep', `絵がまだ無い ${p.noPicture.length}行は組めません（絵タブで決めてください）`]);
      if (p.skippedEdited.length) fin.push(['keep', `✋手直し済み ${p.skippedEdited.length}行は飛ばします（上書きすると手直しが消えるため）`]);
      if (p.includeEdited && p.edited.length) fin.push(['later', `✋手直し済み ${p.edited.filter((id) => p.run.includes(id)).length}行は、元のPSDを psd_final/_backup/ に退避してから上書きします`]);
      const edit = p.edited.length ? `<label class="chk"><input type="checkbox" data-a="f-include" ${C.includeEdited ? 'checked' : ''}> 手直し済みも含める（元のPSDを退避してから上書き）</label>` : '';
      body = `${lane('仕上がり', '合成', fin)}${lane('台本・音声・絵', '', [['keep', '変わりません（絵の選び直しは絵タブで済んでいます）']])}${edit}`;
      runOk = p.run.length > 0 && meta().alive && !busy();
      ok = `${p.run.length}行を合成`;
    } else if (C.kind === 'export') {
      title = '「納品PNGを更新」でこうなります';
      body = lane('仕上がり', '納品PNG', [['auto', `${C.ids.length}行の納品PNG（1920×1080）を PSD から書き出し直します。Photoshop を占有します`]]);
      runOk = C.ids.length > 0 && meta().alive && !busy();
      ok = `${C.ids.length}行を書き出す`;
    }
    const alive = meta().alive ? '' : '<div class="note warn">ホスト工程（Photoshop の常駐）が止まっています。下の「ホスト工程」の起動コマンドで起動してください。</div>';
    const pend = F.psPending ? `<div class="note warn">🖼️ PS切り抜きが未処理の行が ${F.psPending.n}行あります（worker: ${F.psPending.alive ? '稼働中' : '停止中（待っても進みません）'}）。
      <div class="inline"><button class="btn" data-a="f-wait">待つ（今回は何もしません）</button>
      <button class="btn primary" data-a="f-force">このまま進める（rembgの仮絵のまま組みます。後でPS版に差し替わったら「🔧要合成」で拾えます）</button></div></div>` : '';
    return `<div class="confirm" id="f-confirm"><h3>${title}（どのタブから押しても同じ）</h3>${body}${alive}${pend}
      <div class="inline"><span class="hint">費用 0円（画像の生成は走りません）</span><span class="spacer"></span>
      <button class="btn ghost" data-a="f-cancel">やめる</button><button class="btn primary" data-a="f-run" ${runOk ? '' : 'disabled'}>${esc(ok)}</button></div></div>`;
  }

  // ── 描画: 一括操作バー・ホスト工程 ─────────────────────────
  function toolbarHtml() {
    const m = meta();
    if (!lines().length) return '';
    const c = R.counts(lines()), sel = selectedIds(lines(), S.sel), n = sel.length;
    const targets = R.exportTargets(lines());
    const off = S.working || busy();
    const needbuild = R.rowsFor(lines(), 'needbuild');
    const facets = R.codeFacets(lines());
    const cur = S.ffilter;
    const summary = [['blocking', '要対応'], ['advisory', '助言'], ['clean', '問題なし'], ['needbuild', '要合成'], ['ungenerated', '未生成']]
      .map(([k, l]) => (c[k] ? pill(R.SEV_CLASS[k] || 'none', `${R.SEV_MARK[k]} ${l} ${c[k]}`) : '')).join(' ');
    return `<div class="abar">
      <div class="arow"><b class="alabel">合成チェック</b>${summary}
        ${c.edited ? pill('edit', `✋ 手直し済み ${c.edited}`) : ''}
        <span class="hint">最終検査: ${esc(m.checked_at ? new Date(m.checked_at).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—')}</span>
        <button class="btn ghost" data-a="f-reload" title="合成チェックを読み直します">🔄 再読込</button></div>
      ${m.has_qa ? '' : `<div class="note">まだ検査していません。下の「ホスト工程」で ④ 検査する（または全部検査のコマンド）を実行してください。</div>`}
      <div class="arow"><label class="chk"><input type="checkbox" data-a="f-selall" ${allChecked(visible(), S.sel) ? 'checked' : ''}> 全選択（見えている行だけ）</label>
        <span class="hint mono">${n}行を選択中</span><span class="spacer"></span>
        <div class="filter" role="group" aria-label="表示する行">${R.FILTERS.map(([k, name]) =>
          `<button data-a="f-filter" data-f="${k}" aria-pressed="${cur === k}">${name} ${R.rowsFor(lines(), k).length}</button>`).join('')}</div>
        ${facets.length ? `<select data-a="f-code" aria-label="指摘コード別"><option value="">指摘コード別…</option>${facets.map(([code, k]) =>
          `<option value="code:${esc(code)}" ${cur === `code:${code}` ? 'selected' : ''}>${esc(R.codeLabel(code))} ${k}</option>`).join('')}</select>` : ''}</div>
      <div class="arow"><b class="alabel">合成</b>
        <button class="btn" data-a="f-needbuild" ${needbuild.length ? '' : 'disabled'} title="要合成の行だけをチェックします">🔧 要合成を選択${needbuild.length ? `（${needbuild.length}）` : ''}</button>
        <button class="btn primary" data-a="f-resync" ${!n || off || !m.alive ? 'disabled' : ''} title="選択行を Photoshop で最初から合成し直します（確定済みだけ・手直し済みは既定で飛ばします）">🔧 まとめて合成<span class="n">${n || ''}</span></button>
        <button class="btn" data-a="f-export" ${!targets.length || off || !m.alive ? 'disabled' : ''} title="納品PNGが古い・無い行だけ書き出し直します">📤 納品PNGを更新<span class="n">${targets.length || ''}</span></button>
        ${!m.alive ? '<span class="hint warnt">ホスト工程が止まっているため、合成・書き出しは押せません</span>' : ''}</div>
      ${confirmHtml('toolbar')}${hostHtml()}${F.msg ? `<div class="note">${esc(F.msg)}</div>` : ''}</div>`;
  }

  function jobHtml() {
    const job = F.jobs[0];
    if (!job) return '<div class="hint">ジョブはまだありません</div>';
    const pct = R.jobPercent(job), running = job.status === 'running';
    const sum = R.jobSummary(job);
    const log = (job.log || []).slice(-40).join('\n');
    return `<div class="arow"><b>${esc(R.JOB_LABEL[job.kind] || job.kind)}</b> ${pill(job.status === 'failed' ? 'bad' : job.status === 'done' ? 'ok' : running ? 'info' : 'warn', R.JOB_STATUS[job.status] || job.status)}
      ${job.progress && job.progress.total ? `<span class="hint mono">${job.progress.done}/${job.progress.total}</span>` : ''}${sum ? `<span class="hint">${esc(sum)}</span>` : ''}
      ${running ? '<button class="btn danger" data-a="f-cancel-job" title="今のチャンク（最大20行）を終えてから止まります">⏸ 中断</button>' : ''}</div>
      ${running || pct ? `<div class="progress"><i style="width:${pct}%"></i></div>` : ''}
      ${log ? `<pre class="joblog">${esc(log)}</pre>` : ''}`;
  }

  function hostHtml() {
    const m = meta();
    const cmds = R.hostCommands(m.episode_dir, ep());
    const off = S.working || busy() || !m.alive;
    return `<details class="aset" data-a="f-hostopen" ${F.hostOpen ? 'open' : ''}><summary>ホスト工程（Photoshop の常駐）
        ${pill(m.alive ? 'ok' : 'bad', m.alive ? '稼働中' : '停止中')}${F.cutoutPending ? ` ${pill(m.alive ? 'warn' : 'bad', `PS切り抜き 未処理 ${F.cutoutPending}枚`)}` : ''}</summary>
      ${m.alive ? '' : `<div class="note warn">host_worker が止まっています。ホストで次を実行してください（ダブルクリックで起動・ウィンドウを閉じると終了）。</div>
        <pre class="cmd" data-a="f-copy" data-copy="${esc(R.startCommand(m.episode_dir))}" title="クリックでコピー">${esc(R.startCommand(m.episode_dir))}</pre>`}
      <div class="arow">${R.STEPS.map((s) => `<button class="btn" data-a="f-step" data-kind="${s.kind}" ${off ? 'disabled' : ''} title="${esc(s.hint)}">${s.label}${s.ps ? ' <small>(PS占有)</small>' : ''}</button>`).join('')}
        <span class="hint">③ コマを組む は「まとめて合成」に含まれます（手直しの保護を通すため、単独の全行組版は置きません）</span></div>
      ${jobHtml()}
      <div class="hint">ホストで打つコマンド（クリックでコピー）</div>
      ${cmds.map(([name, cmd]) => `<div class="hint">${esc(name)}</div><pre class="cmd" data-a="f-copy" data-copy="${esc(cmd)}" title="クリックでコピー">${esc(cmd)}</pre>`).join('')}
    </details>`;
  }

  // ── 描画: グリッド ────────────────────────────────────────
  function gridHtml() {
    const rows = visible();
    if (!rows.length) return `<div class="note">${lines().length ? 'この条件の行はありません' : '行がありません'}</div>`;
    return `<div class="fgrid">${rows.map(cardHtml).join('')}</div>`;
  }
  function cardHtml(l) {
    const s = R.severity(l), t = R.thumbOf(l), f = l.final || {};
    const issue = R.mainIssue(l);
    const chips = R.chips(l).map(([c, x, t]) => chip(c, x, t)).join('');
    return `<div class="fcard sev-${s}" id="r-${esc(l.id)}" tabindex="0" data-open="${esc(l.id)}">
      <div class="fthumb">${t ? `<img src="${esc(url(t.url))}" loading="lazy" alt="">` : '<span class="none">—</span>'}
        ${t && !t.composed ? '<span class="fbadge" title="合成結果ではなく、割り当てた素材です">素材</span>' : ''}
        ${R.hasPanel(l) ? `<label class="rowsel" title="一括操作の対象にする"><input type="checkbox" data-a="f-selline" data-id="${esc(l.id)}" ${S.sel.has(l.id) ? 'checked' : ''}></label>` : ''}</div>
      <div class="fmeta"><span class="mono">${l.parent_line_id && l.parent_line_id !== l.id ? '↳' : ''}${l.order}</span> <span>${esc(l.speaker_name || '')}</span></div>
      <div class="fchips">${chips}</div>
      ${issue ? `<div class="hint">${esc(issue)}${(f.issues || []).length > 1 ? ` ほか${f.issues.length - 1}` : ''}</div>` : ''}</div>`;
  }

  /** 絵区画の「要合成」注意書きに置くショートカット。押すと仕上がり区画へ移り、合成の確認欄を開く（承認とは別の操作）。 */
  function resyncShortcutHtml(l) {
    const block = R.resyncBlock(l, { enabled: S.enabled, alive: meta().alive, busy: busy() || S.working });
    return `<div class="inline"><button class="btn primary" data-a="f-goto-resync" ${block ? 'disabled' : ''} title="${esc(block || '仕上がり区画へ移り、この行の合成の確認を開きます')}">🔧 この行を合成</button>${block ? `<span class="hint warnt">${esc(block)}</span>` : ''}</div>`;
  }

  // ── Photoshop で開く（host_worker の open_psd・2026-09-30） ──────────
  // ワーカーが開くのは psd_final/panel_{line_id}.psd だけ。古いワーカー（capabilities に無い）は再起動を案内する
  function openHtml(l, m) {
    const why = !m.alive ? 'ホスト工程（host_worker）が止まっています'
      : !(m.capabilities || []).includes('open_psd') ? 'host_worker が古い版です（再起動すると使えます）'
        : !(l.final || {}).has_psd ? 'まだ合成PSDがありません' : '';
    const dis = why || S.working ? 'disabled' : '';
    return `<button class="btn primary" data-a="f-open" data-id="${esc(l.id)}" ${dis} title="${esc(why || 'ホストの Photoshop でこの行のPSDを開きます')}">🖌 Photoshop で開く</button>
      <button class="btn" data-a="f-reveal" data-id="${esc(l.id)}" ${dis} title="${esc(why || 'エクスプローラーでPSDの場所を開きます')}">📂 場所を開く</button>
      ${why ? `<span class="hint warnt">${esc(why)}</span>` : ''}`;
  }

  async function openPsd(lineId, reveal) {
    try {
      await api.psassist.createJob(pid(), ep(), { kind: 'open_psd', lines: [lineId], ...(reveal ? { args: { reveal: true } } : {}) });
      toast(reveal ? 'エクスプローラーで開くよう頼みました' : 'Photoshop で開くよう頼みました（数秒かかります。合成ジョブの実行中はその後に開きます）');
    } catch (e) { fail(e); }
  }

  // ── 描画: 行モーダルの「仕上がり」区画 ─────────────────────
  function modalHtml(l) {
    const f = l.final || {}, a = l.aroll || {}, s = R.severity(l), m = meta();
    if (!a.has_manifest || !a.panel) return '<div class="box"><span class="note">まだ絵の側にコマがありません。絵タブで下ごしらえしてください。</span></div>';
    const t = R.thumbOf(l);
    const stale = f.build_state === 'restale';
    const block = R.resyncBlock(l, { enabled: S.enabled, alive: m.alive, busy: busy() || S.working });
    const issues = stale ? [] : (f.issues || []);
    const regions = stale ? [] : issues.filter((i) => i.region);
    const view = !stale && f.view ? f.view : (t && t.url);
    return `
      <div class="box"><div class="apic">
        <div class="viewwrap">${view ? `<img class="bigthumb" src="${esc(url(view))}" alt="">` : '<div class="bigthumb none">まだ合成していません</div>'}
          ${regions.map((it) => { const b = R.regionBox(f, it); return `<i class="rbox ${it.severity}" style="left:${b.left}%;top:${b.top}%;width:${b.width}%;height:${b.height}%"></i>`; }).join('')}</div>
        <div class="acol"><div class="ach">${R.chips(l).map(([c, x, t]) => chip(c, x, t)).join('')}${f.built_at ? `<span class="hint">組んだ日時 ${esc(new Date(f.built_at).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }))}</span>` : ''}</div>
          ${stale ? '<div class="note warn">🔧 絵を選び直した（または台本の文面が変わった）後に合成していません。上のプレビューは差し替え後の素材で、合成結果ではありません。「この行を合成」で反映します。</div>' : ''}
          ${f.edited ? '<div class="note warn">✋ Photoshop で手直しした合成です。合成すると手直しが消えます（確認して、元のPSDを退避してから上書きします）。</div>' : ''}
          ${f.build_state === 'unbuilt' ? '<div class="note">まだ合成していない行です。絵を決めたら「この行を合成」で組みます。</div>' : ''}
          ${issues.map((it) => `<div class="issue ${it.severity}">${it.severity === 'blocking' ? '🔴' : '🟡'} ${esc(it.label)} <small class="mono">${esc(it.code)}</small></div>`).join('')}
          ${!issues.length && !stale && ['clean', 'advisory'].includes(s) && f.severity ? '<div class="note ok">✔ 機械検査では問題なし（光源の違和感・手の乱れなどは目視で確認してください）</div>' : ''}</div></div></div>
      <div class="box"><span class="flabel">使っている素材（決める場所は絵区画。ここからも入れます）</span>
        <div class="inline">${a.background_id ? `<img class="bgthumb" src="${esc(api.backgroundUrl(a.background_id))}" alt="">` : ''}
          <span class="hint mono">背景: ${esc(a.background_id || '（未割当）')}</span>
          <button class="btn" data-a="goto-bg" ${S.working ? 'disabled' : ''} title="絵区画の背景ピッカーを開きます。選び直すと「要合成」になります">🔀 背景を変える</button></div>
        <div class="inline"><span class="hint mono">キャラ絵: ${esc(a.cutout_slot_id || (a.has_image ? '生成した絵' : '（未割当）'))}</span>
          <button class="btn" data-a="goto-pick" ${S.working ? 'disabled' : ''} title="絵区画の在庫ピッカーを開きます（無料）。選び直すと「要合成」になります">🔀 キャラ絵を選び直す</button></div></div>
      <div class="inline">
        <button class="btn primary" data-a="f-m-resync" ${block ? 'disabled' : ''} title="${esc(block || 'Photoshop で1行分を最初から合成し直します')}">🔧 この行を合成</button>
        ${block ? `<span class="hint warnt">${esc(block)}</span>` : ''}
        ${f.export ? `<a class="btn" href="${esc(url(f.export))}" target="_blank" rel="noopener">🖼 納品PNG（1920×1080）</a>` : ''}
        <button class="btn" data-a="f-m-export" ${!f.has_psd || busy() || !m.alive ? 'disabled' : ''} title="この行だけ納品PNGを書き出し直す">🖼 この行だけ更新</button></div>
      ${confirmHtml('modal')}
      ${f.has_psd || f.psd ? `<div class="box"><span class="flabel">Photoshop で手直しする</span>
        <div class="inline">${openHtml(l, m)}</div>
        ${f.psd ? `<pre class="cmd" data-a="f-copy" data-copy="${esc(R.psdHostPath(m, f))}" title="クリックでコピー">${esc(R.psdHostPath(m, f))}</pre>` : ''}
        <span class="hint">保存すると自動で再検査されます。手直しした行は「✋ 手直し済み」になり、合成しても既定では上書きされません。</span></div>` : ''}
      ${R.measuredRows(f).length ? `<details class="aset"><summary>実測値</summary>${R.measuredRows(f).map(([k, v]) => `<div class="hint">${esc(k)}: ${esc(String(v))}</div>`).join('')}</details>` : ''}`;
  }

  // ── 操作 ────────────────────────────────────────────────
  const copy = async (text) => {
    try { await navigator.clipboard.writeText(text); toast('コピーしました'); } catch { toast('コピーできませんでした（手で選択してください）', { bad: true }); }
  };

  /** 確認欄を開く。ホスト工程が止まっていれば理由を出す（黙って諦めない）。 */
  function openConfirm(kind, ids, origin) {
    F.psPending = null;
    F.confirm = { kind, ids, origin, includeEdited: false };
    ctx.rerender(); redrawModal();
  }
  async function runConfirm(force = false) {
    const C = F.confirm;
    if (!C) return;
    if (C.kind === 'resync') {
      const p = R.planResync(lines(), C.ids, S.enabled, C.includeEdited);
      if (!p.run.length) return toast('合成できる行がありません', { bad: true });
      await working('合成を頼んでいます…', async () => {
        if (await post('resync', p.run, { includeEdited: C.includeEdited, force })) { F.confirm = null; toast(`合成を頼みました（${p.run.length}行・Photoshop を使います）`); }
      });
    } else if (C.kind === 'export') {
      await working('書き出しを頼んでいます…', async () => {
        if (await post('export_png', C.ids, { force })) { F.confirm = null; toast(`納品PNGの書き出しを頼みました（${C.ids.length}行）`); }
      });
    }
    ctx.rerender(); redrawModal();
  }

  async function step(kind) {
    const s = R.STEPS.find((x) => x.kind === kind);
    if (!s || busy() || !meta().alive) return;
    let msg = `${s.label} を実行します。`;
    if (s.ps) msg += '\n\n⚠️ この工程は Photoshop を占有します。\nPhotoshop で作業中なら、先に手を止めてください。';   // 作業中に流すと壊れる
    if (!confirm(`${msg}\n\n続行しますか？`)) return;
    await working('ジョブを頼んでいます…', async () => { await post(kind, undefined); toast(`${s.label} を頼みました`); });
    ctx.rerender();
  }

  async function cancelJob() {
    const job = F.jobs[0];
    if (!job || job.status !== 'running') return;
    if (!confirm('中断を要求します。今のチャンク（最大20行）を終えてから止まります。\nPhotoshop のドキュメントはそのまま残ります。続行しますか？')) return;
    try { await api.psassist.cancel(pid(), ep(), job.job_id); toast('中断を頼みました（今のチャンクが終わったら止まります）'); } catch (e) { fail(e); }
  }

  /**
   * 選び直した直後の自動合成（D25）。確定済み ∧ 手直し無し ∧ 要合成 ∧ ホスト工程が動いている行だけ。
   * それ以外は何もしない＝「要合成」の印だけが残る（手で押す）。戻り値: 走らせたか。
   */
  async function autoResync(lineId) {
    const l = byId(lineId);
    if (!l || !meta().available) return false;
    if (!R.canAutoResync(l, { enabled: S.enabled, alive: meta().alive, busy: busy() })) return false;
    try { return await post('resync', [lineId], { force: true }); } catch { return false; }   // 取りこぼしは印に残る
  }

  async function click(el) {
    const a = el.dataset.a, id = el.dataset.id;
    const act = {
      'f-filter': () => { S.ffilter = el.dataset.f; ctx.rerender(); },
      'f-reload': async () => { F.ver++; await working('読み直しています…', async () => { await load(); await loadJobs(); }); },
      'f-needbuild': () => { S.sel = new Set(R.rowsFor(lines(), 'needbuild').map((l) => l.id)); S.ffilter = 'needbuild'; ctx.rerender(); },
      'f-resync': () => openConfirm('resync', selectedIds(lines(), S.sel), 'toolbar'),
      'f-export': () => openConfirm('export', R.exportTargets(lines()), 'toolbar'),
      'f-m-resync': () => openConfirm('resync', [S.modal.id], 'modal'),
      'f-goto-resync': async () => { S.modal.section = 'final'; S.modal.pending = null; await enter(); openConfirm('resync', [S.modal.id], 'modal'); },
      'f-m-export': () => openConfirm('export', [S.modal.id], 'modal'),
      'f-cancel': () => { F.confirm = null; F.psPending = null; ctx.rerender(); redrawModal(); },
      'f-run': () => runConfirm(false),
      'f-force': () => runConfirm(true),
      'f-wait': () => { F.psPending = null; toast('待ちます（今回は何もしません）'); ctx.rerender(); redrawModal(); },
      'f-step': () => step(el.dataset.kind),
      'f-cancel-job': cancelJob,
      'f-copy': () => copy(el.dataset.copy),
      'f-open': () => openPsd(el.dataset.id, false),
      'f-reveal': () => openPsd(el.dataset.id, true),
    }[a];
    if (act) await act();
    void id;
  }
  function change(el) {
    const a = el.dataset.a;
    if (a === 'f-selline') { el.checked ? S.sel.add(el.dataset.id) : S.sel.delete(el.dataset.id); ctx.rerender(); }
    else if (a === 'f-selall') { S.sel = toggleAll(S.sel, visible(), el.checked); ctx.rerender(); }
    else if (a === 'f-code') { S.ffilter = el.value || 'all'; ctx.rerender(); }
    else if (a === 'f-include') { F.confirm.includeEdited = el.checked; ctx.rerender(); redrawModal(); }
  }
  const toggle = (el) => { if (el.dataset.a === 'f-hostopen') F.hostOpen = el.open; };

  return { F, enter, resyncShortcutHtml, toolbarHtml, gridHtml, modalHtml, click, change, toggle, autoResync, stopTimer, busy };
}
