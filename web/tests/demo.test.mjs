// Consistency checks for the built read-only demo (dist/demo, made by web/demo/build.py).
// Skipped when no demo has been built. Run: node --test web/tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

const DEMO = new URL('../../dist/demo/', import.meta.url);
const built = existsSync(new URL('data/health.json', DEMO));
const read = rel => readFileSync(new URL(rel, DEMO));
const json = rel => JSON.parse(read(rel).toString('utf8'));
const opts = { skip: built ? false : 'no demo built (python3.12 web/demo/build.py)' };

test('demo is read-only and uses the static client', opts, () => {
  const health = json('data/health.json');
  assert.equal(health.read_only, true);
  assert.equal(health.execution_enabled, false);
  assert.equal(health.paid_calls_enabled, false);
  assert.equal(health.approval_corrections, false);
  const client = read('client.js').toString('utf8');
  assert.match(client, /read_only_demo/);
  assert.doesNotMatch(client, /127\.0\.0\.1|localhost/);
});

test('every listed run has its record, labelled as a recorded replay', opts, () => {
  const { items } = json('data/runs.json');
  assert.ok(items.length > 0);
  for (const summary of items) {
    const run = json(`data/runs/${summary.run_id}.json`);
    assert.equal(run.run_id, summary.run_id);
    assert.equal(run.data_origin, 'recorded_replay');
    assert.equal(summary.data_origin, 'recorded_replay');
    if (run.patch_available) assert.ok(existsSync(new URL(`data/patches/${run.run_id}.diff`, DEMO)), `patch for ${run.run_id}`);
  }
});

test('start-here links point at runs that exist', opts, () => {
  const { demo_highlights: links } = json('data/health.json');
  const ids = new Set(json('data/runs.json').items.map(r => r.run_id));
  assert.ok(links.length > 0);
  for (const link of links) assert.ok(ids.has(link.run_id), `highlight ${link.key} → ${link.run_id}`);
});

test('a case shows as approved only with a matching history record', opts, () => {
  for (const c of json('data/cases.json').items) {
    if (c.review?.status !== 'approved') continue;
    assert.equal(c.review.acceptance_sha256, c.acceptance_sha256, c.case_id);
    const history = json(`data/history/${c.case_id}.json`);
    assert.equal(history.acceptance_sha256, c.acceptance_sha256, c.case_id);
    assert.equal(history.items.at(-1).review.reviewed_by, c.review.reviewed_by, c.case_id);
  }
});

test('manifest hashes match the files', opts, () => {
  const { files } = json('data/manifest.json');
  for (const [rel, sha] of Object.entries(files)) {
    assert.equal(createHash('sha256').update(read(rel)).digest('hex'), sha, rel);
  }
});

test('labelled examples are examples', opts, () => {
  for (const [name, record] of Object.entries(json('data/examples.json').items)) assert.equal(record.data_origin, 'example', name);
});
