(function () {
  const panel = document.getElementById('demo-execution-panel');
  const analysis = document.getElementById('demo-analysis');
  const start = document.getElementById('demo-monitor-start');
  const monitorStop = document.getElementById('demo-monitor-stop');
  const stop = document.getElementById('demo-execution-stop');
  if (!panel || !analysis || !start || !monitorStop || !stop) return;
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function render(data) {
    const decision = data.decision || {};
    const analysis = data.analysis || {};
    const execution = data.execution || {};
    const monitoring = data.monitoring || {};
    panel.innerHTML = '<div class="panel-heading"><h3>' + esc(data.stage || monitoring.state || 'IDLE') + '</h3><span class="badge ' + (data.status === 'POSITION_OPEN' ? 'good' : data.status === 'BLOCKED' ? 'bad' : 'neutral') + '">' + esc(data.status || monitoring.state || 'IDLE') + '</span></div>' +
      '<div class="summary-list"><div class="summary-row"><span>Mode</span><span>' + esc(data.mode) + '</span></div><div class="summary-row"><span>Monitoring</span><span>' + esc(monitoring.state || 'STOPPED') + ' · cycles ' + esc(monitoring.cycles ?? 0) + '</span></div><div class="summary-row"><span>New entries</span><span>' + esc(data.new_entries_enabled === true ? 'ENABLED' : 'BLOCKED') + '</span></div><div class="summary-row"><span>Execution enabled</span><span>' + esc(data.execution_enabled) + '</span></div><div class="summary-row"><span>Account</span><span>' + esc(data.account?.server || data.terminal?.name || 'Unavailable') + ' · ' + esc(data.account?.currency || '') + '</span></div><div class="summary-row"><span>Decision</span><span>' + esc(decision.decision || 'Unavailable') + ' · ' + esc(decision.label || '') + '</span></div><div class="summary-row"><span>Qualification</span><span>' + esc(decision.qualification_status || data.stage || 'Unavailable') + '</span></div><div class="summary-row"><span>Rationale</span><span>' + esc(decision.rationale || data.error || data.message || 'No decision yet') + '</span></div><div class="summary-row"><span>Technical score</span><span>' + esc(analysis.technical_score ?? 'Unavailable') + '</span></div><div class="summary-row"><span>Entry / SL / TP</span><span>' + esc(decision.entry) + ' / ' + esc(decision.stop_loss) + ' / ' + esc(decision.take_profit) + '</span></div><div class="summary-row"><span>Risk gate</span><span>' + esc(data.risk_gate?.message || '') + '</span></div><div class="summary-row"><span>Execution</span><span>' + esc(execution.status || data.message || data.error || 'Not submitted') + '</span></div></div><p class="muted">Autonomous entries remain blocked until all configured risk limits pass.</p>';
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
    monitorStop.disabled = true;
    stop.disabled = true;
    try {
      const response = await fetch(path, {method: 'POST'});
      const data = await response.json();
      render(data);
    } catch (error) {
      panel.innerHTML = '<div class="empty-state">Execution command failed: ' + esc(error.message) + '</div>';
    } finally {
      start.disabled = false;
      monitorStop.disabled = false;
      stop.disabled = false;
      refresh();
    }
  }
  start.addEventListener('click', () => command('/api/monitoring/start'));
  monitorStop.addEventListener('click', () => command('/api/monitoring/stop'));
  analysis.addEventListener('click', () => command('/api/session/analyze-demo-execution'));
  stop.addEventListener('click', () => command('/api/session/stop-demo-execution'));
  refresh();
  setInterval(refresh, 15000);
})();
