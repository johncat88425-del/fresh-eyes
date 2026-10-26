import {test} from 'node:test';
import assert from 'node:assert/strict';
import {bindDecision, reviewIssue, exportReview} from '../../cases/review_page/review_state.js';

const subject = {case_id:'synthetic-case', acceptance_sha256:'a'.repeat(64), review_subject_sha256:'b'.repeat(64)};
const accepted = bindDecision(subject, {}, {decision:'accept'}, 'synthetic-reviewer', '2026-10-24T12:00:00-04:00');
const output = (c=subject, d=accepted, name='synthetic-reviewer') => exportReview([c], {}, {name, uid:null, decisions:{[c.case_id]:d}});

test('changed subject is never relabelled with the current hash', () => {
  assert.equal(output({...subject, acceptance_sha256:'c'.repeat(64)}).approvals.length, 0);
  assert.equal(output({...subject, review_subject_sha256:'d'.repeat(64)}).approvals.length, 0);
});
test('editing a note does not reapprove an old decision or change its original subject', () => {
  const current = {...subject, acceptance_sha256:'c'.repeat(64)};
  const edited = bindDecision(current, accepted, {note:'synthetic note'}, 'synthetic-reviewer', '2026-10-24T13:00:00-04:00');
  assert.equal(edited.acceptance_sha256, accepted.acceptance_sha256);
  assert.equal(output(current, edited).approvals.length, 0);
});
test('renaming the reviewer requires an explicit new decision', () => {
  assert.equal(output(subject, accepted, 'synthetic-other').approvals.length, 0);
  const changed = bindDecision(subject, accepted, {decision:'accept'}, 'synthetic-other', '2026-10-24T13:00:00-04:00');
  assert.equal(output(subject, changed, 'synthetic-other').approvals[0].reviewed_by, 'synthetic-other');
  assert.equal(changed.decided_at, '2026-10-24T13:00:00-04:00');
});
test('a rejection without a reason and an undecided grouping cannot be exported', () => {
  const rejected = bindDecision(subject, {}, {decision:'reject'}, 'synthetic-reviewer', accepted.decided_at);
  assert.equal(output(subject, rejected).not_accepted.length, 0);
  assert.equal(reviewIssue(subject, accepted, 'synthetic-reviewer', {extra_choice:{options:['keep','discuss']}}), 'grouping_pending');
});
test('valid records preserve the captured time, hash and rejection reason', () => {
  assert.equal(output().approvals[0].approved_at, accepted.decided_at);
  const rejected = {...accepted, decision:'reject', note:'synthetic reason'};
  assert.equal(output(subject, rejected).not_accepted[0].reason, 'synthetic reason');
  assert.equal(output(subject, rejected).approvals.length, 0);
});
