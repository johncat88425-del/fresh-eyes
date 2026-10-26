// Turn an evaluation results file into Markdown: a results section for the Devpost draft, and a
// human-review checklist for every claimed verified_fix (EVAL-11). Uses the same metric code as the
// evaluation page (web/evaluation.js), so the numbers match what the page shows.
//
//   node evaluation/summarize.mjs evaluation/results.json            > evaluation/summary.md
//   node evaluation/summarize.mjs web/examples/evaluation-example.json --allow-example
//
// Example data is refused unless --allow-example, and is then labelled on every section.
import { readFileSync } from 'node:fs';
import { computeEvaluation, GROUPS, verdictOf } from '../web/evaluation.js';

const GROUP_NAMES = { A: 'A · original README', B: 'B · one fix attempt (Nano + Ultra)', C: 'C · Fresh Eyes, up to two rounds (Nano + Ultra)', D: 'D · same as C, every call on Ultra' };
const frac = (n, d) => `${n} / ${d}`;
const money = v => (v == null ? 'unknown' : `$${v.toFixed(4)}`);
const secs = v => (v == null ? 'unknown' : v < 90 ? `${Math.round(v)} s` : `${(v / 60).toFixed(1)} min`);
const withUnknown = (text, n) => (n && text !== 'unknown' ? `${text} (+${n} unknown)` : text);
const REASONS = {
  hash_mismatch: 'acceptance hash differs from the case', duplicate: 'more than one run for the same case and group',
  bad_state: "progress and result don't fit together", bad_number: 'time or cost is not a finite, non-negative number',
  bad_hash: 'acceptance hash missing or malformed', not_terminal: 'still running', bad_case: 'malformed case entry',
  bad_case_hash: 'case acceptance hash missing or malformed', a_excluded: 'original run left out of the original-pass count',
  a_other: 'original run counted neither as a pass nor as a README failure',
};

export function summarize(data) {
  const m = computeEvaluation(data);
  const example = data.data_origin === 'example';
  const tag = example ? ' — EXAMPLE DATA, NOT A RESULT' : '';
  const out = [];
  out.push(`## Evaluation${tag}`, '');
  if (example) out.push('> Invented example data for checking this tool. Never paste it into the submission.', '');
  out.push(`Frozen ${data.frozen_at ?? 'unknown'}; freeze \`${(data.freeze_sha256 ?? '').slice(0, 12) || 'unknown'}…\`; runner \`${data.freeze?.runner ?? 'unknown'}\`; dependencies \`${data.freeze?.dependency_mode ?? 'unknown'}\`. Holdout cases: ${m.holdout.length}.`, '');
  out.push('| Measure | Result |', '|---|---|');
  out.push(`| Original README passes (A) | ${frac(m.aPassed.length, m.aValid.length)} |`);
  for (const g of ['B', 'C', 'D']) {
    const f = m.fix[g];
    const notes = [f.unpaired ? `${f.unpaired} not run` : '', f.n ? `team review: ${f.accepted} accepted, ${f.rejected} rejected, ${f.pending} pending` : ''].filter(Boolean);
    out.push(`| Failed cases fixed and verified by the clean re-runs (${g}) | ${frac(f.n, f.d)}${notes.length ? ` (${notes.join('; ')})` : ''} |`);
  }
  out.push(`| Healthy cases left alone (C) | ${frac(m.healthy.n, m.healthy.d)} |`);
  for (const g of ['B', 'C', 'D']) out.push(`| Claimed fixes rejected in team review (${g}) | ${frac(m.review[g].n, m.review[g].d)}${m.review[g].pending ? ` (${m.review[g].pending} not reviewed yet)` : ''} |`);
  if (m.boundaryRows.length) out.push(`| Boundary cases that stopped as expected | ${frac(m.boundaryRows.filter(r => r.match).length, m.boundaryRows.length)} |`);
  out.push('');
  out.push('| Group | Runs | Total time | Median time | Model cost (from usage, pending reconciliation) | Total cost |', '|---|---|---|---|---|---|');
  for (const r of m.timeCost) {
    out.push(`| ${GROUP_NAMES[r.g]} | ${r.runs} | ${withUnknown(secs(r.knownTime), r.unknownTime)} | ${secs(r.medianTime)} | ${withUnknown(money(r.knownModel), r.unknownModel)} | ${withUnknown(money(r.knownCost), r.unknownCost)} |`);
  }
  out.push('');
  const unrunByGroup = GROUPS.map(g => [g, m.unrun.filter(u => u.group === g)]).filter(([, rows]) => rows.length);
  if (unrunByGroup.length) {
    out.push('**Not run:**');
    for (const [g, rows] of unrunByGroup) out.push(`- ${GROUP_NAMES[g]}: ${rows.length} case(s). ${[...new Set(rows.map(u => u.reason || 'no reason given'))].join(' / ')}`);
    out.push('');
  }
  if (m.batchStops.length) out.push(`**The batch stopped early:** ${m.batchStops.map(b => (typeof b === 'string' ? b : JSON.stringify(b))).join('; ')}`, '');
  const excluded = [...m.issues, ...m.aNotes];
  if (excluded.length) {
    out.push('**Left out of the counts, with the reason:**');
    for (const i of excluded) out.push(`- \`${i.case_id}\`${i.group ? ` group ${i.group}` : ''}: ${REASONS[i.code] ?? i.code}${i.status ? ` (ended as ${i.status})` : ''}`);
    out.push('');
  }
  out.push('Limits: a small, purposively chosen sample, reviewed by the team itself. These results describe these cases only.', '');

  // Review checklist: every verified_fix needs a person to look at the patch and the outputs.
  const claimed = m.holdout.flatMap(c => GROUPS.map(g => m.of(c.case_id)[g]).filter(r => r && r.result_status === 'verified_fix'));
  out.push(`## Team review of claimed fixes${tag}`, '');
  if (!claimed.length) out.push('No run claims `verified_fix`, so there is nothing to review.', '');
  else {
    out.push('For each run: open the report, read the patch, compare both outputs with the expected outputs, and confirm only README/dependency lines changed. Then mark it.', '');
    out.push('| Case | Group | Run | Accepted / rejected | Reviewer | Reviewed at | Note |', '|---|---|---|---|---|---|---|');
    for (const r of claimed) {
      const v = verdictOf(r); const hr = r.human_review || {};
      out.push(`| \`${r.case_id}\` | ${r.group} | \`${r.run_id}\` | ${v === 'pending' ? '☐ accepted ☐ rejected' : v} | ${v === 'pending' ? '' : hr.reviewer ?? ''} | ${v === 'pending' ? '' : hr.reviewed_at ?? ''} | ${(v === 'pending' ? '' : hr.note ?? '').replace(/\|/g, '\\|')} |`);
    }
    out.push('');
  }
  return out.join('\n');
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const [file, flag] = process.argv.slice(2);
  if (!file) { console.error('usage: node evaluation/summarize.mjs RESULTS.json [--allow-example]'); process.exit(2); }
  const data = JSON.parse(readFileSync(file, 'utf8'));
  if (data.data_origin === 'example' && flag !== '--allow-example') { console.error('This is example data; pass --allow-example to summarise it for testing.'); process.exit(1); }
  process.stdout.write(summarize(data));
}
