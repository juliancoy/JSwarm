const status = document.getElementById('version-status');
const rows = document.getElementById('version-nodes');
const refresh = document.getElementById('refresh-versions');
let pending = false;
async function update() {
  if (pending) return;
  pending = true;
  refresh.disabled = true;
  try {
    const response = await fetch('/versions.json', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const report = await response.json();
    const fragment = document.createDocumentFragment();
    for (const node of report.nodes) {
      const row = document.createElement('tr');
      const labels = {connected: 'Connected', 'version-unavailable': 'Version unavailable', unavailable: 'Unavailable'};
      for (const value of [node.node, labels[node.status] || 'Unknown', node.source_revision || 'Not reported', node.protocol || 'Not reported']) {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.append(cell);
      }
      fragment.append(row);
    }
    rows.replaceChildren(fragment);
    status.textContent = `Checked ${new Date(report.checked_at).toLocaleString()} by ${report.reporting_node}.`;
  } catch (error) {
    rows.replaceChildren();
    status.textContent = `Versions unavailable: ${error.message}. Retry with Refresh.`;
  } finally {
    pending = false;
    refresh.disabled = false;
  }
}
refresh.addEventListener('click', update);
document.addEventListener('visibilitychange', () => { if (!document.hidden) void update(); });
setInterval(() => { if (!document.hidden) void update(); }, 15000);
void update();
