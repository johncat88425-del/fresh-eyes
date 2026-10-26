// Evaluation metrics, computed from individual runs. Pure functions: no DOM, no strings for display.
// Definitions follow evaluation/README.md. Anything that can't be trusted is excluded and reported as an issue,
// never silently dropped or resolved by array order.

export const GROUPS = ['A', 'B', 'C', 'D'];
export const RESULTS = ['already_passes', 'verified_fix', 'startup_only', 'needs_review', 'needs_input', 'unsupported', 'unresolved', 'infrastructure_error'];
const SETS = ['holdout', 'dev', 'boundary'];
const ORIGINS = ['evaluation', 'example'];
const SHA256 = /^[a-f0-9]{64}$/;

const isSha = v => typeof v === 'string' && SHA256.test(v);
const okNumber = v => v === null || v === undefined || (typeof v === 'number' && Number.isFinite(v) && v >= 0);

// Team review of a claimed fix: only "accepted" or "rejected" count as reviewed; null, "draft" or anything else is pending.
export const verdictOf = r => (r?.human_review?.verdict === 'accepted' || r?.human_review?.verdict === 'rejected' ? r.human_review.verdict : 'pending');

export function median(xs) {
  if (!xs.length) return null;
  const v = [...xs].sort((a, b) => a - b); const m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

// Throws on a file that isn't an evaluation file at all; returns issues for individual bad records.
export function checkFile(data) {
  if (!data || data.schema_version !== 1 || !Array.isArray(data.cases) || !Array.isArray(data.runs)) {
    throw new Error('schema_version 1 with cases[] and runs[] expected');
  }
  if (!ORIGINS.includes(data.data_origin)) throw new Error(`data_origin must be "evaluation" or "example", got ${JSON.stringify(data.data_origin)}`);
}

// Why a run can't be used, or null. Order matters only for which reason is reported first.
function runProblem(r) {
  if (!['queued', 'running', 'completed', 'failed'].includes(r.run_state)) return 'bad_state';
  if (r.run_state === 'queued' || r.run_state === 'running') return 'not_terminal';
  if (r.run_state === 'completed' && !RESULTS.includes(r.result_status)) return 'bad_state';
  if (r.run_state === 'failed' && r.result_status != null && !RESULTS.includes(r.result_status)) return 'bad_state';
  if (!okNumber(r.duration_s) || !okNumber(r.cost_usd) || !okNumber(r.model_usd)) return 'bad_number';
  if (!isSha(r.acceptance_sha256)) return 'bad_hash';
  return null;
}

export function computeEvaluation(data) {
  checkFile(data);
  const issues = [];      // { code, case_id, group }
  const inProgress = [];  // runs still queued/running
  const cases = new Map();
  const badCases = new Set();
  for (const c of data.cases) {
    if (!c || typeof c.case_id !== 'string' || cases.has(c.case_id) || !SETS.includes(c.set)) { issues.push({ code: 'bad_case', case_id: c?.case_id ?? '?' }); continue; }
    cases.set(c.case_id, c);
    if (!isSha(c.acceptance_sha256)) { badCases.add(c.case_id); issues.push({ code: 'bad_case_hash', case_id: c.case_id }); }
  }

  // Group by case+group first so duplicates are detected before anything is counted.
  const buckets = new Map();
  for (const r of data.runs) {
    const key = `${r?.case_id}\u0000${r?.group}`;
    (buckets.get(key) || buckets.set(key, []).get(key)).push(r);
  }
  const runs = new Map(); // case_id -> { A: run, ... }
  const mismatched = new Set(); // `${case_id}:${group}` with a hash mismatch, for the matrix
  for (const list of buckets.values()) {
    const r = list[0];
    const c = cases.get(r?.case_id);
    if (!c || !GROUPS.includes(r?.group)) { list.forEach(x => issues.push({ code: 'unknown_case_or_group', case_id: x?.case_id ?? '?', group: x?.group ?? '?' })); continue; }
    if (badCases.has(c.case_id)) { issues.push({ code: 'case_hash_invalid_run_skipped', case_id: c.case_id, group: r.group }); continue; }
    if (list.length > 1) { issues.push({ code: 'duplicate', case_id: c.case_id, group: r.group, count: list.length }); continue; }
    const problem = runProblem(r);
    if (problem === 'not_terminal') { inProgress.push({ case_id: c.case_id, group: r.group, run_state: r.run_state }); continue; }
    if (problem) { issues.push({ code: problem, case_id: c.case_id, group: r.group }); continue; }
    if (r.acceptance_sha256 !== c.acceptance_sha256) { issues.push({ code: 'hash_mismatch', case_id: c.case_id, group: r.group }); mismatched.add(`${c.case_id}:${r.group}`); continue; }
    (runs.get(c.case_id) || runs.set(c.case_id, {}).get(c.case_id))[r.group] = r;
  }

  const of = id => runs.get(id) || {};
  const all = [...cases.values()].filter(c => !badCases.has(c.case_id));
  const holdout = all.filter(c => c.set === 'holdout');
  const boundary = all.filter(c => c.set === 'boundary');
  const result = r => r?.result_status ?? null;

  const aNotes = [];
  const aRunning = new Set(inProgress.filter(r => r.group === 'A').map(r => r.case_id));
  const aValid = holdout.filter(c => {
    const a = of(c.case_id).A;
    if (!a || result(a) == null || result(a) === 'infrastructure_error') {
      aNotes.push({ code: 'a_excluded', case_id: c.case_id, status: aRunning.has(c.case_id) ? 'in_progress' : result(a) });
      return false;
    }
    return true;
  });
  const aPassed = aValid.filter(c => result(of(c.case_id).A) === 'already_passes');
  const aFailed = aValid.filter(c => result(of(c.case_id).A) === 'unresolved');
  aValid.filter(c => !['already_passes', 'unresolved'].includes(result(of(c.case_id).A)))
    .forEach(c => aNotes.push({ code: 'a_other', case_id: c.case_id, status: result(of(c.case_id).A) }));

  const fix = {};
  for (const g of ['B', 'C', 'D']) {
    const paired = aFailed.filter(c => of(c.case_id)[g]);
    const fixed = paired.map(c => of(c.case_id)[g]).filter(r => result(r) === 'verified_fix');
    // The engine result stays as recorded; team review is reported next to it, never folded into it.
    const count = v => fixed.filter(r => verdictOf(r) === v).length;
    fix[g] = { n: fixed.length, d: paired.length, unpaired: aFailed.length - paired.length,
      accepted: count('accepted'), rejected: count('rejected'), pending: count('pending') };
  }
  const healthyPaired = aPassed.filter(c => of(c.case_id).C);
  const healthy = { n: healthyPaired.filter(c => result(of(c.case_id).C) === 'already_passes').length, d: healthyPaired.length };

  const review = {};
  for (const g of ['B', 'C', 'D']) {
    const v = holdout.map(c => of(c.case_id)[g]).filter(r => r && result(r) === 'verified_fix');
    review[g] = { n: v.filter(r => verdictOf(r) === 'rejected').length, d: v.length, pending: v.filter(r => verdictOf(r) === 'pending').length };
  }

  // Time and cost: known values are summed; unknown ones are counted, never treated as 0.
  const sums = rs => {
    const durs = rs.map(r => r.duration_s).filter(x => x != null);
    const costs = rs.map(r => r.cost_usd).filter(x => x != null);
    // Model spend from usage (pending reconciliation) is its own subtotal: the total stays unknown while runner cost is unknown.
    const models = rs.map(r => r.model_usd).filter(x => x != null);
    return {
      runs: rs.length,
      knownTime: durs.length ? durs.reduce((a, b) => a + b, 0) : null, unknownTime: rs.length - durs.length, medianTime: median(durs),
      knownCost: costs.length ? costs.reduce((a, b) => a + b, 0) : null, unknownCost: rs.length - costs.length,
      knownModel: models.length ? models.reduce((a, b) => a + b, 0) : null, unknownModel: rs.length - models.length,
    };
  };
  const timeCost = GROUPS.map(g => ({ g, ...sums(holdout.map(c => of(c.case_id)[g]).filter(Boolean)) }));

  const both = holdout.filter(c => of(c.case_id).C && of(c.case_id).D);
  const cvd = ['C', 'D'].map(g => {
    const rs = both.map(c => of(c.case_id)[g]);
    return { g, verified: rs.filter(r => result(r) === 'verified_fix').length, ...sums(rs) };
  });

  const boundaryRows = boundary.map(c => {
    const r = of(c.case_id).C || of(c.case_id).A;
    return { case_id: c.case_id, expected: c.expected_state ?? null, actual: result(r), match: r ? result(r) === c.expected_state : null };
  });

  // Groups the export lists as not run, with its stated reason; batch stops are passed through as recorded.
  const unrun = (Array.isArray(data.unrun) ? data.unrun : [])
    .filter(u => u && typeof u.case_id === 'string' && GROUPS.includes(u.group))
    .map(u => ({ case_id: u.case_id, group: u.group, reason: typeof u.reason === 'string' ? u.reason : null }));
  const batchStops = Array.isArray(data.batch_stops) ? data.batch_stops : [];

  return { holdout, of, mismatched, issues, inProgress, aValid, aPassed, aFailed, aNotes, fix, healthy, review, timeCost, cvd, cvdCases: both.length, boundaryRows, unrun, batchStops };
}
