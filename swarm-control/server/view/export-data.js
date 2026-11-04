import { loadSettings } from './settings.js';

export async function collectDashboardData(read, settings = {}, browserObservation = null) {
  const errors = [];
  const get = async path => {
    try { return await read(path); }
    catch (error) { errors.push({ source: path, error: String(error.message || error) }); return null; }
  };
  const started = new Date().toISOString();
  const [network, tests, resources] = await Promise.all([get('/api/devices?resolve=0'), get('/api/distributed-tests'), get('/api/resources')]);
  const reports = tests?.run ? [tests.run] : [];
  const pending = (tests?.history || []).filter(item => item.id !== tests.run?.id);
  // Bound concurrent requests while including every retained report.
  for (let i = 0; i < pending.length; i += 4) {
    const batch = await Promise.all(pending.slice(i, i + 4).map(item => get('/api/distributed-tests?id=' + encodeURIComponent(item.id))));
    for (const result of batch) if (result?.run) reports.push(result.run);
  }
  return {
    schema_version: 1,
    kind: 'map.local-observations-for-llm',
    collection_started_at: started,
    exported_at: new Date().toISOString(),
    complete: errors.length === 0,
    scope: {
      network: 'Full current collector response, including retained device history, perspectives, router observations, traffic, DNS and geography. Unknown, stale and unavailable observations retain their original labels.',
      resources: 'Latest bounded enrolled-node CPU, memory, GPU, disk, socket and LAN observations, with aggregate five-minute histories. Per-record timestamps and scope apply.',
      application_tests: 'Every retained test report, including job status, phases, exit codes, durations, exact source hashes and bounded log tails. Not just the selected host, filtered graph, or latest run.',
      limits: { test_retention_days: tests?.retention_days ?? 30, test_run_limit: 100, test_log_lines_per_job: 80, traffic: 'Collector retention applies; expired observations and packet payloads are not available.' },
      credentials: 'No private SSH keys, passwords, deployment credentials, raw browser storage, or configuration files are read. Export is copied locally; nothing is sent to an LLM service.',
      consistency: 'Live observations may change during collection; original per-record timestamps are included.',
    },
    network,
    resources,
    application_tests: tests ? { running: tests.running, current_run_id: tests.run?.id ?? null, history: tests.history, reports } : null,
    browser: { map_settings: settings, benchmark_status_text: browserObservation },
    collection_errors: errors,
  };
}

function showManualCopy(text, filename = 'map-local-observations.json') {
  let dialog = document.getElementById('json-export-dialog');
  if (!dialog) {
    dialog = document.createElement('dialog');dialog.id = 'json-export-dialog';
    const title = document.createElement('h2');title.textContent = 'JSON for your LLM';
    const help = document.createElement('p');help.textContent = 'Clipboard access is unavailable. Select the JSON and press Ctrl+C (or Command+C), or download the JSON file.';
    const area = document.createElement('textarea');area.readOnly = true;area.setAttribute('aria-label', 'Exported JSON');
    area.style.cssText = 'width:100%;height:50vh;resize:vertical;font:12px monospace;';
    const select = document.createElement('button');select.textContent = 'Select all JSON';select.onclick = () => { area.focus();area.select(); };
    const download = document.createElement('button');download.textContent = 'Download JSON';download.onclick = () => {
      const url = URL.createObjectURL(new Blob([area.value], { type: 'application/json' }));
      const link = document.createElement('a');link.href = url;link.download = dialog.dataset.filename;link.click();setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    const close = document.createElement('button');close.textContent = 'Close';close.onclick = () => dialog.close();
    dialog.style.cssText = 'width:min(92vw,1000px);max-height:90vh;background:#111a21;color:#ecf1f5;border:1px solid #597b78;border-radius:12px;padding:22px;';
    dialog.append(title, help, area, select, download, close);document.body.append(dialog);
  }
  dialog.dataset.filename = filename;
  dialog.querySelector('textarea').value = text;
  dialog.showModal();dialog.querySelector('textarea').focus();dialog.querySelector('textarea').select();
}

export async function copyJson(text, filename = 'map-local-observations.json') {
  let copied = false;
  if (navigator.clipboard?.writeText) { try { await navigator.clipboard.writeText(text);copied = true; } catch { /* HTTP and denied permissions use the selection fallback. */ } }
  if (!copied) {
    const area = document.createElement('textarea');area.value = text;area.setAttribute('aria-label', 'JSON clipboard buffer');area.style.cssText = 'position:fixed;left:-9999px;top:0;';document.body.append(area);area.focus();area.select();
    try { copied = document.execCommand('copy'); } catch { copied = false; } finally { area.remove(); }
  }
  if (!copied) showManualCopy(text, filename);
  return copied;
}

export function installExportButton() {
  const button = document.getElementById('copy-json');
  if (!button) return;
  let status = document.getElementById('copy-json-status');
  if (!status) { status = document.createElement('span');status.id = 'copy-json-status';status.className = 'muted';status.setAttribute('role', 'status');status.setAttribute('aria-live', 'polite');button.insertAdjacentElement('afterend', status); }
  button.onclick = async () => {
    button.disabled = true;status.textContent = 'Collecting all available observations…';
    try {
      const report = await collectDashboardData(async path => {
        const response = await fetch(path, { cache: 'no-store' });
        if (!response.ok) throw Error('HTTP ' + response.status);
        return response.json();
      }, loadSettings(), document.getElementById('benchmark-result')?.textContent || null);
      const text = JSON.stringify(report, null, 2);
      const copied = await copyJson(text);
      const size = (new Blob([text]).size / 1024).toFixed(0) + ' KiB';
      status.textContent = (copied ? 'JSON copied · ' : 'JSON ready to select · ') + size + (report.complete ? ' · All available sources included.' : ' · ' + report.collection_errors.length + ' sources unavailable; errors included.');
    } catch (error) { status.textContent = 'Export failed: ' + error.message; }
    finally { button.disabled = false; }
  };
}

if (typeof document !== 'undefined') installExportButton();
