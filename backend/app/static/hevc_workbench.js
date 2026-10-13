/* Page-local selection; shared encoder helpers retain durable request IDs. */
(() => {
  "use strict";
  const root = () => document.getElementById("hevc-workbench-live");
  if (!root()) return;
  const actions = document.getElementById("hevc-workbench-actions");
  const result = document.getElementById("hevc-workbench-result");
  const notice = document.getElementById("hevc-workbench-selection-notice");
  const selected = new Set();
  let busy = false;
  let pendingRefresh = false;
  let preserved = null;
  const checks = () => [...root().querySelectorAll(".hevc-encode-check")];
  const enabled = () => root().querySelector("[data-encoder-enabled='1']") !== null;
  function sync() {
    const visible = new Set();
    checks().forEach(check => {
      const key = check.dataset.selectionKey;
      if (!check.disabled) visible.add(key);
      check.checked = !check.disabled && selected.has(key);
    });
    let removed = 0;
    [...selected].forEach(key => { if (!visible.has(key)) { selected.delete(key); removed += 1; } });
    if (removed) notice.textContent = `Снят выбор с ${removed}: версия исчезла или больше не доступна для отправки.`;
    document.getElementById("hevc-workbench-count").textContent = String(selected.size);
    actions.querySelector("[data-hevc-send]").disabled = busy || !enabled() || !selected.size;
    actions.querySelector("[data-hevc-select]").disabled = busy || !enabled();
    actions.querySelector("[data-hevc-clear]").disabled = busy;
    actions.querySelector("[data-hevc-refresh]").disabled = busy;
  }
  function refresh() {
    if (busy) { pendingRefresh = true; return; }
    const state = root().querySelector("[data-hevc-state]");
    if (state && window.htmx) window.htmx.ajax("GET", state.dataset.hevcLiveUrl, {target:root(), swap:"innerHTML settle:0 show:none"});
  }
  root().addEventListener("change", event => {
    const check = event.target.closest(".hevc-encode-check");
    if (!check || check.disabled || busy) return;
    if (check.checked && selected.size < 100) selected.add(check.dataset.selectionKey);
    else selected.delete(check.dataset.selectionKey);
    sync();
  });
  actions.querySelector("[data-hevc-select]").addEventListener("click", () => {
    checks().filter(check => !check.disabled).slice(0, 100).forEach(check => selected.add(check.dataset.selectionKey));
    sync();
  });
  actions.querySelector("[data-hevc-clear]").addEventListener("click", () => { selected.clear(); sync(); });
  actions.querySelector("[data-hevc-refresh]").addEventListener("click", refresh);
  actions.querySelector("[data-hevc-send]").addEventListener("click", async () => {
    if (busy || !selected.size || !enabled()) return;
    const chosen = checks().filter(check => check.checked && !check.disabled);
    const ids = chosen.map(check => Number(check.dataset.fileId));
    if (!ids.length || ids.length > 100) return;
    busy = true;
    sync();
    try {
      result.textContent = "Выбор параметров…";
      const layers = await window.pickEncoderPreset();
      if (!layers) { result.textContent = ""; return; }
      result.textContent = "Отправка…";
      const payload = await window.submitEncoderFiles(ids, layers, {noRepeatPrompt:true});
      window.showEncoderReceipt(result, payload);
      const accepted = new Set((payload.file_ids || []).map(String));
      // Keep errors selected, but prevent an uncertain/active source from another POST.
      const protectedIds = new Set((payload.items || []).filter(item => ["accepted", "unknown", "sending"].includes(item.status)).map(item => String(item.file_id)));
      chosen.forEach(check => {
        if (accepted.has(check.dataset.fileId)) selected.delete(check.dataset.selectionKey);
        if (protectedIds.has(check.dataset.fileId)) check.disabled = true;
      });
      pendingRefresh = true;
    } catch (error) {
      result.textContent = error.message || "Ошибка отправки";
      const link = document.createElement("a");
      link.href = error.historyUrl || "/encoder-history";
      link.textContent = " Открыть журнал Kensetsu";
      result.append(link);
      pendingRefresh = true;
    } finally {
      busy = false;
      sync();
      if (pendingRefresh) { pendingRefresh = false; refresh(); }
    }
  });
  document.body.addEventListener("htmx:beforeSwap", event => {
    if (event.detail.target !== root()) return;
    if (busy) { event.detail.shouldSwap = false; pendingRefresh = true; return; }
    preserved = {scrollY:window.scrollY, open:[...root().querySelectorAll("details[open][data-sse-key]")].map(item => item.dataset.sseKey)};
  });
  document.body.addEventListener("htmx:afterSwap", event => {
    if (event.detail.target !== root()) return;
    const state = root().querySelector("[data-hevc-state]");
    if (state) root().dataset.uiSseUrl = state.dataset.hevcLiveUrl;
    sync();
    if (preserved) {
      const saved = preserved;
      preserved = null;
      const open = new Set(saved.open);
      root().querySelectorAll("details[data-sse-key]").forEach(item => { item.open = open.has(item.dataset.sseKey); });
      window.requestAnimationFrame(() => window.scrollTo(0, saved.scrollY));
    }
  });
  sync();
})();
