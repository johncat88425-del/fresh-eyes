import { test } from 'node:test';
import assert from 'node:assert/strict';
import { repairPathFacts, refusalKey } from '../repair_path.js';
import { STR } from '../i18n.js';

test('human rejection removes the AI success summary while preserving the engine result', () => {
  const run = { result_status: 'verified_fix', repair_mode: 'model', result_review: { verdict: 'rejected', reviewer: 'synthetic-reviewer' } };
  const facts = repairPathFacts(run);
  assert.equal(facts.humanRejected, true);
  assert.equal(facts.verifiedByAi, false);
  assert.equal(run.result_status, 'verified_fix');
});

test('second-round success summarises the latest clean attempts and actual returned model', () => {
  const attempts = [{ phase: 'original_readme', status: 'failed' },
    { phase: 'candidate_patch', round: 1, status: 'failed' },
    { phase: 'candidate_patch', round: 2, status: 'passed' },
    { phase: 'fresh_verification', round: 2, status: 'passed' }];
  const facts = repairPathFacts({ evidence: { attempts }, costs: { model_calls: [
    { stage: 'classify', returned_model: 'first-model' },
    { stage: 'classify_r2', returned_model: 'nvidia/Nemotron-3-Ultra-550b-a55b' }] } });
  assert.equal(facts.attempt('original_readme').status, 'failed');
  assert.equal(facts.attempt('candidate_patch').round, 2);
  assert.equal(facts.attempt('fresh_verification').status, 'passed');
  assert.equal(facts.modelOf('classify'), 'Nemotron 3 Ultra');
});

test('raw engine numeric call counts do not crash the model summary', () => {
  const facts = repairPathFacts({ evidence: { model_calls: 4, attempts: null } });
  assert.equal(facts.modelOf('diagnose', 'recorded-model'), 'recorded-model');
  assert.equal(facts.attempt('candidate_patch'), undefined);
  assert.equal(facts.verifiedByAi, false);
});

test('manual and unresolved repairs never receive an AI success badge', () => {
  for (const run of [{ result_status: 'verified_fix', repair_mode: 'manual_candidate' },
    { result_status: 'unresolved', repair_mode: 'model' }]) assert.equal(repairPathFacts(run).verifiedByAi, false);
});

test('a stopped second round does not borrow an earlier round clean verification', () => {
  const facts = repairPathFacts({ evidence: { repair_rounds: [{ round: 1 }, { round: 2, status: 'no_candidate' }],
    attempts: [{ phase: 'candidate_patch', round: 1, status: 'passed' },
      { phase: 'fresh_verification', round: 1, status: 'failed' }] } });
  assert.equal(facts.attempt('candidate_patch'), undefined);
  assert.equal(facts.attempt('fresh_verification'), undefined);
});

test('patch-checker refusals from real runs get plain words, unknown text stays as recorded', () => {
  const cases = [
    // 0c0141f2, round 1
    ["Patched setup command is absent from delivered README: pip install -e '.[dev]'", 'rej_setup_absent', { cmd: "pip install -e '.[dev]'" }],
    // e9a04f64, round 1
    ['Replacement in README.md matched 0 times; expected exactly once', 'rej_match', { file: 'README.md', n: '0' }],
    // e5150fec run reason
    ['Candidate patch rejected: Each replacement must match exactly once', 'rej_match_any', {}],
    ["Lines 38-42 of README.md don't match the quoted text; they read: 'x'", 'rej_lines', { a: '38', b: '42', file: 'README.md' }],
    ['Patch is outside prototype scope: setup.py', 'rej_scope', { file: 'setup.py' }],
    ['Only project dependency fields may change', 'rej_pyproject', {}],
    ['Requirement URLs, local paths and pip options are unsupported', 'rej_requirements', {}],
  ];
  for (const [reason, key, vars] of cases) {
    assert.deepEqual(refusalKey(reason), { key, vars }, reason);
    for (const lang of ['zh', 'en']) assert.ok(STR[lang][key], `${lang}.${key}`);
  }
  assert.equal(refusalKey('Classification is a hint requiring human review; no patch requested.'), null);
  assert.equal(refusalKey(''), null);
});
