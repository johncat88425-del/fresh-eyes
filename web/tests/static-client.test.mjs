import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFreshEyesClient } from '../demo/static-client.js';

test('read-only client refuses every write without sending a request', async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw Error('Unexpected fetch'); };
  try {
    const api = createFreshEyesClient('https://unused.invalid');
    assert.equal(api.readOnly, true);
    for (const method of ['approve', 'correctApproval', 'startRun']) {
      await assert.rejects(api[method]({}), e => e.code === 'read_only_demo' && e.status === 403);
    }
    assert.equal(calls, 0);
  } finally { globalThis.fetch = original; }
});

test('reads stay relative to a static subdirectory even with an API override', async () => {
  const original = globalThis.fetch;
  const paths = [];
  globalThis.fetch = async path => { paths.push(path); return { ok: true, json: async () => ({}), text: async () => '' }; };
  try {
    const api = createFreshEyesClient('http://127.0.0.1:8765');
    await api.health(); await api.cases(); await api.runs(); await api.run('id/with space');
    await api.approvalHistory('case'); await api.patch('run'); await api.examples(); await api.example('verified-fix');
    assert.deepEqual(paths, ['data/health.json', 'data/cases.json', 'data/runs.json',
      'data/runs/id%2Fwith%20space.json', 'data/history/case.json', 'data/patches/run.diff',
      'data/examples.json', 'data/examples/verified-fix.json']);
  } finally { globalThis.fetch = original; }
});

test('missing static evidence preserves the API error code', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, status: 404 });
  try {
    const api = createFreshEyesClient();
    for (const [method, code] of [['run', 'run_not_found'], ['patch', 'patch_unavailable'], ['approvalHistory', 'approval_missing']]) {
      await assert.rejects(api[method]('absent'), e => e.code === code && e.status === 404);
    }
  } finally { globalThis.fetch = original; }
});
