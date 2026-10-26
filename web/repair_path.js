// Select evidence for the compact repair summary without changing the stored engine result.
import { verdictOf } from './evaluation.js';

export function repairPathFacts(run) {
  const ev = run.evidence || {};
  const attempts = Array.isArray(ev.attempts) ? ev.attempts : [];
  const calls = Array.isArray(run.costs?.model_calls) ? run.costs.model_calls
    : Array.isArray(ev.model_calls) ? ev.model_calls : [];
  const humanRejected = verdictOf({ human_review: run.result_review }) === 'rejected';
  const rounds = [...attempts, ...(Array.isArray(ev.repair_rounds) ? ev.repair_rounds : [])]
    .map(a => a.round).filter(n => Number.isInteger(n) && n > 0);
  const latestRound = rounds.length ? Math.max(...rounds) : null;
  return {
    humanRejected,
    verifiedByAi: run.result_status === 'verified_fix' && run.repair_mode === 'model' && !humanRejected,
    attempt: phase => phase === 'original_readme' ? attempts.find(a => a.phase === phase)
      : attempts.findLast(a => a.phase === phase && (latestRound == null || a.round === latestRound)),
    modelOf(stage, fallback) {
      const id = calls.findLast(c => c.stage === stage || c.stage === `${stage}_r2`)?.returned_model || fallback || '';
      const match = /Nemotron-3-(Nano|Super|Ultra)/i.exec(id);
      return match ? `Nemotron 3 ${match[1][0].toUpperCase()}${match[1].slice(1).toLowerCase()}` : id || null;
    },
  };
}

// Plain words for the patch checker's refusals (src/fresh_eyes/patches.py). Returns {key, vars} for an i18n
// "rej_*" string, or null for a message we don't recognise (shown as it is). A run's reason may carry the
// "Candidate patch rejected: " prefix.
const REFUSALS = [
  [/^Patched setup command is absent from delivered README: (.+)$/s, m => ({ key: 'rej_setup_absent', vars: { cmd: m[1] } })],
  [/^Replacement in (.+) matched (\d+) times; expected exactly once$/, m => ({ key: 'rej_match', vars: { file: m[1], n: m[2] } })],
  [/^Each replacement must match exactly once$/, () => ({ key: 'rej_match_any', vars: {} })],
  [/^Lines (\d+)-(\d+) of (.+?) don't match the quoted text/s, m => ({ key: 'rej_lines', vars: { a: m[1], b: m[2], file: m[3] } })],
  [/^Patch is outside prototype scope: (.+)$/, m => ({ key: 'rej_scope', vars: { file: m[1] } })],
  [/^Only project dependency fields may change$/, () => ({ key: 'rej_pyproject', vars: {} })],
  [/^Requirement URLs, local paths and pip options are unsupported$/, () => ({ key: 'rej_requirements', vars: {} })],
];
export function refusalKey(reason) {
  const text = String(reason || '').replace(/^Candidate patch rejected: /, '');
  for (const [pattern, make] of REFUSALS) {
    const m = pattern.exec(text);
    if (m) return make(m);
  }
  return null;
}
