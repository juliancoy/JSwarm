import './swarm-navigation.js';
// Source-workspace pages follow local edits; immutable node bundles do not opt in.
if (!window.__jswarmLive) {
  window.__jswarmLive = true;
  const key = `jswarm-live:${location.pathname}`;
  const selectIds = ['composition-dataset', 'composition-run', 'alignment-sample', 'pipeline-sample', 'length-axis'];
  let previous = document.querySelector("script[data-source-revision]")?.dataset.sourceRevision;
  let candidate, checking = false, stopped = false;
  function restore() {
    let selections;
    try { selections = JSON.parse(sessionStorage.getItem(key) || '{}'); sessionStorage.removeItem(key); }
    catch { return; }
    if (!selections || typeof selections !== "object" || Array.isArray(selections)) return;
    const apply = () => {
      for (const [id, value] of Object.entries(selections)) {
        if (!selectIds.includes(id)) { delete selections[id]; continue; }
        const select = document.getElementById(id);
        if (!select || ![...select.options].some(option => option.value === value)) continue;
        delete selections[id];
        if (select.value !== value) { select.value = value; select.dispatchEvent(new Event('change', {bubbles: true})); }
      }
      if (!Object.keys(selections).length) observer.disconnect();
    };
    const observer = new MutationObserver(apply);
    observer.observe(document.documentElement, {childList: true, subtree: true});
    apply(); setTimeout(() => observer.disconnect(), 15000);
  }
  restore();
  function reload() {
    const selections = {};
    for (const id of selectIds) {
      const select = document.getElementById(id);
      if (select) selections[id] = select.value;
    }
    try { sessionStorage.setItem(key, JSON.stringify(selections)); } catch {}
    location.reload();
  }
  async function check() {
    if (checking || stopped || document.hidden) return;
    checking = true;
    try {
      const response = await fetch('/api/jswarm-web/source-revision', {cache:'no-store', signal:AbortSignal.timeout(5000)});
      if (response.status === 404) { stopped = true; return; }
      if (!response.ok) return;
      const {revision} = await response.json();
      if (!/^[a-f0-9]{64}$/.test(revision)) return;
      if (previous === undefined) previous = revision;
      else if (revision === previous) candidate = undefined;
      else if (candidate === revision) {
        // Wait until typing has finished; read-only viewer selectors are safe.
        if (!document.activeElement?.matches('input,textarea,[contenteditable=true]')) reload();
      } else candidate = revision;
    } catch { /* Keep the current page during a server restart; retry next tick. */ }
    finally { checking = false; }
  }
  check();
  let timer = setInterval(check, 1500);
  window.addEventListener("pageshow", event => { if (event.persisted) { stopped = false; timer = setInterval(check,1500); check(); } });
  document.addEventListener('visibilitychange', check);
  window.addEventListener('pagehide', () => { clearInterval(timer); stopped = true; });
}
