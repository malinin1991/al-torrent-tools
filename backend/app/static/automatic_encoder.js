/* Shared release policies; dialogs and action results live outside SSE fragments. */
(() => {
  let busy = false;
  let preserved = [];
  // Keep confirmed revisions locally: an earlier SSE request may finish after PUT.
  const savedPolicies = new Map();
  const result = () => document.getElementById("auto-encoder-result") || document.getElementById("releases-action-result");
  const root = () => document.getElementById("automatic-encoder-live");
  const showResult = message => { const node = result(); if (node) node.textContent = message; };
  const isAutomatic = policy => !policy.video_id && !policy.audio_id;
  function policySummary(policy) {
    if (isAutomatic(policy)) return "Автоматический · настройки Kensetsu по умолчанию";
    return (policy.video_label || policy.video_id || "Видео не задано") + " / " +
      (policy.audio_label || policy.audio_id || "Аудио не задано") + " · " +
      (policy.audio_downmix === "stereo" ? "даунмикс в стерео" : "без даунмикса");
  }
  function currentPolicy(row) {
    const rendered = JSON.parse(row.dataset.policy);
    const saved = savedPolicies.get(String(rendered.release_id));
    return saved && saved.revision > rendered.revision ? saved : rendered;
  }
  function renderPolicy(row, policy) {
    row.dataset.policy = JSON.stringify(policy);
    row.querySelectorAll("[data-auto-policy-summary]").forEach(node => { node.textContent = policySummary(policy); });
    row.querySelectorAll("[data-auto-policy-clear]").forEach(button => {
      if (isAutomatic(policy) && document.activeElement === button) {
        row.querySelector("[data-auto-policy-edit]")?.focus({preventScroll:true});
      }
      button.hidden = isAutomatic(policy);
    });
  }
  function applySavedPolicies(scope) {
    scope.querySelectorAll("[data-auto-release]").forEach(row => {
      const rendered = JSON.parse(row.dataset.policy);
      const saved = savedPolicies.get(String(rendered.release_id));
      if (saved && saved.revision > rendered.revision) renderPolicy(row, saved);
    });
  }
  async function request(url, method, body) {
    const response = await fetch(url, {method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Ошибка: HTTP " + response.status);
    return data;
  }
  document.addEventListener("click", async event => {
    const button = event.target.closest("[data-auto-policy-edit],[data-auto-policy-clear],[data-auto-backfill]");
    if (!button || busy) return;
    event.preventDefault();
    busy = true;
    button.disabled = true;
    showResult("");
    try {
      const row = button.closest("[data-auto-release]");
      if (row) {
        const policy = currentPolicy(row);
        const layers = button.hasAttribute("data-auto-policy-clear") ?
          {video_id:null, audio_id:null, audio_downmix:"none"} :
          await pickEncoderPreset({initial:policy, submitLabel:"Сохранить для релиза"});
        if (!layers) return;
        const confirmed = await request("/api/automatic-encoder/releases/" + policy.release_id, "PUT", {...layers, revision:policy.revision});
        const updated = {...policy, ...layers, ...confirmed};
        savedPolicies.set(String(updated.release_id), updated);
        document.querySelectorAll('[data-auto-release="' + updated.release_id + '"]').forEach(node => renderPolicy(node, updated));
        showResult("Параметры релиза сохранены. Принятые файлы заново не отправляются.");
      } else {
        const scope = root();
        const id = button.dataset.autoBackfill;
        const row = scope?.querySelector('[data-auto-release="' + id + '"]');
        if (!row) throw new Error("Обновите список отслеживания");
        const policy = currentPolicy(row);
        const files = [...scope.querySelectorAll(".auto-backfill-check:checked:not(:disabled)")].map(c => JSON.parse(c.dataset.source));
        if (!files.length) throw new Error("Выберите существующие файлы");
        const data = await request("/api/automatic-encoder/releases/" + id + "/backfill", "POST", {revision:policy.revision, files});
        scope.querySelectorAll(".auto-backfill-check:checked").forEach(c => { c.checked = false; });
        showResult("Добавлено в очередь: " + data.queued);
      }
    } catch (error) {
      showResult(error.message);
    } finally {
      busy = false;
      button.disabled = false;
      refreshUiSseTargets("automatic_encoder");
      refreshUiSseTargets("releases");
    }
  });
  document.body.addEventListener("htmx:beforeSwap", event => {
    const target = event.detail.target;
    if (!["automatic-encoder-live", "releases-live"].includes(target?.id)) return;
    if (busy) { event.detail.shouldSwap = false; return; }
    if (target.id === "automatic-encoder-live" && event.detail.shouldSwap) {
      preserved = [...target.querySelectorAll(".auto-backfill-check:checked")].map(c => c.dataset.selectionKey);
    }
  });
  document.body.addEventListener("htmx:afterSwap", event => {
    const target = event.detail.target;
    if (!["automatic-encoder-live", "releases-live"].includes(target?.id)) return;
    applySavedPolicies(target);
    if (target.id !== "automatic-encoder-live") return;
    target.querySelectorAll(".auto-backfill-check").forEach(c => {
      c.checked = !c.disabled && preserved.includes(c.dataset.selectionKey);
    });
    const remaining = [...target.querySelectorAll(".auto-backfill-check")].filter(c => c.checked).length;
    if (remaining < preserved.length) showResult("Снят выбор файлов, которые больше недоступны в этой версии.");
    const state = target.querySelector("[data-auto-state]");
    if (state) target.dataset.uiSseUrl = "/automatic-encoder/live?" + state.dataset.liveQuery;
  });
})();
