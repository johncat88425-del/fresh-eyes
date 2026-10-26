// A stored choice belongs to the exact subject and named reviewer at the time of the click.
export function reviewIssue(c, decision, name, guidance = {}) {
  if (!decision?.decision) return 'not_decided';
  if (decision.acceptance_sha256 !== c.acceptance_sha256) return 'acceptance_changed';
  if (!c.review_subject_sha256 || decision.review_subject_sha256 !== c.review_subject_sha256) return 'subject_changed';
  if (!name.trim() || decision.reviewed_by !== name.trim()) return 'reviewer_changed';
  if (!['accept', 'reject'].includes(decision.decision)) return 'not_decided';
  if (!/([+-]\d{2}:\d{2}|Z)$/.test(decision.decided_at || '') || !Number.isFinite(Date.parse(decision.decided_at))) return 'time_missing';
  if (decision.decision === 'reject' && !String(decision.note || '').trim()) return 'reason_required';
  if (decision.decision === 'accept' && guidance.extra_choice && decision.extra !== guidance.extra_choice.options[0]) return 'grouping_pending';
  if (c.hash_matches_review_file === false || c.excerpts?.some(e => e.lines.some(line => line[1] == null))) return 'source_invalid';
  return null;
}

export function bindDecision(c, previous, patch, name, time) {
  const next = { ...previous, ...patch };
  if (patch.decision) Object.assign(next, { acceptance_sha256:c.acceptance_sha256,
    review_subject_sha256:c.review_subject_sha256, reviewed_by:name.trim(), decided_at:time });
  return next;
}

export function exportReview(cases, guidance, state) {
  const approvals = [], notAccepted = [];
  for (const c of cases) {
    const d = state.decisions[c.case_id];
    if (reviewIssue(c, d, state.name, guidance[c.case_id])) continue;
    const common = {case_id:c.case_id, acceptance_sha256:d.acceptance_sha256,
      review_subject_sha256:d.review_subject_sha256};
    if (d.decision === 'accept') approvals.push({...common, status:'approved', reviewed_by:d.reviewed_by,
      approved_at:d.decided_at, note:d.note || undefined, choice:d.extra || undefined});
    else notAccepted.push({...common, reason:d.note, decided_at:d.decided_at, choice:d.extra || undefined});
  }
  return {reviewed_by:state.name.trim(), account_id:state.uid, approvals, not_accepted:notAccepted};
}
