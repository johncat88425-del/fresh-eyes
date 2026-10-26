/** Read-only demo client: same interface as api/client.js, but every read comes from a static JSON file
 * written by web/demo/build.py, and every write is refused. It never runs, approves or calls a model. */
export function createFreshEyesClient() {
  async function get(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      error.code = 'http_error';
      throw error;
    }
    return response;
  }
  const json = path => get(path).then(r => r.json());
  // Static files can only answer 404 for a missing record; give the page the code the API would have used.
  const as = code => error => { if (error.status === 404) error.code = code; throw error; };
  const refuse = async () => {
    const error = new Error('This read-only demo never runs, approves or changes anything.');
    error.status = 403;
    error.code = 'read_only_demo';
    throw error;
  };
  const id = value => encodeURIComponent(value);
  return {
    readOnly: true,
    health: () => json('data/health.json'),
    cases: () => json('data/cases.json'),
    approve: refuse,
    approvalHistory: caseId => json(`data/history/${id(caseId)}.json`).catch(as('approval_missing')),
    correctApproval: refuse,
    startRun: refuse,
    runs: () => json('data/runs.json'),
    run: runId => json(`data/runs/${id(runId)}.json`).catch(as('run_not_found')),
    patch: runId => get(`data/patches/${id(runId)}.diff`).then(r => r.text()).catch(as('patch_unavailable')),
    examples: () => json('data/examples.json'),
    example: name => json(`data/examples/${id(name)}.json`),
  };
}
