/* Keep sync failure selection and paging across HTMX/SSE replacements. */
(() => {
  const selected = new Map();
  function syncPanel() { return document.querySelector('[data-sync-failures-job]'); }
  function selection(panel) {
    const key = panel.dataset.syncFailuresJob;
    if (!selected.has(key)) selected.set(key, new Set());
    return selected.get(key);
  }
  function restoreSelection() {
    const panel = syncPanel();
    if (!panel) return;
    const ids = selection(panel);
    panel.querySelectorAll('.sync-failure-select').forEach(input => {
      input.checked = ids.has(Number(input.dataset.failureId));
    });
    panel.querySelectorAll('[data-sync-selected-count]').forEach(el => { el.textContent = ids.size; });
    const detail = document.getElementById('job-detail-live');
    if (detail && detail.dataset.uiSseUrl) {
      const url = new URL(detail.dataset.uiSseUrl, location.origin);
      url.searchParams.set('failure_after_id', panel.dataset.failureAfterId || '0');
      detail.dataset.uiSseUrl = url.pathname + url.search;
    }
  }
  async function jsonPost(url, payload) {
    const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Запрос отклонён. Проверьте выбранные данные.');
    return body;
  }
  document.addEventListener('change', event => {
    if (!event.target.matches('.sync-failure-select')) return;
    const panel = syncPanel();
    if (!panel) return;
    const ids = selection(panel), id = Number(event.target.dataset.failureId);
    if (event.target.checked) ids.add(id); else ids.delete(id);
    restoreSelection();
  });
  document.body.addEventListener('htmx:afterSwap', restoreSelection);
  // jobs.html rewrites live URLs after settle; preserve the failure cursor then too.
  document.body.addEventListener('htmx:afterSettle', restoreSelection);
  document.addEventListener('click', async event => {
    const retry = event.target.closest('[data-sync-retry-selected],[data-sync-retry-all]');
    if (retry && !retry.disabled) {
      const panel = syncPanel(), result = panel.querySelector('[data-sync-retry-result]');
      const ids = [...selection(panel)];
      if (retry.hasAttribute('data-sync-retry-selected') && !ids.length) {
        result.textContent = 'Выберите ошибки для повтора.';
        return;
      }
      retry.disabled = true;
      result.textContent = 'Подготовка точечного повтора…';
      try {
        const body = await jsonPost(`/api/jobs/${panel.dataset.syncFailuresJob}/retry-failures`, {
          failure_ids:retry.hasAttribute('data-sync-retry-selected') ? ids : null,
        });
        location.assign(body.url);
      } catch (error) { result.textContent = error.message; }
      finally { retry.disabled = false; }
    }
    const outbox = event.target.closest('[data-outbox-retry]');
    if (!outbox || outbox.disabled) return;
    const unknown = outbox.dataset.deliveryUnknown === '1';
    if (unknown && !confirm('Доставка неизвестна. Проверьте Telegram: повтор может создать дубль. Вы проверили и хотите повторить?')) return;
    const result = document.getElementById('telegram-outbox-action');
    outbox.disabled = true;
    try {
      await jsonPost(`/api/telegram-outbox/${outbox.dataset.outboxRetry}/retry`, {
        bot_key:outbox.dataset.botKey, acknowledge_unknown:unknown,
      });
      result.textContent = 'Повтор поставлен в очередь. Доставку выполнит процесс бота.';
      refreshUiSseTargets('telegram-outbox');
    } catch (error) { result.textContent = error.message; }
    finally { outbox.disabled = false; }
  });
  document.body.addEventListener('telegram-outbox-updated', () => refreshUiSseTargets('telegram-outbox'));
  restoreSelection();
})();
