(function () {
  const panel = document.getElementById('trial-readiness-panel');
  if (!panel) return;
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function render(data) {
    const badge = data.overall_status === 'DEMO_ELIGIBLE' ? 'good' : 'bad';
    const dataGate = (data.gates || []).find((gate) => gate.name === 'data');
    const datasets = dataGate?.evidence?.datasets || [];
    panel.innerHTML = '<div class="panel-heading"><h3>' + esc(data.lifecycle_stage) + '</h3><span class="badge ' + badge + '">' + esc(data.overall_status) + '</span></div>' +
      '<p>' + esc(data.safety_message) + '</p>' +
      '<div class="summary-list">' + (data.gates || []).map((gate) => '<div class="summary-row"><span>' + esc(gate.name) + '</span><span class="' + (gate.status === 'PASS' ? 'positive' : gate.status === 'BLOCKED' ? 'negative' : '') + '">' + esc(gate.status) + ' · ' + esc(gate.detail) + '</span></div>').join('') + '</div>' +
      '<div class="table-wrap"><table class="data-table"><thead><tr><th>Dataset</th><th>Candles</th><th>Target</th><th>Remaining</th><th>Quality</th></tr></thead><tbody>' +
      (datasets.map((row) => '<tr><td>' + esc(row.symbol) + ' ' + esc(row.timeframe) + '</td><td>' + esc(row.candles) + '</td><td>' + esc(row.target) + '</td><td>' + esc(row.remaining) + '</td><td>' + esc(JSON.stringify(row.quality)) + '</td></tr>').join('') || '<tr><td colspan="5">No pilot dataset audit is available.</td></tr>') +
      '</tbody></table></div><p class="muted"><strong>Next action:</strong> ' + esc(data.next_action) + '</p>';
  }
  async function refresh() {
    try {
      const response = await fetch('/api/trial-readiness', {cache: 'no-store'});
      if (!response.ok) throw new Error('Trial-readiness request failed');
      render(await response.json());
    } catch (error) {
      panel.innerHTML = '<div class="empty-state">Trial-readiness report unavailable: ' + esc(error.message) + '</div>';
    }
  }
  refresh();
  setInterval(refresh, 15000);
})();
