// Older nodes expose release hashes through the authenticated machine inspector.
export async function fetchVersions(fetcher = fetch, now = () => Date.now()) {
  const read = async path => {
    const response = await fetcher(path, {cache: 'no-store', signal: AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  };
  const response = await fetcher('/versions.json', {cache: 'no-store', signal: AbortSignal.timeout(8000)});
  if (response.ok) return response.json();
  if (response.status !== 404) throw new Error(`HTTP ${response.status}`);
  const context = await read('/api/jswarm-web');
  const nodes = await Promise.all(context.nodes.map(async ({name}) => {
    try {
      const value = await read('/api/tasks/machine?host=' + encodeURIComponent(name));
      const age = now() / 1000 - value.fetched_at;
      const fresh = value.host === name && value.status === 'live' && Number.isFinite(value.fetched_at) && age >= -5 && age <= 30;
      const revision = fresh && typeof value.node_revision === 'string' && /^[a-f0-9]{64}$/.test(value.node_revision) ? value.node_revision : null;
      return {node: name, status: revision ? 'connected' : value.status === 'loading' ? 'loading' : 'unavailable',
        source_revision: revision, protocol: null,
        observed_at: Number.isFinite(value.fetched_at) ? new Date(value.fetched_at * 1000).toISOString() : null};
    } catch {
      return {node: name, status: 'unavailable', source_revision: null, protocol: null, observed_at: null};
    }
  }));
  return {schema: 'jswarm-node-versions.v1', reporting_node: context.node,
    checked_at: new Date(now()).toISOString(), source: 'authenticated-machine-inspector', nodes};
}
