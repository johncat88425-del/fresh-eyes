/** Local frontend helper. It never handles model keys or invents execution results. */
export function createFreshEyesClient(base = 'http://127.0.0.1:8765', fetchImpl = globalThis.fetch) {
  const root = base.replace(/\/$/, '');
  async function request(path, { method = 'GET', body, text = false, signal } = {}) {
    const response = await fetchImpl(`${root}${path}`, {
      method, signal,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const error = new Error(payload.error?.message || `HTTP ${response.status}`);
      error.status = response.status;
      error.code = payload.error?.code || 'http_error';
      throw error;
    }
    return text ? response.text() : response.json();
  }
  return {
    health: () => request('/api/health'),
    cases: () => request('/api/cases'),
    approve: (caseId, acceptanceHash, reviewedBy) => request(`/api/cases/${encodeURIComponent(caseId)}/approval`, {
      method: 'POST', body: { acceptance_sha256: acceptanceHash, reviewed_by: reviewedBy },
    }),
    approvalHistory: caseId => request(`/api/cases/${encodeURIComponent(caseId)}/approval/history`),
    correctApproval: (caseId, body) => request(`/api/cases/${encodeURIComponent(caseId)}/approval/correction`, { method: 'POST', body }),
    startRun: body => request('/api/runs', { method: 'POST', body }),
    runs: () => request('/api/runs'),
    run: (id, signal) => request(`/api/runs/${encodeURIComponent(id)}`, { signal }),
    patch: id => request(`/api/runs/${encodeURIComponent(id)}/patch`, { text: true }),
    examples: () => request('/api/examples'),
    example: name => request(`/api/examples/${encodeURIComponent(name)}`),
  };
}
