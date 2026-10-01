// director の窓口・状態取得・他コンテナのプロキシを呼ぶ薄いクライアント（Docs/LINE_WORKBENCH_PLAN.md §3・§5）。
// 同じオリジン（director が /workbench/ を配信）なので相対パスでよい。fetch は差し替え可能（テスト用）。

export class ApiError extends Error {
  constructor(status, detail) {
    const msg = typeof detail === 'string' ? detail : (detail && (detail.message || JSON.stringify(detail))) || `HTTP ${status}`;
    super(msg);
    this.status = status;
    this.detail = detail;
  }
}

export function createApi(fetchImpl = (...a) => fetch(...a)) {
  const q = (params) => {
    const e = Object.entries(params || {}).filter(([, v]) => v !== undefined && v !== null && v !== false);
    return e.length ? '?' + e.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v === true ? 'true' : v)}`).join('&') : '';
  };
  async function call(method, path, body, params) {
    const init = { method, headers: {} };
    if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    const res = await fetchImpl(path + q(params), init);
    let data = null;
    try { data = await res.json(); } catch { /* 本文なし */ }
    if (!res.ok) throw new ApiError(res.status, data && data.detail !== undefined ? data.detail : data);
    return data;
  }
  const ep = (pid, n) => `/projects/${encodeURIComponent(pid)}/episodes/${n}`;
  // 絵（scrapping-agent）は director の中継 /api/scrapping/ 経由。1話ぶんのAロールは .../aroll 以下
  const ar = (pid, n) => `/api/scrapping${ep(pid, n)}/aroll`;
  return {
    projects: () => call('GET', '/projects'),
    episodes: (pid) => call('GET', `/projects/${encodeURIComponent(pid)}/episodes`),
    view: (pid, n) => call('GET', `${ep(pid, n)}/workbench`),
    // 行の操作の窓口（Docs/LINE_WORKBENCH_PLAN.md §3）
    lineOp: (pid, n, op, body, { dryRun = false } = {}) => call('POST', `${ep(pid, n)}/lines/${op}`, body || {}, { dry_run: dryRun }),
    undo: (pid, n, { force = false } = {}) => call('POST', `${ep(pid, n)}/lines/undo`, undefined, { force }),
    // 確定・LLMの案の採用（§4）
    confirm: (pid, n, lineIds) => call('POST', `${ep(pid, n)}/lines/confirm`, lineIds ? { line_ids: lineIds } : {}),
    startConfirmations: (pid, n) => call('POST', `${ep(pid, n)}/lines/confirmations/start`, { baseline: true }),
    proposal: (pid, n) => call('GET', `${ep(pid, n)}/lines/proposal`),
    adopt: (pid, n, body, { dryRun = false } = {}) => call('POST', `${ep(pid, n)}/lines/adopt`, body || {}, { dry_run: dryRun }),
    // 音声（tts-agent のプロキシ）。頼んだ行のうち最新でない行だけが作り直される
    runLines: (pid, n, lineIds) => call('POST', `/api/tts/projects/${encodeURIComponent(pid)}/run/lines`, { line_ids: lineIds }, { episode: n }),
    // 絵（Aロール）。課金するもの・上書きするものは呼び出し側（画面）が確認してから呼ぶ
    aroll: {
      prompts: (pid, n, body) => call('POST', `${ar(pid, n)}/prompts`, body),
      // コマの下ごしらえ（LLMなし）: 指名した行のプロンプト無しのコマを、サブ行は親から引き継ぎ・独立した行はルールで埋める
      syncStructure: (pid, n, body) => call('POST', `${ar(pid, n)}/lines/sync-structure`, body || {}),
      status: (pid, n) => call('GET', `${ar(pid, n)}/status`),
      stop: (pid, n) => call('POST', `${ar(pid, n)}/stop`),
      generate: (pid, n, body) => call('POST', `${ar(pid, n)}/generate`, body),
      batchPlan: (pid, n, body) => call('POST', `${ar(pid, n)}/batch/plan`, body),
      generateLine: (pid, n, lineId, body) => call('POST', `${ar(pid, n)}/generate/line/${encodeURIComponent(lineId)}`, body || {}),
      updateLine: (pid, n, lineId, body) => call('PUT', `${ar(pid, n)}/lines/${encodeURIComponent(lineId)}`, body),
      approveImages: (pid, n, lineIds) => call('POST', `${ar(pid, n)}/approve-images`, { line_ids: lineIds }),
      fillMissing: (pid, n, lineIds) => call('POST', `${ar(pid, n)}/fill-missing`, { line_ids: lineIds }),
      cutoutPlan: (pid, n) => call('GET', `${ar(pid, n)}/cutout-plan`),
      applyCutoutPlan: (pid, n, lineIds) => call('POST', `${ar(pid, n)}/cutout-plan/apply`, lineIds ? { line_ids: lineIds } : {}),
      candidates: (pid, n, lineId, limit = 24) => call('GET', `${ar(pid, n)}/cutout-candidates`, undefined, { line_id: lineId, limit }),
      setCutout: (pid, n, lineId, slotId) => call('POST', `${ar(pid, n)}/lines/${encodeURIComponent(lineId)}/cutout`, { slot_id: slotId }),
      rejectCutout: (pid, n, lineId) => call('POST', `${ar(pid, n)}/lines/${encodeURIComponent(lineId)}/cutout/reject`),
      autoAssignBackgrounds: (pid, n, body) => call('POST', `${ar(pid, n)}/backgrounds/auto_assign`, body),
      setCut: (pid, n, lineId, body) => call('PUT', `${ar(pid, n)}/cuts/${encodeURIComponent(lineId)}`, body),
    },
    // 仕上がり（ホスト工程＝Photoshop の常駐へのジョブ。director はキューに書くだけ・HTTPでは繋がない）
    psassist: {
      jobs: (pid, n) => call('GET', `${ep(pid, n)}/psassist/jobs`),
      createJob: (pid, n, body) => call('POST', `${ep(pid, n)}/psassist/jobs`, body),
      cancel: (pid, n, jobId) => call('POST', `${ep(pid, n)}/psassist/jobs/${encodeURIComponent(jobId)}/cancel`),
      pendingCutouts: (charId) => call('GET', `/api/scrapping/panel-library/${encodeURIComponent(charId)}/ps-status`),
    },
    presets: () => call('GET', '/api/scrapping/panel/presets'),
    styles: () => call('GET', '/api/scrapping/imagegen/styles'),
    backgrounds: () => call('GET', '/api/scrapping/backgrounds'),
    characters: () => call('GET', '/api/scrapping/characters'),
    llmUsage: () => call('GET', '/api/scripting/llm-usage'),
    backgroundUrl: (bgId) => `/api/scrapping/backgrounds/file/${encodeURIComponent(bgId)}.png`,
    cutoutUrl: (charId, filename) => `/api/scrapping/characters/${encodeURIComponent(charId)}/panel_library/cutout/${encodeURIComponent(filename)}`,
    // 1行だけ作り直す（force＝キャッシュを無視して新規生成。TTSは生成ごとに結果が変わる＝テイクのやり直し）。同期で返る
    retakeLine: (pid, n, lineId) => call('POST', `/api/tts/projects/${encodeURIComponent(pid)}/run/line/${encodeURIComponent(lineId)}`, undefined, { episode: n, force: true }),
    audioUrl: (pid, n, lineId, v) => `/api/tts/audio/project/${encodeURIComponent(pid)}/${encodeURIComponent(lineId)}.wav?episode=${n}&v=${v}`,
  };
}
