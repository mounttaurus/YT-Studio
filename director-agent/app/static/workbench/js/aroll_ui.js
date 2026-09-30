// 絵タブ（Aロール）の描画と操作（Docs/LINE_WORKBENCH_PLAN.md §5-4-1・W4b-1）。
// 状態は director の GET .../workbench（lines[].aroll）。絵の決定は今の scrapping-agent の `…/aroll/…` を
// director の中継（/api/scrapping/）経由でそのまま呼ぶ＝バックエンドは新設しない。
// 課金するもの・絵を上書きするものは、押した後に必ず確認を出す（文面は director の Aロールタブと同じ）。
// 選び直した直後の自動再合成は入れない（D25＝W4b-2）。「再合成が要る」の印だけ出す。
import * as R from './aroll_model.js';

const AXES = [['emotion', '表情'], ['shot', 'ショット'], ['angle', 'アングル'], ['pose', 'ポーズ(任意)']];

export function createArollUi(ctx) {
  const { S, api, esc, toast, fail, working, load, byId, pill, redrawModal } = ctx;
  const A = {
    presets: null, styles: [], chars: null, plan: null, planBusy: false, planMsg: '',
    job: null, running: false, timer: null, ver: 1, msg: '', warnings: [],
    settings: { style: '', aspect: '16:9', extra: '', model: '', overwrite: false, paid: false, target: 'missing' }, touched: false,
    credits: null, setOpen: false,
    picker: null,     // {lineId, items, note, busy}
    bg: null,         // {lineId, items, cat, busy}
    hover: '',
  };
  const pid = () => S.pid;
  const ep = () => S.ep;
  const lines = () => S.lines;
  const noManifest = () => lines().length > 0 && !lines().some((l) => l.aroll && l.aroll.has_manifest);
  const thumb = (l) => (l.aroll && l.aroll.thumb ? `${l.aroll.thumb}?v=${A.ver}` : '');
  const bgUrl = (id) => `${api.backgroundUrl(id)}?v=${A.ver}`;
  const visibleRows = () => R.rowsFor(lines(), S.afilter);
  const selected = () => R.selectedIds(lines(), S.sel);
  const chip = (cls, text) => `<span class="chip ${cls || ''}">${esc(text)}</span>`;

  // ── 読み込み ─────────────────────────────────────────────
  /** タブを開いた時・話数を読み込んだ時に呼ぶ。冪等（一度読んだものは読み直さない）。 */
  async function enter() {
    if (S.view && S.view.aroll_meta && !A.touched) {
      A.settings.aspect = S.view.aroll_meta.aspect || A.settings.aspect;
      A.settings.style = S.view.aroll_meta.style || A.settings.style;
    }
    const jobs = [];
    if (!A.presets) jobs.push(api.presets().then((d) => { A.presets = d.presets || {}; }).catch(() => { A.presets = {}; }));
    if (!A.styles.length) {
      jobs.push(api.styles().then((d) => { A.styles = Object.keys(d.styles || {}); }).catch(() => { /* 既定のまま */ }));
    }
    await Promise.all(jobs);
    if (!noManifest() && lines().some((l) => l.aroll && l.aroll.has_manifest)) {
      if (!A.plan) await loadPlan();
      await pollStatus(true);
    }
    ctx.rerender();
  }

  async function loadPlan() {
    A.planBusy = true;
    try { A.plan = await api.aroll.cutoutPlan(pid(), ep()); } catch (e) { A.plan = null; A.planMsg = `試算に失敗しました: ${e.message}`; }
    A.planBusy = false;
  }

  // ── 生成バッチの見守り ────────────────────────────────────
  async function pollStatus(first = false) {
    const was = A.running;
    let st;
    try { st = await api.aroll.status(pid(), ep()); } catch { A.running = false; return; }
    A.job = st.job || null;
    A.running = !!st.running;
    const d = R.arollPollDecision({ running: A.running, wasRunning: was });
    if (d === 'continue') {
      startTimer();
      A.ver++;                 // 1行終わるごとにサムネ・状態を逐次反映する
      if (!first) await load();
    } else if (d === 'finished') {
      stopTimer();
      A.ver++;
      await load();
      await loadPlan();
      const j = A.job || {};
      toast(`🎬 生成が終わりました（成功 ${j.done ?? 0}・失敗 ${j.failed ?? 0}）`, { bad: !!j.failed });
    } else stopTimer();
  }
  function startTimer() { if (!A.timer) A.timer = setInterval(() => pollStatus(), 2500); }
  function stopTimer() { clearInterval(A.timer); A.timer = null; }

  // ── 描画: 一括操作バー ────────────────────────────────────
  function filterHtml() {
    return `<div class="filter" role="group" aria-label="表示する行">${R.AROLL_FILTERS.map(([k, n]) =>
      `<button data-a="filter" data-f="${k}" aria-pressed="${S.afilter === k}">${n} ${R.filterCount(lines(), k)}</button>`).join('')}</div>`;
  }

  const opt = (v, label, cur) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(label)}</option>`;

  function toolbarHtml() {
    if (!lines().length) return '';
    if (noManifest()) {
      return `<div class="abar"><div class="arow"><span class="hint">この話数のAロールはまだ始まっていません。台本からコマごとの演出プロンプトを作ります（LLM・無料枠）。</span>
        <button class="btn primary" data-a="prompts-all" ${S.working ? 'disabled' : ''}>✍ プロンプトを作る（全行）</button></div>${settingsHtml(false)}${msgHtml()}</div>`;
    }
    const sel = selected(), vis = visibleRows(), n = sel.length;
    const p = A.plan, bill = R.billableCount(lines(), A.settings.target, p), total = R.batchTargetCount(lines(), A.settings.target);
    const missingSel = sel.filter((id) => !byId(id).aroll.picture);
    const fillLabel = n && missingSel.length ? `✂ 選択の ${missingSel.length}行を在庫で埋める（無料）`
      : p && p.from_stock ? `✂ 在庫 ${p.from_stock}行を割り当てる（無料）` : '✂ 在庫で埋める（無料）';
    const fillOff = S.working || A.running || (n && missingSel.length ? false : !(p && p.from_stock));
    const genLabel = A.running ? '生成中…'
      : bill === null ? `🎬 残りを生成（${total}枚 ≈ $${R.usd(total)}）`
      : `🎬 残りを生成（${total}枚中 課金 ${bill}枚 ≈ $${R.usd(bill)}）`;
    const okTargets = sel.filter((id) => { const a = byId(id).aroll; return a.status === 'done' && a.has_image; });
    const newLines = lines().filter(R.needsPrep);
    const job = A.job || {};
    const pct = R.jobPercent(job);
    const off = S.working || A.running;
    return `<div class="abar">
      <div class="arow"><label class="chk"><input type="checkbox" data-a="selall" ${R.allChecked(vis, S.sel) ? 'checked' : ''}> 全選択（見えている行だけ）</label>
        <span class="hint mono">${n}行を選択中</span><span class="spacer"></span>${filterHtml()}</div>
      <div class="arow"><b class="alabel">絵を決める</b>
        <button class="btn primary" data-a="fill" ${fillOff ? 'disabled' : ''} title="在庫に合う絵がある行を無料で割り当てます。賄えない行は無理に埋めません（ワンパターンになるため）">${esc(fillLabel)}</button>
        <select data-a="target" aria-label="生成の対象">${opt('missing', '未生成のみ（失敗含む）', A.settings.target)}${opt('failed', '失敗した行のみ', A.settings.target)}${opt('all', '全行（生成済みも作り直す）', A.settings.target)}</select>
        <button class="btn" data-a="gen" ${off || !total ? 'disabled' : ''} title="キャラ画像を生成します（NanoBanana・実課金）">${esc(genLabel)}</button>
        <label class="chk" title="OpenRouter経由のNanoBananaはFree表示でも実際は課金されます。OFFなら失敗行は失敗マークで続行します"><input type="checkbox" data-a="paid" ${A.settings.paid ? 'checked' : ''}> Gemini失敗時にOpenRouterへ課金退避${A.settings.paid ? `（残高 ${A.credits === null ? '取得中…' : `$${A.credits.toFixed(2)}`}）` : ''}</label>
        ${A.running ? '<button class="btn danger" data-a="stop">⏸ 中断</button>' : ''}</div>
      ${A.running || pct ? `<div class="progress" title="${esc(`${job.done ?? 0}成功 / ${job.failed ?? 0}失敗 / ${job.total ?? 0}`)}"><i style="width:${pct}%"></i></div>` : ''}
      <div class="arow hint">${planHtml()}</div>
      <div class="arow"><b class="alabel">選んだ行に</b>
        <button class="btn ok" data-a="approve" ${!okTargets.length || off ? 'disabled' : ''} title="絵を見て「これでいい」と確定します（無料・画像は生成しません）">✓ この絵でOK<span class="n">${okTargets.length || ''}</span></button>
        <button class="btn" data-a="regen" ${!n || off ? 'disabled' : ''} title="選択行を作り直します（課金・確認あり）">↻ まとめて生成し直す</button>
        <button class="btn" data-a="bg-sel" ${!n || S.working ? 'disabled' : ''} title="選択行の背景を自動で割り当て直します（手動選択も上書き・無料）">🏞️ 背景を割り当て直す</button>
        ${AXES.map(([k, label]) => `<select data-a="bulkslot" data-axis="${k}" aria-label="一括: ${label}" ${!n || S.working ? 'disabled' : ''}><option value="">一括: ${label.replace('(任意)', '')}</option>${((A.presets || {})[k] || []).map((o) => `<option value="${esc(o.id)}">${esc(o.label_ja)}</option>`).join('')}</select>`).join('')}</div>
      <div class="arow"><b class="alabel">下ごしらえ</b>
        <button class="btn" data-a="bg-missing" ${S.working ? 'disabled' : ''} title="背景が未割当の行に自動で割り当てます（無料・手動で選んだ行は変えません）">🏞️ 未割当の行に背景を自動割当</button>
        <button class="btn" data-a="prep" ${!newLines.length || S.working ? 'disabled' : ''} title="コマまたはプロンプトが無い行の分だけ、LLMで演出プロンプトを作り背景を割り当てます（無料・画像は生成しません）">🆕 プロンプトの無い行を下ごしらえ<span class="n">${newLines.length || ''}</span></button>
        <span class="hint">絵の在庫（許可・ラベル手直し）と背景アーカイブは director で</span></div>
      ${settingsHtml(true)}${msgHtml()}</div>`;
  }

  function planHtml() {
    if (A.planMsg && !A.plan) return esc(A.planMsg);
    if (!A.plan) return A.planBusy ? '在庫からの割当を試算しています…' : '';
    const p = A.plan, why = R.planReasons(p).slice(0, 4).map(([w, n]) => `${n}行 ${esc(w)}`).join('・');
    return `✂️ 在庫で ${p.from_stock}行（無料）／ 新規生成が要る ${p.need_generation}行 ≈ $${R.usd(p.need_generation_cuts || 0)}${why ? `　<span class="warnt">賄えない理由: ${why}</span>` : ''}
      <button class="btn ghost" data-a="replan" ${A.planBusy ? 'disabled' : ''}>🔄 試算</button>`;
  }

  function settingsHtml(withStyle) {
    const s = A.settings;
    const styles = A.styles.includes(s.style) || !s.style ? A.styles : [s.style, ...A.styles];
    return `<details class="aset" data-a="setopen" ${A.setOpen ? 'open' : ''}><summary>設定とプロンプトの作り直し（スタイル・アスペクト比・追加指示）</summary>
      <div class="arow">
        <label class="fld">スタイル<select data-a="style">${styles.map((x) => opt(x, x, s.style)).join('') || '<option value="">（既定）</option>'}</select></label>
        <label class="fld">アスペクト比<select data-a="aspect">${['16:9', '1:1', '9:16', '4:3'].map((x) => opt(x, x, s.aspect)).join('')}</select></label>
        <label class="fld grow">追加指示（任意・プロンプト生成LLMへ）<input type="text" data-a="extra" value="${esc(s.extra)}" placeholder="例: 全体的にコミカルに、背景は教室で統一"></label>
        <label class="fld">モデル（任意・空＝既定の無料枠）<input type="text" data-a="model" value="${esc(s.model)}" placeholder="anthropic/claude-sonnet-5" title="安全フィルタで拒否される機微なテーマは anthropic/claude-sonnet-5（有料・直接API）で通ります"></label>
        ${withStyle ? `<label class="chk"><input type="checkbox" data-a="overwrite" ${s.overwrite ? 'checked' : ''}> 既存プロンプトを作り直す（手編集した行は保持）</label>
        <button class="btn" data-a="prompts" ${S.working || A.running ? 'disabled' : ''}>✍ プロンプトを作り直す</button>` : ''}
      </div>
      <div class="hint">プロンプトは台本を確定した時に自動で用意されます。ここは作り直したい時だけ。スタイルやキャラ外見は生成時に自動で合成されるので、プロンプトに書くのは演出（表情・ポーズ・構図）だけ。</div></details>`;
  }

  function msgHtml() {
    return `${A.warnings.map((w) => `<div class="note warn">⚠ ${esc(w)}</div>`).join('')}${A.msg ? `<div class="note">${esc(A.msg)}</div>` : ''}`;
  }

  // ── 描画: 行の中身列 ──────────────────────────────────────
  function rowSelHtml(l) {
    return R.hasPanel(l) ? `<label class="rowsel" title="一括操作の対象にする"><input type="checkbox" data-a="selline" data-id="${esc(l.id)}" ${S.sel.has(l.id) ? 'checked' : ''}></label>` : '';
  }

  function detailHtml(l) {
    const a = l.aroll || {};
    if (!a.has_manifest) return '<div class="detail"><span class="chip none">Aロール未着手</span></div>';
    if (!a.panel) return `<div class="detail">${chip('bad', 'コマが無い')}<span class="hint">台本にあってAロールに無い行です（「プロンプトの無い行を下ごしらえ」でコマができます）</span></div>`;
    const t = thumb(l);
    const cast = (a.characters || []).length
      ? a.characters.map((c, i) => `<span class="chip cast" title="${i === 0 ? '話者（外せません）' : esc(c.id)}">${esc(c.name)}${i === 0 ? ' 🔒' : ''}</span>`).join('')
      : '<span class="chip info" title="話者のキャラが『画像を使わない』設定です。背景だけのコマになります">🎙 ナレーション</span>';
    const used = R.usedSlotSummary(a, A.presets);
    const cut = R.cutAction(l, lines());
    const cutBtn = cut === 'join' ? `<button class="btn ghost" data-a="cut" data-mode="join" data-id="${esc(l.id)}" title="この行は前の行と同じ絵にします（吹き出しだけ変わる）。同じ話者のときだけ有効">↑ 前の絵を使う</button>`
      : cut === 'split' ? `<button class="btn ghost" data-a="cut" data-mode="start" data-id="${esc(l.id)}" title="前の行と同じ絵を使っています。別の絵にします">✂ 別の絵にする</button>` : '';
    return `<div class="detail adetail">${t ? `<img class="thumb" src="${esc(t)}" loading="lazy" alt="">` : '<span class="thumb none">—</span>'}
      <div class="acol"><div class="ach">${cast}${R.statusChips(l).map(([c, x]) => chip(c, x)).join('')}</div>
      ${used ? `<div class="hint">${esc(used)}</div>` : ''}
      ${!a.matched && (a.characters || []).length && !a.cutout_slot_id ? '<div class="hint warnt" title="表情・ショット・アングルが揃うと在庫と照合できます">未照合（必ず課金生成）</div>' : ''}
      ${a.sync === 'stale' && a.source_text ? `<div class="hint warnt" title="${esc(a.source_text)}">生成時: ${esc(a.source_text)}</div>` : ''}</div>
      ${cutBtn}</div>`;
  }

  // ── 描画: 行モーダルの「絵」区画 ──────────────────────────
  function presetOpts(axis, cur) {
    return `<option value="">${AXES.find((x) => x[0] === axis)[1]}</option>${((A.presets || {})[axis] || []).map((o) => opt(o.id, o.label_ja, cur)).join('')}`;
  }

  function pickerHtml(l) {
    const P = A.picker;
    if (!P || P.lineId !== l.id) return '';
    const cid = (l.aroll.characters || [])[0]?.id || '';
    const items = P.items.map((it) => {
      const file = (it.cutout || '').split('/').pop();
      return `<button class="pitemx ${it.too_close ? 'close' : ''}" data-a="pick" data-slot="${esc(it.slot_id)}" data-file="${esc(file)}" title="向き: ${esc(R.facingGlyph(it))}">
        <span class="fg">${esc(R.facingGlyph(it))}</span><img src="${esc(api.cutoutUrl(cid, file))}" loading="lazy" alt="">
        <small>${esc(String(it.distance ?? ''))} / ${it.times_used || 0}回</small></button>`;
    }).join('');
    return `<div class="picker2"><div class="hint">直近の行から遠い順（クリックで割当・無料）。薄いものは直近に近すぎる絵です。</div>
      ${P.busy ? '<div class="hint">読み込み中…</div>' : ''}${P.note ? `<div class="note warn">${esc(P.note)}</div>` : ''}<div class="pgrid">${items}</div></div>`;
  }

  function bgPickerHtml(l) {
    const B = A.bg;
    if (!B || B.lineId !== l.id) return '';
    const framing = (l.aroll.slot || {}).shot || '';
    const list = R.bgFiltered(B.items, B.cat, framing);
    const facets = R.bgFacets(B.items);
    return `<div class="picker2"><div class="hint">背景アーカイブから選ぶ（クリックで差し替え・無料）</div>
      ${B.items.length ? `<div class="inline"><button class="btn ghost ${B.cat === 'all' ? 'on' : ''}" data-a="bgcat" data-cat="all">すべて ${B.items.length}</button>${facets.map(([c, n]) =>
        `<button class="btn ghost ${B.cat === c ? 'on' : ''}" data-a="bgcat" data-cat="${esc(c)}">${esc(c)} ${n}</button>`).join('')}</div>` : ''}
      ${B.busy ? '<div class="hint">読み込み中…</div>' : ''}${!B.busy && !list.length ? '<div class="hint">該当する背景がありません</div>' : ''}
      <div class="pgrid bg">${list.map((b) => `<button class="pitemx ${b.bg_id === l.aroll.background_id ? 'cur' : ''}" data-a="bgpick" data-bg="${esc(b.bg_id)}"><img src="${esc(bgUrl(b.bg_id))}" loading="lazy" alt=""><small>${esc(b.bg_id)}</small></button>`).join('')}</div></div>`;
  }

  function modalHtml(l) {
    const a = l.aroll || {};
    if (!a.has_manifest) return `<div class="box"><span class="note">この話数のAロールはまだ始まっていません。絵タブの「プロンプトを作る」から始めます。</span></div>`;
    if (!a.panel) return `<div class="box">${chip('bad', 'コマが無い')}<span class="note">台本にあってAロールに無い行です。</span>
      <div class="inline"><button class="btn primary" data-a="prep-line" data-id="${esc(l.id)}" ${S.working ? 'disabled' : ''}>🆕 この行を下ごしらえ</button><span class="hint">プロンプトを作り、背景を割り当てます（無料・画像は生成しません）</span></div></div>`;
    const t = thumb(l), off = S.working || A.running, slot = a.slot || {};
    const voiceMatch = R.voiceEmotionInPresets(l, A.presets);
    const cut = R.cutAction(l, lines());
    const addable = R.addableChars(A.chars, a);
    const canPick = (a.characters || []).length === 1;
    return `
      <div class="box"><div class="apic">${t ? `<img class="bigthumb" src="${esc(t)}" alt="">` : '<div class="bigthumb none">まだ絵がありません</div>'}
        <div class="acol"><div class="ach">${R.statusChips(l).map(([c, x]) => chip(c, x)).join('')}</div>
          ${a.status === 'done' && a.has_image ? (a.approved ? '<span class="pill ok">✓ この絵でOK（確定済み）</span>'
            : `<button class="btn ok" data-a="m-approve" ${off ? 'disabled' : ''} title="この行の絵はこれでいい、と確定します（無料）">✓ この絵でOK</button>`) : '<span class="hint">絵ができたら「この絵でOK」を押せます</span>'}
          ${a.sync === 'stale' && a.source_text ? `<div class="note warn">セリフが変わっています。生成時: ${esc(a.source_text)}</div>` : ''}
          ${a.speaker_changed ? '<div class="note warn">話者を変えた行です。在庫から絵を選び直してください。</div>' : ''}
          ${a.restale ? '<div class="note warn">♻️ 選び直した絵がまだ合成（PSD）に反映されていません。再合成が要ります（「仕上がり」区画の「この行を再合成」から。確定済み・手直し無しの行は選び直した直後に自動で再合成されます）。</div>' : ''}</div></div></div>
      <div class="pair">
        <div class="box"><span class="flabel">声の感情（台本）</span><span>${esc(l.emotion || 'neutral')}</span></div>
        <div class="box"><span class="flabel">絵の表情（Aロール）</span><span>${esc(a.emotion || '—')}</span>
          ${voiceMatch && voiceMatch !== a.emotion ? `<button class="btn ghost" data-a="m-voice" ${off ? 'disabled' : ''} title="絵の表情を声の感情に合わせ、在庫から無料で選び直します">🗣 絵の表情を声に合わせる</button>` : ''}</div></div>
      <div class="field"><span class="flabel">絵の希望（表情・ショット・アングル・ポーズ）</span>
        <div class="inline">${AXES.map(([k]) => `<select data-a="m-slot" data-axis="${k}" ${off ? 'disabled' : ''}>${presetOpts(k, slot[k] || '')}</select>`).join('')}</div>
        <span class="hint">語彙は固定です（在庫の分類が崩れるため）。表情・ショット・アングルの3つが揃うと、在庫から無料で選び直します。細かい指示は下のプロンプトへ。</span></div>
      ${a.cutout_slot_id ? `<div class="inline">${chip('cyan', `✂️ ${a.cutout_slot_id}`)}
        <button class="btn ghost" data-a="m-unset" ${off ? 'disabled' : ''} title="この行から外す（絵は在庫に残り、他の行では使える）">✕ 外す</button>
        <button class="btn ghost danger" data-a="m-reject" ${off ? 'disabled' : ''} title="絵そのものが失敗（指が6本など）。在庫からも削除します（ゴミ箱へ・復元可）">🗑 失敗</button></div>` : ''}
      <div class="inline">
        <button class="btn" data-a="m-regen" ${off || !a.has_prompt ? 'disabled' : ''} title="在庫に合う絵があれば無料、無ければ課金して作り直します">↻ 生成し直す</button>
        <button class="btn" data-a="m-picker" ${off || !canPick ? 'disabled' : ''} title="${canPick ? '在庫から選び直す（無料）' : 'キャラが1人に確定していない行は選べません'}">🔀 選び直す</button>
        <button class="btn ghost" data-a="m-fresh" ${off || !a.has_prompt ? 'disabled' : ''} title="在庫を使わず必ず新しく課金生成する">🎲 新しく生成（課金）</button></div>
      ${pickerHtml(l)}
      <div class="box"><span class="flabel">前の絵を使う</span><div class="inline">
        ${a.cut && a.cut.size > 1 ? `<span class="hint">この行を含む ${a.cut.size}行が同じ絵を使います</span>` : '<span class="hint">この行は自分の絵を使います</span>'}
        ${cut === 'join' ? `<button class="btn" data-a="cut" data-mode="join" data-id="${esc(l.id)}" ${off ? 'disabled' : ''}>↑ 前の絵を使う</button>` : ''}
        ${cut === 'split' ? `<button class="btn" data-a="cut" data-mode="start" data-id="${esc(l.id)}" ${off ? 'disabled' : ''}>✂ 別の絵にする</button>` : ''}
        ${cut ? `<button class="btn ghost" data-a="cut" data-mode="reset" data-id="${esc(l.id)}" ${off ? 'disabled' : ''} title="手直しを消して自動の判定に戻す">↺ 自動に戻す</button>` : ''}</div></div>
      <div class="box"><span class="flabel">背景（この行に敷く1枚）</span><div class="inline">
        ${a.background_id ? `<img class="bgthumb" src="${esc(bgUrl(a.background_id))}" alt="">` : ''}
        <span class="hint mono">${esc(a.background_id || '（背景未割当）')}</span>
        <button class="btn" data-a="m-bgpicker" ${off ? 'disabled' : ''}>🔀 背景</button></div>${bgPickerHtml(l)}</div>
      <details class="aset"><summary>映すキャラ・プロンプト（詳細）</summary>
        <div class="inline">${(a.characters || []).length ? a.characters.map((c, i) =>
          `<span class="chip cast">${esc(c.name)}${i === 0 ? ' 🔒' : ` <button class="x" data-a="m-rmchar" data-cid="${esc(c.id)}" title="外す" ${off ? 'disabled' : ''}>×</button>`}</span>`).join('') : '<span class="chip info">🎙 ナレーション（背景のみ）</span>'}
          ${(a.characters || []).length === 1 && addable.length ? `<select data-a="m-addchar" ${off ? 'disabled' : ''}><option value="">＋キャラ（2人写り）</option>${addable.map((c) => `<option value="${esc(c.char_id)}">${esc(c.name)}</option>`).join('')}</select>` : ''}</div>
        <span class="hint">先頭は話者で外せません。2人写りは自動では作らない方針なので、必要な行だけ手で足します。</span>
        <label class="flabel" for="m-prompt">プロンプト（英語で書いてください・画像モデルに渡すため）</label>
        <textarea id="m-prompt" data-a="m-prompt" rows="3" placeholder="（プロンプト未生成 — 下ごしらえで作るか手入力）">${esc(a.prompt)}</textarea></details>
      <span class="hint">単独キャラの行は、生成した瞬間に自動で背景を抜いて在庫へ登録します（無料）。2人以上写る行だけは背景付きのままです。</span>`;
  }

  // ── 操作 ────────────────────────────────────────────────
  const say = (msg, warnings = []) => { A.msg = msg; A.warnings = warnings; };
  const busyMsg = (m) => `${m}…`;

  /** 変更のあと画面を読み直す（絵の版を上げてサムネを取り直す）。 */
  async function refresh({ plan = true } = {}) {
    A.ver++;
    if (S.modal) S.modal.edited = false;
    await load();
    if (plan) await loadPlan();
    ctx.rerender();
    redrawModal();
  }

  async function fill() {
    const sel = selected();
    const missing = sel.filter((id) => !byId(id).aroll.picture);
    if (sel.length && missing.length) {
      await working(busyMsg('在庫で埋めています'), async () => {
        const d = await api.aroll.fillMissing(pid(), ep(), missing);
        const filled = (d.filled || []).length, need = d.need_generation || [];
        say(`${filled}行を無料で埋めました（未OKのまま。絵を見て「この絵でOK」を押してください）${need.length ? `／在庫でも埋まらない ${need.length}行は新規生成が必要です` : ''}`);
        await refresh();
        if (need.length) await offerGenerate(need);
      });
      return;
    }
    const p = A.plan;
    if (!p || !p.from_stock) return toast('在庫で賄える行はありません');
    if (!confirm(`在庫から ${p.from_stock} 行に割り当てます（無料・画像生成なし）。よろしいですか？`)) return;
    await working(busyMsg('在庫から割り当てています'), async () => {
      const d = await api.aroll.applyCutoutPlan(pid(), ep());
      say(`${d.applied} 行に割り当てました（${d.skipped} 行は在庫で賄えないため触っていません${d.kept_decided ? `・絵が決まっている ${d.kept_decided} 行はそのまま` : ''}）`);
      await refresh();
    });
  }

  /** 在庫でも埋まらない行を、金額を見せて確認してから生成する（director の「まとめて確定」と同じ確認）。 */
  async function offerGenerate(lineIds) {
    if (!confirm(`在庫でも埋まらない ${lineIds.length}行（同じ絵の行は1枚と数えます）は新規生成が必要です（概算 $${R.usd(lineIds.length)}・実課金）。生成しますか？\nキャンセルすると、埋まった分だけ処理して終わります。`)) return;
    await startGenerate({ line_ids: lineIds, only_missing: true });
  }

  async function startGenerate(body) {
    const s = A.settings;
    await api.aroll.generate(pid(), ep(), { ...body, allow_paid_fallback: s.paid, aspect: s.aspect, style: s.style || undefined });
    A.running = true;
    toast('🎬 生成を開始しました');
    startTimer();
    await pollStatus(true);
    ctx.rerender();
  }

  async function gen() {
    const s = A.settings, n = R.batchTargetCount(lines(), s.target);
    if (!n) return;
    if (!confirm(`${n}枚 生成します（概算 $${R.usd(n)}・NanoBanana実課金）。続行しますか？`)) return;
    const body = { only_missing: s.target !== 'all' };
    if (s.target === 'failed') body.line_ids = lines().filter((l) => l.aroll.status === 'failed').map((l) => l.id);
    await working(busyMsg('生成を頼んでいます'), () => startGenerate(body));
  }

  async function regen() {
    const ids = selected();
    if (!ids.length) return;
    if (!confirm(`選択した ${ids.length}行 を生成し直します（概算 $${R.usd(ids.length)}・実課金）。\n既存の絵は上書きされ、Photoshopでの再合成が必要になります。続行しますか？`)) return;
    await working(busyMsg('生成を頼んでいます'), () => startGenerate({ line_ids: ids, only_missing: false }));
  }

  async function approve(ids) {
    if (!ids.length) return;
    if (ids.length > 1 && !confirm(`選択した ${ids.length}行 の絵を「これでいい」と確定します。\n無料です（画像は生成しません）。続行しますか？`)) return;
    await working(busyMsg('確定しています'), async () => {
      const d = await api.aroll.approveImages(pid(), ep(), ids);
      say(`✓ ${d.approved}行の絵をOKにしました${d.registered ? `（うち${d.registered}件は旧データのため在庫へ新規登録）` : ''}${d.synced ? `（うち${d.synced}件は台本とのズレも解消）` : ''}`);
      await refresh({ plan: false });
    });
  }

  async function assignBg(lineIds, onlyMissing) {
    const body = onlyMissing ? { only_missing: true } : { only_missing: false, line_ids: lineIds };
    if (onlyMissing && !confirm('全行の背景を自動割当します（無料・画像生成なし）。手動で選んだ行は変更しません。よろしいですか？')) return;
    await working(busyMsg('背景を割り当てています'), async () => {
      const d = await api.aroll.autoAssignBackgrounds(pid(), ep(), body);
      say(`🏞️ ${d.assigned ?? 0}行に背景を割り当てました${d.unmatched ? `（候補なし ${d.unmatched}行）` : ''}${d.from_actual_shot ? `（実物優先 ${d.from_actual_shot}行）` : ''}`);
      await refresh({ plan: false });
    });
  }

  async function bulkSlot(axis, value) {
    const ids = selected();
    if (!ids.length || !value) return;
    await working(busyMsg(`${ids.length}行の${axis}を変更しています`), async () => {
      // 1行ずつ既存の行APIを叩く（一括用の口を増やさない＝挙動が枝分かれしない）
      for (const id of ids) await setSlot(id, axis, value, { quiet: true });
      say(`🎭 ${ids.length}行の${AXES.find((x) => x[0] === axis)[1]}を変更しました`);
      await refresh({ plan: false });
    });
  }

  /** 表情・ショット・アングル・ポーズの希望を保存する。3軸そろったら在庫から無料で選び直す（課金しない）。 */
  async function setSlot(lineId, axis, value, { quiet = false } = {}) {
    const a = byId(lineId).aroll;
    const slot = { emotion: '', shot: '', angle: '', pose: '', ...(a.slot || {}) };
    slot[axis] = value;
    if (!(slot.emotion && slot.shot && slot.angle)) { if (!quiet) toast('表情・ショット・アングルが揃うと保存されます'); return; }
    await api.aroll.updateLine(pid(), ep(), lineId, { slot: { emotion: slot.emotion, shot: slot.shot, angle: slot.angle, pose: slot.pose || null } });
    try { await api.aroll.generateLine(pid(), ep(), lineId, { use_library: true, library_only: true }); } catch { /* 在庫に無ければそのまま（課金しない） */ }
  }

  async function genLine(lineId, { fresh }) {
    const a = byId(lineId).aroll;
    if (fresh) {
      if (!confirm('ライブラリを使わず新規課金生成します（≈$0.04）。よろしいですか？')) return;
    } else if (a.has_image && !confirm(`${lineId} の絵を作り直します。\n\n・今の絵は上書きされます（元に戻せません）\n・ライブラリに一致が無ければ課金されます（≈$0.04）\n・合成済みの場合、Photoshopでの再合成が必要になります\n\nよろしいですか？`)) return;
    await working(busyMsg('生成しています（数十秒かかります）'), async () => {
      try {
        await api.aroll.generateLine(pid(), ep(), lineId, { allow_paid_fallback: A.settings.paid, ...(fresh ? { use_library: false } : {}) });
        say(`${lineId} の絵を作り直しました`);
      } catch (e) { say(`${lineId} の生成に失敗: ${e.message}`); throw e; } finally { await refresh(); }
    });
  }

  async function openPicker(lineId) {
    if (A.picker && A.picker.lineId === lineId) { A.picker = null; redrawModal(); return; }
    A.picker = { lineId, items: [], note: '', busy: true };
    redrawModal();
    const P = A.picker;
    try {
      const d = await api.aroll.candidates(pid(), ep(), lineId);
      P.items = d.items || [];
      if (!P.items.length) P.note = d.reason || '適格な在庫がありません（未許可・世代違い・生涯上限・感情の不一致）。director の📚キャラ在庫で許可してください';
    } catch (e) { P.note = `候補の取得に失敗しました: ${e.message}`; }
    P.busy = false;
    if (A.picker === P) redrawModal();
  }

  async function pick(lineId, slotId) {
    // パネル画像の差し替えではなく、合成素材の指定。自動の再合成は走らせない（D25＝確定済み ∧ 手直し無しの行だけ・W4b-2）
    await working(busyMsg('割り当てています'), async () => {
      await api.aroll.setCutout(pid(), ep(), lineId, slotId);
      A.picker = null;
      await refresh();
      // 確定済み ∧ 手直し無しの行だけ自動で再合成する（D25）。それ以外は「再合成が要る」の印だけ残す
      const ran = ctx.autoResync ? await ctx.autoResync(lineId) : false;
      say(ran ? `${lineId} に在庫の絵を割り当て、再合成を頼みました（Photoshop を使います）`
        : `${lineId} に在庫の絵を割り当てました。合成に反映するには再合成が要ります（確定済み・手直し無しの行は自動で再合成されます）`);
      ctx.rerender();
    });
  }

  async function openBg(lineId) {
    if (A.bg && A.bg.lineId === lineId) { A.bg = null; redrawModal(); return; }
    A.bg = { lineId, items: [], cat: 'all', busy: true };
    redrawModal();
    const B = A.bg;
    try { B.items = (await api.backgrounds()).backgrounds || []; } catch { B.items = []; }
    B.busy = false;
    if (A.bg === B) redrawModal();
  }

  // 章ごとの失敗（時間切れ・安全フィルタの拒否）の一言。成功した章は保存済み（2026-09-30）
  const failNote = (d) => {
    const f = (d && d.failed_sections) || [];
    if (!f.length) return '';
    const refused = f.some((x) => x.refused);
    return `（⚠ ${f.length}章は失敗: ${f.map((x) => x.section).join('・')}。${refused
      ? '安全フィルタの拒否は、設定の「モデル」に anthropic/claude-sonnet-5 を入れて作り直すと通ります'
      : 'もう一度押すと、失敗した章だけ作れます'}）`;
  };
  // すべての章が失敗した時（502）は、章ごとの理由を警告欄へ出す（トーストだけだと理由が読めない）
  async function promptCall(body) {
    try {
      return await api.aroll.prompts(pid(), ep(), body);
    } catch (e) {
      if (e.detail && e.detail.failed_sections) { say(`❌ ${e.message}${failNote(e.detail)}`, e.detail.warnings || []); return null; }
      throw e;
    }
  }

  async function prompts(all) {
    const s = A.settings;
    if (!all && s.overwrite && !confirm('既存のプロンプトを作り直します（手編集した行は保持）。LLMを使います（無料枠）。続行しますか？')) return;
    await working(busyMsg('LLMがプロンプトを作っています（章ごとに最大90秒で打ち切ります）'), async () => {
      const d = await promptCall({ extra_prompt: s.extra || null, overwrite: !all && s.overwrite, aspect: s.aspect, style: s.style || 'kamishibai', model: s.model || null });
      if (!d) return;
      say(`✔ ${(d.manifest.panels || []).filter((p) => p.prompt).length}行のプロンプトを用意しました${failNote(d)}`, d.warnings || []);
      await refresh();
    });
  }

  async function prep(targets) {
    const n = targets.length;
    if (!n) return;
    if (!confirm(`コマまたはプロンプトが無い ${n}行分のプロンプトを用意します（無料・画像生成なし）。続行しますか？`)) return;
    const s = A.settings, sections = [...new Set(targets.map((l) => l.section || 'main'))];
    await working(busyMsg('プロンプトを作っています'), async () => {
      const d = await promptCall({ sections, overwrite: false, aspect: s.aspect, style: s.style || 'kamishibai', model: s.model || null });
      if (!d) return;
      // 新しい行だけが背景未割当のはずなので only_missing で絞れる
      let bg = null;
      try { bg = await api.aroll.autoAssignBackgrounds(pid(), ep(), { only_missing: true }); } catch { /* 背景は後からでもよい */ }
      say(`✔ ${n}行を下ごしらえしました${bg && bg.assigned ? `（背景 ${bg.assigned}件を割当）` : ''}${failNote(d)}`, d.warnings || []);
      await refresh();
    });
  }

  async function setCut(lineId, mode) {
    await working(busyMsg('絵の共有を変更しています'), async () => {
      const body = mode === 'reset' ? { reset: true } : { boundary: mode };
      await api.aroll.setCut(pid(), ep(), lineId, body);
      say(mode === 'join' ? `${lineId} は前の行と同じ絵を使います` : mode === 'start' ? `${lineId} は別の絵にします（絵が無ければ「在庫で埋める」か生成）` : `${lineId} の絵の共有を自動に戻しました`);
      await refresh({ plan: false });
    });
  }

  async function editChars(lineId, chars) {
    await working(busyMsg('キャラを変更しています'), async () => {
      await api.aroll.updateLine(pid(), ep(), lineId, { characters: chars });
      await refresh({ plan: false });
    });
  }

  async function loadChars() {
    if (A.chars) return;
    try { A.chars = (await api.characters()).characters || []; } catch { A.chars = []; }
  }

  // ── イベント（app.js の委譲から呼ばれる） ─────────────────────
  async function click(el) {
    const a = el.dataset.a, id = el.dataset.id;
    const act = {
      filter: () => { S.afilter = el.dataset.f; ctx.rerender(); },
      fill, gen, regen, stop: async () => { try { await api.aroll.stop(pid(), ep()); toast('中断を頼みました（今の1枚が終わったら止まります）'); } catch (e) { fail(e); } },
      approve: () => approve(selected().filter((x) => { const g = byId(x).aroll; return g.status === 'done' && g.has_image; })),
      'bg-sel': () => assignBg(selected(), false),
      'bg-missing': () => assignBg(null, true),
      prep: () => prep(lines().filter(R.needsPrep)),
      'prep-line': () => prep([byId(id)]),
      prompts: () => prompts(false), 'prompts-all': () => prompts(true),
      replan: async () => { await loadPlan(); ctx.rerender(); },
      cut: () => setCut(id, el.dataset.mode),
      'm-approve': () => approve([S.modal.id]),
      'm-regen': () => genLine(S.modal.id, { fresh: false }),
      'm-fresh': () => genLine(S.modal.id, { fresh: true }),
      'm-picker': () => openPicker(S.modal.id),
      pick: () => pick(S.modal.id, el.dataset.slot),
      'm-unset': () => working(busyMsg('外しています'), async () => { await api.aroll.setCutout(pid(), ep(), S.modal.id, null); await refresh(); }),
      'm-reject': async () => {
        if (!confirm('この絵はどの行でも使えない失敗作として在庫から削除します（ゴミ箱へ移動・復元可）。\nよろしいですか？')) return;
        await working(busyMsg('削除しています'), async () => { const d = await api.aroll.rejectCutout(pid(), ep(), S.modal.id); say(`🗑 ${d.slot_id} を在庫から削除しました`); await refresh(); });
      },
      'm-bgpicker': () => openBg(S.modal.id),
      bgcat: () => { A.bg.cat = A.bg.cat === el.dataset.cat && el.dataset.cat !== 'all' ? 'all' : el.dataset.cat; redrawModal(); },
      bgpick: () => working(busyMsg('背景を差し替えています'), async () => {
        await api.aroll.updateLine(pid(), ep(), S.modal.id, { background_id: el.dataset.bg });
        A.bg = null; await refresh({ plan: false });
      }),
      'm-rmchar': () => { const l = byId(S.modal.id); return editChars(l.id, l.aroll.characters.map((c) => c.id).filter((c) => c !== el.dataset.cid)); },
      'm-voice': () => { const l = byId(S.modal.id); return working(busyMsg('絵の表情を合わせています'), async () => { await setSlot(l.id, 'emotion', R.voiceEmotionInPresets(l, A.presets), { quiet: true }); await refresh(); }); },
    }[a];
    if (act) await act();
  }

  async function change(el) {
    const a = el.dataset.a;
    if (a === 'selline') { el.checked ? S.sel.add(el.dataset.id) : S.sel.delete(el.dataset.id); ctx.rerender(); return; }
    if (a === 'selall') { S.sel = R.toggleAll(S.sel, visibleRows(), el.checked); ctx.rerender(); return; }
    const setv = { target: 'target', style: 'style', aspect: 'aspect', extra: 'extra', model: 'model' }[a];
    if (setv) { A.settings[setv] = el.value; A.touched = true; ctx.rerender(); return; }
    if (a === 'overwrite') { A.settings.overwrite = el.checked; A.touched = true; return; }
    if (a === 'paid') {
      A.settings.paid = el.checked;
      if (el.checked && A.credits === null) api.llmUsage().then((d) => { A.credits = typeof d.remaining === 'number' ? d.remaining : null; ctx.rerender(); }).catch(() => {});
      ctx.rerender(); return;
    }
    if (a === 'bulkslot') { const v = el.value; el.value = ''; await bulkSlot(el.dataset.axis, v); return; }
    if (a === 'm-slot') {
      const l = byId(S.modal.id);
      await working(busyMsg('保存しています'), async () => { await setSlot(l.id, el.dataset.axis, el.value); await refresh({ plan: false }); });
      return;
    }
    if (a === 'm-addchar') {
      const l = byId(S.modal.id);
      if (el.value) await editChars(l.id, [...l.aroll.characters.map((c) => c.id), el.value]);
      return;
    }
    if (a === 'm-prompt') {
      const l = byId(S.modal.id);
      await working(busyMsg('保存しています'), async () => { await api.aroll.updateLine(pid(), ep(), l.id, { prompt: el.value }); toast('プロンプトを保存しました'); await refresh({ plan: false }); });
    }
  }
  const input = (el) => { if (el.dataset.a === 'extra') { A.settings.extra = el.value; A.touched = true; } };
  const toggle = (el) => { if (el.dataset.a === 'setopen') A.setOpen = el.open; };

  return { A, enter, toolbarHtml, detailHtml, rowSelHtml, modalHtml, click, change, input, toggle, loadChars, stopTimer,
    hasBusyInput: () => { const e = document.activeElement; return !!e && /^(INPUT|TEXTAREA)$/.test(e.tagName) && e.type !== 'checkbox' && !!e.closest('#toolbar'); } };
}
