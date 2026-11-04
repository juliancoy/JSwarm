const run = document.getElementById('benchmark-run');
const cancel = document.getElementById('benchmark-cancel');
const output = document.getElementById('benchmark-result');
let controller;
run.onclick = async () => {
  controller = new AbortController();
  const signal = controller.signal;
  const timer = setTimeout(() => controller.abort('30-second test limit reached'), 30000);
  run.disabled = true; cancel.hidden = false;
  output.textContent = `Source: this browser (${location.host} connection). Destination: ${location.origin}, map.local server on compai. Starting…`;
  async function request(path, options = {}) {
    const response = await fetch(path, {...options, signal, cache: 'no-store'});
    if (!response.ok) throw Error(response.status === 429 ? 'Benchmark busy or expired. Retry after 45 seconds.' : `HTTP ${response.status}`);
    return response;
  }
  try {
    const {token, bytes} = await (await request('/api/benchmark/start', {method:'POST'})).json();
    const path = kind => `/api/benchmark/${kind}?token=${encodeURIComponent(token)}`;
    const times = [];
    for (let i=0; i<6; i++) {
      const start = performance.now();
      await (await request(path('ping'))).arrayBuffer();
      if (i) times.push(performance.now() - start); // First request warms the connection.
    }
    times.sort((a,b)=>a-b);
    const latency = `HTTP RTT median ${times[2].toFixed(2)} ms (min ${times[0].toFixed(2)}, max ${times[4].toFixed(2)}; 5 samples).`;
    output.textContent = `${latency} Measuring download…`;
    async function transfer(kind) {
      let elapsed = 0;
      const payload = kind === 'upload' ? new Uint8Array(bytes) : null;
      if (payload) for (let i=0; i<bytes; i+=65536) crypto.getRandomValues(payload.subarray(i,i+65536));
      for (let i=0; i<3; i++) {
        const start = performance.now();
        const response = await request(kind === 'download' ? path(kind) : '/api/benchmark/upload', payload ? {method:'POST', headers:{'X-Benchmark-Token':token, 'Content-Type':'application/octet-stream'},body:payload} : {});
        const body = await response.arrayBuffer();
        if (kind === 'download' && body.byteLength !== bytes) throw Error('Incomplete download');
        elapsed += performance.now() - start;
      }
      return `${(bytes*3*8/elapsed/1000).toFixed(2)} Mbps (${(elapsed/1000).toFixed(3)} s)`;
    }
    const download = await transfer('download');
    output.textContent = `${latency} Download ${download}. Measuring upload…`;
    const upload = await transfer('upload');
    output.textContent = `Source: this browser → destination: ${location.origin} (compai). Download (server → browser): ${download}. Upload (browser → server): ${upload}. ${latency} 12 MiB per direction; ${new Date().toLocaleTimeString()}.`;
  } catch (error) {
    output.textContent += ` Test ${signal.aborted ? 'stopped: '+(signal.reason || 'cancelled') : 'failed: '+error.message}. Incomplete results are not a completed benchmark.`;
  } finally {
    clearTimeout(timer); run.disabled = false; cancel.hidden = true; controller = null;
  }
};
cancel.onclick = () => controller?.abort('Cancelled by user');
