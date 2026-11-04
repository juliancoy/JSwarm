// Keep the latest displayed snapshot while hidden; refresh immediately on return.
export function pollVisible(task, interval, environment = globalThis) {
  const {document} = environment;
  let timer, busy = false, stopped = false;
  async function tick() {
    if (stopped || document.hidden || busy) return;
    busy = true;
    try { await task(); }
    finally {
      busy = false;
      if (!stopped && !document.hidden) timer = environment.setTimeout(tick, interval);
    }
  }
  function visibility() {
    environment.clearTimeout(timer);
    if (!document.hidden) void tick();
  }
  document.addEventListener('visibilitychange', visibility);
  void tick();
  return () => {
    stopped = true;
    environment.clearTimeout(timer);
    document.removeEventListener('visibilitychange', visibility);
  };
}
