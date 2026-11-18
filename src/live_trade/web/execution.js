(function () {
  const panel = document.getElementById('demo-execution-panel');
  const analysis = document.getElementById('demo-analysis');
  const start = document.getElementById('demo-execution-start');
  const stop = document.getElementById('demo-execution-stop');
  if (!panel || !analysis || !start || !stop) return;
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function render(data) {
    const decision = data.decision || {};
    const analysis = data.analysis || {};
    const execution = data.execution || {};
    panel.innerHTML = '<div class="panel-heading"><h3>' + esc(data.stage || 'IDLE') + '</h3><span class="badge ' + (data.status === 'POSITION_OPEN' ? 'good' : data.status === 'BLOCKED' ? 'bad' : 'neutral') + '">' + esc(data.status || 'IDLE') + '</span></div>' +
      '<div class="summary-list"><div class="summary-row"><span>Mode</span><span>' + esc(data.mode) + '</span></div><div class="summary-row"><span>Execution enabled</span><span>' + esc(data.execution_enabled) + '</span></div><div class="summary-row"><span>Account</span><span>' + esc(data.account?.server || data.terminal?.name || 'Unavailable') + ' · ' + esc(data.account?.currency || '') + '</span></div><div class="summary-row"><span>Decision</span><span>' + esc(decision.decision || 'Unavailable') + ' · ' + esc(decision.label || '') + '</span></div><div class="summary-row"><span>Rationale</span><span>' + esc(decision.rationale || data.error || 'No decision yet') + '</span></div><div class="summary-row"><span>Technical score</span><span>' + esc(analysis.technical_score ?? 'Unavailable') + '</span></div><div class="summary-row"><span>Entry / SL / TP</span><span>' + esc(decision.entry) + ' / ' + esc(decision.stop_loss) + ' / ' + esc(decision.take_profit) + '</span></div><div class="summary-row"><span>Risk</span><span>' + esc(JSON.stringify(data.risk || {})) + '</span></div><div class="summary-row"><span>Execution</span><span>' + esc(execution.status || data.message || data.error || 'Not submitted') + '</span></div></div><p class="muted">No trade is considered executed until broker confirmation and position reconciliation are returned.</p>';
  }
  async function refresh() {
    try {
      const response = await fetch('/api/execution', {cache: 'no-store'});
      render(await response.json());
    } catch (error) {
      panel.innerHTML = '<div class="empty-state">Execution status unavailable: ' + esc(error.message) + '</div>';
    }
  }
  async function command(path) {
    start.disabled = true;
    stop.disabled = true;
    try {
      const response = await fetch(path, {method: 'POST'});
      const data = await response.json();
      render(data);
    } catch (error) {
      panel.innerHTML = '<div class="empty-state">Execution command failed: ' + esc(error.message) + '</div>';
    } finally {
      start.disabled = false;
      stop.disabled = false;
      refresh();
    }
  }
  start.addEventListener('click', () => command('/api/session/start-demo-execution'));
  analysis.addEventListener('click', () => command('/api/session/analyze-demo-execution'));
  stop.addEventListener('click', () => command('/api/session/stop-demo-execution'));
  refresh();
  setInterval(refresh, 15000);
})();
