// Public workflow metadata only: this module cannot dispatch a run or read a key.
export const WORKFLOW = 'https://github.com/johncat88425-del/fresh-eyes/actions/workflows/nightly-readme.yml';
export const HISTORY_API = 'https://api.github.com/repos/johncat88425-del/fresh-eyes/actions/workflows/nightly-readme.yml/runs?per_page=20';
const conclusions = new Set(['success', 'failure', 'cancelled', 'timed_out', 'action_required', 'neutral', 'skipped', 'stale', 'startup_failure']);
export function normalizeHistory(body) {
  if (!Array.isArray(body?.workflow_runs)) throw new Error('invalid_history');
  const rows = body.workflow_runs.slice(0, 20).flatMap(r => {
    if (!Number.isSafeInteger(r.id) || r.id <= 0 || !Number.isFinite(Date.parse(r.created_at)) ||
        r.path !== '.github/workflows/nightly-readme.yml') return [];
    return [{id: r.id, url: `https://github.com/johncat88425-del/fresh-eyes/actions/runs/${r.id}`,
      trigger: r.event === 'schedule' ? 'scheduled' : r.event === 'workflow_dispatch' ? 'manual' : 'other',
      createdAt: r.created_at,
      status: r.status === 'completed' ? 'completed' : r.status === 'in_progress' ? 'running' : 'queued',
      conclusion: conclusions.has(r.conclusion) ? r.conclusion : null,
      toolCommit: /^[a-f0-9]{40}$/.test(r.head_sha) ? r.head_sha : null}];
  });
  return rows.sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
}
export async function fetchHistory({fetcher = fetch} = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetcher(HISTORY_API, {credentials: 'omit', referrerPolicy: 'no-referrer',
      headers: {Accept: 'application/vnd.github+json'}, signal: controller.signal});
    if (!response.ok) throw new Error(response.status === 404 ? 'not_ready' :
      [403, 429].includes(response.status) ? 'rate_limited' : 'history_unavailable');
    return normalizeHistory(await response.json());
  } finally { clearTimeout(timer); }
}
