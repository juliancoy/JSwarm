// Share recent read responses between panels and same-origin embedded frames.
// Mutations always go directly to the server.
export function createSnapshotFetch(store, request = globalThis.fetch.bind(globalThis), now = Date.now) {
  return async function snapshotFetch(path, options = {}) {
    const url = new URL(path, globalThis.location?.origin || 'http://localhost');
    const ttl = url.pathname === '/api/devices' ? 3000 : 2000;
    const historical=url.pathname==='/api/distributed-tests'&&url.searchParams.has('id');
    if ((options.method || 'GET') !== 'GET') return request(path, options);
    const key = url.href;
    let entry = store.get(key);
    if (!entry || options.cache==='reload' || (entry.finished !== null && now() - entry.finished >= (entry.ttl||ttl))) {
      entry = {finished: null};
      entry.promise = request(path, options).then(async response => {
        // Buffer once so every consumer gets an independent response body.
        const body = await response.arrayBuffer();
        if (!response.ok && store.get(key)===entry) store.delete(key);
        if(response.ok&&historical){
          try{const data=JSON.parse(new TextDecoder().decode(body)),run=data.run;
            if(run?.id===url.searchParams.get('id')&&run.finished&&['passed','failed','partial','stopped','interrupted','unavailable','timed_out'].includes(run.status)&&!['waiting_for_deployment','running'].includes(run.post_deployment?.status)){entry.ttl=300000;entry.historical=true;}
          }catch{}
        }
        entry.finished = now();
        if(entry.historical){const old=[...store].filter(([k,v])=>k!==key&&v.historical);while(old.length>=10)store.delete(old.shift()[0]);}
        return {body, status: response.status, headers: response.headers};
      }).catch(error => { if(store.get(key)===entry)store.delete(key); throw error; });
      store.set(key, entry);
    }
    const result = await entry.promise;
    return new Response(result.body.slice(0), {status: result.status, headers: result.headers});
  };
}
let owner = globalThis;
try { if (globalThis.top && globalThis.location && globalThis.top.location.origin === globalThis.location.origin) owner = globalThis.top; } catch {}
const store = owner.__lanSnapshots ||= new Map();
export const snapshotFetch = createSnapshotFetch(store);
