// Tests for web/evaluation.js. All records here are SYNTHETIC TEST DATA, not evaluation results.
// Run: node --test web/tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { computeEvaluation, verdictOf } from '../evaluation.js';
import { summarize } from '../../evaluation/summarize.mjs';

const H = ch => ch.repeat(64);
const file = (cases, runs) => ({ schema_version: 1, data_origin: 'example', cases, runs });
const kase = (id, hash, set = 'holdout', expected_state = null) => ({ case_id: id, set, acceptance_sha256: hash, expected_state });
const run = (case_id, group, result_status, extra = {}) => ({
  case_id, group, run_id: `test-${case_id}-${group}`, run_state: 'completed', result_status,
  acceptance_sha256: extra.hash ?? H('a'), duration_s: 10, cost_usd: 0, ...extra,
});
const codes = m => m.issues.map(i => i.code);

test('missing acceptance hashes are invalid, not a match (undefined === undefined)', () => {
  const m = computeEvaluation(file(
    [{ case_id: 'test-1', set: 'holdout', expected_state: null }],
    [{ case_id: 'test-1', group: 'A', run_state: 'completed', result_status: 'already_passes', duration_s: 1, cost_usd: 0 }],
  ));
  assert.deepEqual(m.aValid.length, 0);
  assert.equal(m.aPassed.length, 0);
  assert.ok(codes(m).includes('bad_case_hash'));
});

test('a run with a missing hash on a valid case is reported, not counted', () => {
  const m = computeEvaluation(file([kase('test-1', H('a'))], [run('test-1', 'A', 'already_passes', { hash: undefined, acceptance_sha256: undefined })]));
  assert.equal(m.aPassed.length, 0);
  assert.ok(codes(m).includes('bad_hash'));
});

test('a short or non-hex hash is invalid', () => {
  const m = computeEvaluation(file([kase('test-1', 'abc')], [run('test-1', 'A', 'already_passes', { hash: 'abc' })]));
  assert.equal(m.aValid.length, 0);
  assert.ok(codes(m).includes('bad_case_hash'));
});

test('duplicate case+group runs are excluded whatever their order', () => {
  for (const order of [['verified_fix', 'unresolved'], ['unresolved', 'verified_fix']]) {
    const m = computeEvaluation(file([kase('test-1', H('a'))], [
      run('test-1', 'A', 'unresolved'),
      run('test-1', 'C', order[0]), run('test-1', 'C', order[1]),
    ]));
    assert.deepEqual(m.fix.C, { n: 0, d: 0, unpaired: 1, accepted: 0, rejected: 0, pending: 0 });
    const dup = m.issues.find(i => i.code === 'duplicate');
    assert.equal(dup.group, 'C'); assert.equal(dup.count, 2);
  }
});

test('all-unknown times stay unknown; partial times are a known subtotal plus a count', () => {
  const allUnknown = computeEvaluation(file([kase('test-1', H('a'))], [
    run('test-1', 'A', 'already_passes'),
    run('test-1', 'C', 'already_passes', { duration_s: null }), run('test-1', 'D', 'already_passes', { duration_s: null }),
  ]));
  for (const r of allUnknown.cvd) { assert.equal(r.knownTime, null); assert.equal(r.unknownTime, 1); }
  assert.equal(allUnknown.timeCost.find(r => r.g === 'C').knownTime, null);

  const partial = computeEvaluation(file([kase('test-1', H('a')), kase('test-2', H('b'))], [
    run('test-1', 'C', 'already_passes', { duration_s: 30 }),
    run('test-2', 'C', 'already_passes', { hash: H('b'), duration_s: null }),
  ]));
  const c = partial.timeCost.find(r => r.g === 'C');
  assert.equal(c.knownTime, 30); assert.equal(c.unknownTime, 1); assert.equal(c.runs, 2);
});

test('queued or running runs are listed as in progress, never counted', () => {
  const m = computeEvaluation(file([kase('test-1', H('a'))], [run('test-1', 'A', 'already_passes', { run_state: 'running' })]));
  assert.equal(m.aValid.length, 0);
  assert.deepEqual(m.inProgress, [{ case_id: 'test-1', group: 'A', run_state: 'running' }]);
  assert.equal(m.issues.length, 0);
  assert.deepEqual(m.aNotes, [{ code: 'a_excluded', case_id: 'test-1', status: 'in_progress' }]);
});

test('completed without a result, unknown run_state, and negative or non-finite numbers are invalid', () => {
  const m = computeEvaluation(file([kase('t1', H('a')), kase('t2', H('a')), kase('t3', H('a')), kase('t4', H('a'))], [
    run('t1', 'A', null),
    run('t2', 'A', 'already_passes', { run_state: 'done' }),
    run('t3', 'A', 'already_passes', { duration_s: -5 }),
    run('t4', 'A', 'already_passes', { cost_usd: Infinity }),
  ]));
  assert.equal(m.aValid.length, 0);
  assert.deepEqual(m.issues.map(i => [i.case_id, i.code]).sort(), [['t1', 'bad_state'], ['t2', 'bad_state'], ['t3', 'bad_number'], ['t4', 'bad_number']]);
});

test('a failed run stays in the fix denominator as unverified', () => {
  const m = computeEvaluation(file([kase('test-1', H('a'))], [
    run('test-1', 'A', 'unresolved'),
    run('test-1', 'C', 'infrastructure_error', { run_state: 'failed' }),
  ]));
  assert.deepEqual(m.fix.C, { n: 0, d: 1, unpaired: 0, accepted: 0, rejected: 0, pending: 0 });
});

test('a file without a valid data_origin is rejected', () => {
  assert.throws(() => computeEvaluation({ schema_version: 1, cases: [], runs: [] }), /data_origin/);
});

