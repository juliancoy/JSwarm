import test from 'node:test';
import assert from 'node:assert/strict';
import {fetchVersions, formatReleaseTime} from '../view/versions-model.js';
const response = (status, body) => ({ok: status === 200, status, json: async () => body});

test('uses native version report when deployed', async () => {
  const report = {nodes: [{node: 'one', source_revision: 'a'.repeat(64)}]};
  const calls = [];
  const value = await fetchVersions(async path => {calls.push(path); return response(200, path === '/api/tasks/releases' ? {releases: {['a'.repeat(64)]: {released_at: '2026-11-13T20:00:00+00:00'}}} : report);});
  assert.equal(value.nodes[0].released_at, '2026-11-13T20:00:00+00:00');
  assert.equal(value.nodes[0].source_revision, report.nodes[0].source_revision);
  assert.deepEqual(calls, ['/versions.json', '/api/tasks/releases']);
});

test('404 falls back to existing inspector and rejects stale or mismatched versions', async () => {
  const samples = {
    fresh: {host: 'fresh', status: 'live', fetched_at: 99, node_revision: 'a'.repeat(64)},
    stale: {host: 'stale', status: 'live', fetched_at: 1, node_revision: 'b'.repeat(64)},
    wrong: {host: 'fresh', status: 'live', fetched_at: 99, node_revision: 'c'.repeat(64)},
    loading: {host: 'loading', status: 'loading'},
    broken: null,
  };
  const value = await fetchVersions(async (path, options) => {
    assert.equal(options.cache, 'no-store');
    if (path === '/versions.json') return response(404);
    if (path === '/api/jswarm-web') return response(200, {node: 'fresh', nodes: Object.keys(samples).map(name => ({name}))});
    const name = new URL(path, 'http://localhost').searchParams.get('host');
    if (!samples[name]) throw new Error('offline');
    return response(200, samples[name]);
  }, () => 100000);
  assert.equal(value.nodes[0].source_revision, 'a'.repeat(64));
  assert.equal(value.source, 'authenticated-machine-inspector');
  assert.ok(value.nodes.slice(1).every(node => node.source_revision === null));
  assert.equal(value.nodes[3].status, 'loading');
  assert.equal(value.nodes[4].status, 'unavailable');
  assert.equal(value.checked_at, '1970-01-01T00:01:40.000Z');
});

test('server failures are reported instead of masked by fallback', async () => {
  let calls = 0;
  await assert.rejects(fetchVersions(async () => {calls++; return response(503);}), /HTTP 503/);
  assert.equal(calls, 1);
});

test('missing and invalid release dates remain explicit', () => {
  assert.equal(formatReleaseTime({}), 'Not recorded');
  assert.equal(formatReleaseTime({released_at: 'bad'}), 'Not recorded');
  assert.equal(formatReleaseTime({released_at: '2026-11-13T20:00:00Z'}), new Date('2026-11-13T20:00:00Z').toLocaleString());
});
