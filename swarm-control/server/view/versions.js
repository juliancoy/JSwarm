import {fetchVersions} from './versions-model.js';
const status = document.getElementById('version-status');
const rows = document.getElementById('version-nodes');
const refresh = document.getElementById('refresh-versions');
const download = document.getElementById('download-versions');
let downloadURL = null;
let pending = false;
function clearDownload() {
  if (downloadURL) URL.revokeObjectURL(downloadURL);
  downloadURL = null;
  download.removeAttribute('href');
  download.setAttribute('aria-disabled', 'true');
}
async function update() {
  if (pending) return;
  pending = true;
  refresh.disabled = true;
  try {
    const report = await fetchVersions();
    const fragment = document.createDocumentFragment();
    for (const node of report.nodes) {
      const row = document.createElement('tr');
      const labels = {connected: 'Connected', loading: 'Checking…', 'version-unavailable': 'Version unavailable', unavailable: 'Unavailable'};
      for (const value of [node.node, labels[node.status] || 'Unknown', node.source_revision || 'Not reported', node.protocol || 'Not reported']) {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.append(cell);
      }
      fragment.append(row);
    }
    rows.replaceChildren(fragment);
    clearDownload();
    downloadURL = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2) + '\n'], {type: 'application/json'}));
    download.href = downloadURL;
    download.removeAttribute('aria-disabled');
    status.textContent = `Checked ${new Date(report.checked_at).toLocaleString()} by ${report.reporting_node}.`;
  } catch (error) {
    rows.replaceChildren();
    clearDownload();
    status.textContent = `Versions unavailable: ${error.message}. Retry with Refresh.`;
  } finally {
    pending = false;
    refresh.disabled = false;
  }
}
clearDownload();
refresh.addEventListener('click', update);
document.addEventListener('visibilitychange', () => { if (!document.hidden) void update(); });
setInterval(() => { if (!document.hidden) void update(); }, 15000);
void update();