test('the labelled example file still gives the hand-computed numbers', () => {
  const data = JSON.parse(readFileSync(new URL('../examples/evaluation-example.json', import.meta.url)));
  const m = computeEvaluation(data);
  assert.deepEqual([m.aPassed.length, m.aValid.length], [1, 3]);
  assert.deepEqual(m.fix, {
    B: { n: 1, d: 2, unpaired: 0, accepted: 0, rejected: 1, pending: 0 },
    C: { n: 2, d: 2, unpaired: 0, accepted: 2, rejected: 0, pending: 0 },
    D: { n: 1, d: 1, unpaired: 1, accepted: 0, rejected: 0, pending: 1 },
  });
  assert.deepEqual(m.healthy, { n: 1, d: 1 });
  assert.deepEqual(m.review.B, { n: 1, d: 1, pending: 0 });
  assert.deepEqual(m.review.D, { n: 0, d: 1, pending: 1 });
  assert.equal(m.boundaryRows.filter(r => r.match).length, 2);
  assert.ok(m.mismatched.has('example-4:C'));
  const a = m.timeCost.find(r => r.g === 'A');
  assert.equal(a.knownTime, 504); assert.equal(a.medianTime, 69);
});

test('model spend is a separate subtotal; unknown model spend is counted, not 0', () => {
  const m = computeEvaluation(file([kase('test-1', H('a')), kase('test-2', H('b'))], [
    run('test-1', 'C', 'unresolved', { cost_usd: null, model_usd: 0.0136 }),
    run('test-2', 'C', 'unresolved', { hash: H('b'), cost_usd: null, model_usd: null }),
  ]));
  const c = m.timeCost.find(r => r.g === 'C');
  assert.equal(c.knownCost, null);
  assert.equal(c.unknownCost, 2);
  assert.equal(c.knownModel, 0.0136);
  assert.equal(c.unknownModel, 1);
});

test('a negative or non-numeric model_usd makes the run unusable', () => {
  const m = computeEvaluation(file([kase('test-1', H('a'))], [run('test-1', 'B', 'unresolved', { model_usd: -1 })]));
  assert.ok(codes(m).includes('bad_number'));
});

test('unrun groups and batch stops from the export are passed through, malformed rows dropped', () => {
  const data = file([kase('test-1', H('a'))], [run('test-1', 'A', 'unresolved')]);
  data.unrun = [{ case_id: 'test-1', group: 'B', reason: 'not authorised' }, { case_id: 'test-1', group: 'Z' }, null];
  data.batch_stops = [{ reason: 'unknown charge' }];
  const m = computeEvaluation(data);
  assert.deepEqual(m.unrun, [{ case_id: 'test-1', group: 'B', reason: 'not authorised' }]);
  assert.equal(m.batchStops.length, 1);
});

test("A's real export format (from its own export code, SYNTHETIC fixture run) reads without issues", () => {
  const path = new URL('./fixtures/export-format.json', import.meta.url);
  const m = computeEvaluation(JSON.parse(readFileSync(path, 'utf8')));
  assert.deepEqual(m.issues, []);
  assert.deepEqual(m.unrun.map(u => u.group), ['B', 'C', 'D']);
  assert.equal(m.timeCost.find(r => r.g === 'A').knownModel, 0);
});

test('summary labels example data on every section and lists unrun groups for real exports', () => {
  const example = JSON.parse(readFileSync(new URL('../examples/evaluation-example.json', import.meta.url), 'utf8'));
  const ex = summarize(example);
  assert.equal(ex.match(/EXAMPLE DATA, NOT A RESULT/g).length, 2);
  const real = summarize(JSON.parse(readFileSync(new URL('./fixtures/export-format.json', import.meta.url), 'utf8')));
  assert.doesNotMatch(real, /EXAMPLE/);
  assert.match(real, /\*\*Not run:\*\*/);
  assert.doesNotMatch(real, /unknown \(\+\d+ unknown\)/);
});

// B19: team review of claimed fixes, in the exact shape A's result_reviews.apply_reviews writes (SYNTHETIC fixture).
const reviewsFixture = () => JSON.parse(readFileSync(new URL('./fixtures/export-reviews.json', import.meta.url), 'utf8'));

test('review counts sit next to the fix rate; a rejected fix keeps its engine result', () => {
  const data = reviewsFixture();
  const before = JSON.stringify(data);
  const m = computeEvaluation(data);
  assert.deepEqual(m.fix.C, { n: 3, d: 3, unpaired: 0, accepted: 1, rejected: 1, pending: 1 });
  assert.deepEqual(m.review.C, { n: 1, d: 3, pending: 1 });
  assert.equal(m.of('synthetic-review-2').C.result_status, 'verified_fix');
  assert.equal(JSON.stringify(data), before, 'computeEvaluation must not modify its input');
});

test('only accepted or rejected count as reviewed; draft, null or anything else is pending', () => {
  for (const human_review of [null, undefined, { verdict: 'draft' }, { verdict: 'approved' }, { verdict: '' }]) {
    assert.equal(verdictOf({ human_review }), 'pending');
  }
  assert.equal(verdictOf({ human_review: { verdict: 'accepted' } }), 'accepted');
  assert.equal(verdictOf({ human_review: { verdict: 'rejected' } }), 'rejected');
});

test('summary states review outcomes and never attributes a pending review', () => {
  const text = summarize(reviewsFixture());
  assert.match(text, /3 \/ 3 \(team review: 1 accepted, 1 rejected, 1 pending\)/);
  const rows = text.split('\n').filter(l => l.startsWith('| `synthetic-review-'));
  assert.match(rows[1], /\| rejected \| SYNTHETIC REVIEWER \|/);
  assert.match(rows[2], /☐ accepted ☐ rejected \|  \|  \|  \|$/);
});
