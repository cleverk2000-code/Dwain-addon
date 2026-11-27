/* Admin-only local editor; all requests use the authenticated HA session. */
class DwainAddonManager extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode:"open"});
    this.busy = false;
    this.snapshot = null;
    this.plan = null;
    this.initialized = false;
  }

  set hass(value) {
    this._hass = value;
    if (this.isConnected && !this.initialized) this.start();
  }

  connectedCallback() {
    if (this._hass && !this.initialized) this.start();
  }

  start() {
    this.initialized = true;
    const root = this.shadowRoot;
    root.innerHTML = `
      <style>
        :host{display:block;color:var(--primary-text-color);padding:16px}
        main{max-width:1050px;margin:auto;display:grid;gap:14px}
        section{padding:16px;border-radius:12px;background:var(--card-background-color);border:1px solid var(--divider-color)}
        h1,h2,p{margin:0 0 10px} h2{font-size:1.05rem}
        label{display:block;margin:6px 0}
        textarea,select{box-sizing:border-box;width:100%;font:inherit;color:inherit;background:var(--secondary-background-color);border:1px solid var(--divider-color);border-radius:6px;padding:10px}
        textarea{min-height:180px;font-family:monospace}
        button{font:inherit;min-height:44px;padding:8px 14px;margin:4px 4px 4px 0;border:1px solid var(--divider-color);border-radius:8px;color:inherit;background:var(--secondary-background-color);cursor:pointer}
        button:disabled{opacity:.45;cursor:default}
        pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;font-size:13px}
        .actions{display:flex;flex-wrap:wrap;gap:4px}
        #status{white-space:pre-wrap;overflow-wrap:anywhere;min-height:1.5em}
      </style>
      <main>
        <h1>Dwain Manager</h1>
        <section><p>기존 Dwains Lovelace의 배치만 변경합니다. 실제 기기는 제어하지 않습니다.</p>
          <button id="reload">설정 새로 읽기</button>
          <div id="status" role="status" aria-live="polite"></div>
          <details><summary>현재 방 · 엔티티 · 배치 설정</summary><pre id="snapshot"></pre></details>
        </section>
        <section><h2>배치 변경</h2><label for="patch">JSON 변경사항 (ID는 위 조회 결과 참조)</label>
          <textarea id="patch" spellcheck="false" aria-label="JSON 변경사항">{
  "areas": {},
  "entities": {}
}</textarea>
          <div class="actions"><button id="preview">미리보기</button><button id="apply">변경 적용</button></div>
          <pre id="diff" aria-live="polite"></pre>
        </section>
        <section><h2>백업 복구</h2><label for="backups">백업 ID (최근 순)</label><select id="backups"></select>
          <div class="actions"><button id="restorePreview">복구 미리보기</button><button id="restore">복구 적용</button><button id="recover">중단된 작업 복구</button></div>
        </section>
      </main>`;
    this.el = id => root.getElementById(id);
    this.el("patch").addEventListener("input", () => this.invalidate());
    this.el("backups").addEventListener("change", () => this.invalidate());
    for (const [id, fn] of [
      ["reload",()=>this.read()],["preview",()=>this.preview("preview")],
      ["apply",()=>this.commit("apply")],["restorePreview",()=>this.preview("restore_preview")],
      ["restore",()=>this.commit("restore")],["recover",()=>this.recover()]
    ]) this.el(id).addEventListener("click",()=>this.run(fn));
    this.controls();
    this.run(()=>this.read());
  }

  controls() {
    const s = this.snapshot;
    const pending = Boolean(s?.pending);
    const noSnapshot = !s || pending;
    this.el("reload").disabled = this.busy;
    this.el("patch").disabled = this.busy;
    this.el("backups").disabled = this.busy;
    this.el("preview").disabled = this.busy || noSnapshot;
    this.el("apply").disabled = this.busy || noSnapshot || this.plan?.action !== "apply";
    this.el("restorePreview").disabled = this.busy || noSnapshot || !this.el("backups").value;
    this.el("restore").disabled = this.busy || noSnapshot || this.plan?.action !== "restore";
    this.el("recover").disabled = this.busy || !pending;
  }

  invalidate() {
    this.plan = null;
    this.el("diff").textContent = "";
    this.controls();
  }

  async api(method, body) {
    if (!this._hass?.callApi) throw new Error("HA 인증 연결을 사용할 수 없습니다.");
    return this._hass.callApi(method, "dwain_addon/layout", body);
  }

  async run(fn) {
    if (this.busy) return;
    this.busy = true;
    this.controls();
    try {
      await fn();
    } catch (err) {
      this.invalidate();
      this.el("status").textContent = "실패: " + (err?.body?.error || err?.message || String(err)) + " — 설정을 다시 읽고 확인하세요.";
    } finally {
      this.busy = false;
      this.controls();
    }
  }

  async read() {
    this.invalidate();
    this.snapshot = null;
    this.el("status").textContent = "설정 읽는 중";
    const result = await this.api("GET");
    this.snapshot = result;
    this.el("snapshot").textContent = JSON.stringify(result, null, 2);
    const select = this.el("backups");
    select.replaceChildren();
    for (const id of result.backups || []) {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = id;
      select.append(option);
    }
    this.el("status").textContent = result.pending
      ? "중단된 작업이 있습니다. 복구 후 변경할 수 있습니다: " + result.pending
      : "설정 조회 완료 · revision " + result.revision.slice(0,12);
  }

  async preview(action) {
    this.invalidate();
    if (!this.snapshot || this.snapshot.pending) throw new Error("설정을 다시 읽으세요.");
    const body = {action, revision:this.snapshot.revision};
    if (action === "preview") body.patch = JSON.parse(this.el("patch").value);
    else body.backup_id = this.el("backups").value;
    const result = await this.api("POST",body);
    this.el("diff").textContent = JSON.stringify(result,null,2);
    this.plan = {...body,action:action==="preview"?"apply":"restore"};
    this.el("status").textContent = "미리보기 완료 · 적용 전 변경 내용을 확인하세요.";
  }

  async commit(action) {
    if (this.plan?.action !== action) throw new Error("먼저 미리보기를 실행하세요.");
    if (!window.confirm(action === "restore" ? "선택한 백업으로 복구할까요?" : "미리보기 변경사항을 적용할까요?")) return;
    const body = this.plan;
    this.invalidate();
    this.snapshot = null;
    const result = await this.api("POST",body);
    await this.read();
    this.el("status").textContent = result.changed ? "완료 · 자동 백업 " + result.backup_id : "변경 없음";
  }

  async recover() {
    if (!this.snapshot?.pending) throw new Error("중단된 작업 없음");
    if (!window.confirm("중단된 작업을 변경 전 상태로 복구할까요?")) return;
    const body = {action:"recover",backup_id:this.snapshot.pending};
    this.invalidate();
    this.snapshot = null;
    await this.api("POST",body);
    await this.read();
    this.el("status").textContent = "중단된 작업 복구 완료";
  }
}
if (!customElements.get("dwain-addon-manager")) customElements.define("dwain-addon-manager",DwainAddonManager);
