// 全行再生（浮動バー）の状態機械。DOMを触らない（Audio は差し替え可能・Nodeでテストできる）。
// director の TTS タブの全行再生と同じ操作: 全行／ここから／一時停止と再開／前後の行／停止／続きから。

export class Player {
  /**
   * @param {object} o
   * @param {(url: string) => HTMLAudioElement} o.createAudio  音声要素を作る（onended / onerror を後から代入する）
   * @param {() => Array<{id: string, url: string}>} o.getItems 再生できる行（台本の順）。呼ぶたびに最新を返す
   * @param {() => void} o.onChange                            状態が変わった（バーを描き直す）
   * @param {Storage|null} [o.storage]                         「続きから」の記録先（無ければ記録しない）
   * @param {string} [o.storageKey]
   */
  constructor({ createAudio, getItems, onChange, storage = null, storageKey = 'wb-last' }) {
    this.createAudio = createAudio;
    this.getItems = getItems;
    this.onChange = onChange || (() => {});
    this.storage = storage;
    this.storageKey = storageKey;
    this.status = 'idle';        // idle | playing | paused
    this.id = null;              // いま鳴らしている（一時停止中の）行
    this.audio = null;
  }

  _items() { return this.getItems() || []; }
  get index() { return this._items().findIndex((x) => x.id === this.id); }
  get total() { return this._items().length; }
  get last() { try { return this.storage && this.storage.getItem(this.storageKey); } catch { return null; } }
  _remember(id) { try { this.storage && this.storage.setItem(this.storageKey, id); } catch { /* 記録できなくても再生は続ける */ } }

  _emit() { this.onChange({ status: this.status, id: this.id, index: this.index, total: this.total }); }

  _drop() {
    if (this.audio) { this.audio.onended = null; this.audio.onerror = null; try { this.audio.pause(); } catch { /* ignore */ } }
    this.audio = null;
  }

  /** index の行から順に鳴らす。範囲外なら終わる。 */
  _playAt(index) {
    const items = this._items();
    const item = items[index];
    this._drop();
    if (!item) { this.status = 'idle'; this.id = null; this._emit(); return; }
    const audio = this.createAudio(item.url);
    this.audio = audio;
    this.id = item.id;
    this.status = 'playing';
    this._remember(item.id);
    // 差し替え・一時停止・停止で古くなった音声のイベントでは次へ進めない（this.audio と一致する時だけ）
    audio.onended = () => { if (this.audio === audio) this._playAt(this.index + 1); };
    audio.onerror = () => { if (this.audio === audio) this._playAt(this.index + 1); };   // 壊れた行は飛ばす
    const p = audio.play();
    if (p && p.catch) p.catch(() => { /* 差し替えによる AbortError 等。古い音声なら無視 */ });
    this._emit();
  }

  playAll() { this._playAt(0); }

  playFrom(id) {
    const i = this._items().findIndex((x) => x.id === id);
    if (i >= 0) this._playAt(i);
  }

  /** 最後に鳴らした行から。記録の行が消えていたら最初から。 */
  continueFromLast() {
    const i = this._items().findIndex((x) => x.id === this.last);
    this._playAt(i >= 0 ? i : 0);
  }

  pause() {
    if (this.status !== 'playing' || !this.audio) return;
    this.audio.pause();
    this.status = 'paused';
    this._emit();
  }

  resume() {
    if (this.status !== 'paused' || !this.audio) return;
    const p = this.audio.play();
    if (p && p.catch) p.catch(() => {});
    this.status = 'playing';
    this._emit();
  }

  /** Space キー用: 鳴っていれば止め、止まっていれば再開、何も無ければ続きから。 */
  toggle() {
    if (this.status === 'playing') this.pause();
    else if (this.status === 'paused') this.resume();
    else this.continueFromLast();
  }

  next() { if (this.status !== 'idle') this._playAt(this.index + 1); }

  prev() { if (this.status !== 'idle') this._playAt(Math.max(0, this.index - 1)); }

  stop() {
    this._drop();
    this.status = 'idle';
    this.id = null;
    this._emit();
  }
}
