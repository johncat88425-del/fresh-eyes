// Fresh Eyes frontend. Pages: the beginner front door (home, start, answer, examples), dashboard, case, runs,
// run report, evaluation, state preview.
// Every value shown comes from the API (or a labelled example). Untrusted text (README, logs, output,
// diffs) is only ever inserted as text nodes, never as HTML.
import { createFreshEyesClient } from './client.js';
import { computeEvaluation, GROUPS, verdictOf } from './evaluation.js';
import { STR, LABELS } from './i18n.js';
import { repairPathFacts, refusalKey } from './repair_path.js';
import { readRepoReadme } from './github_readme.js';

const params = new URLSearchParams(location.search);
const API_BASE = (params.get('api') || 'http://127.0.0.1:8765').replace(/\/$/, '');
const api = createFreshEyesClient(API_BASE);
const POLL_MS = 1500;

let lang = 'zh';
try { lang = localStorage.getItem('fe-lang') === 'en' ? 'en' : 'zh'; } catch { /* storage unavailable */ }
// ?lang=en or ?lang=zh in a shared link wins over the stored choice (judges get English links).
if (['en', 'zh'].includes(params.get('lang'))) lang = params.get('lang');
const t = (key, vars = {}) => Object.entries(vars).reduce((s, [k, v]) => s.replaceAll(`{${k}}`, v), STR[lang][key] ?? key);
const lbl = (group, code) => LABELS[lang][group]?.[code ?? 'none'] ?? code ?? '—';

/* ------------------------------------------------------------------ helpers */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked') el.checked = Boolean(v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const mount = (id, ...kids) => { const el = document.getElementById(id); el.replaceChildren(...kids.flat(Infinity).filter(Boolean)); return el; };
const badge = (cls, text, code, extra = '', title = null) => h('span', { class: `badge s-${cls} ${extra}`.trim(), title }, text, code ? h('code', {}, code) : null);
const kv = pairs => h('dl', { class: 'kv' }, pairs.filter(Boolean).flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
const short = sha => (sha ? `${sha.slice(0, 12)}…` : '—');
const money = v => (v == null ? t('unknown') : `$${Number(v).toFixed(4)}`);
const repoName = url => (url || '').replace(/^https:\/\/github\.com\//, '');
const card = (...kids) => h('section', { class: 'card' }, ...kids);
function when(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso; // date only: nothing to convert
  // en-GB in both languages: it names the zone BST/GMT, where zh-TW prints "GMT+1".
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/London', year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZoneName: 'short',
  }).format(d).replace(/GMT\+0$/, 'GMT'); // some engines print UK winter time as "GMT+0"
}
const fmtDur = sec => {
  if (sec == null) return t('unknown');
  const whole = Math.round(sec); const m = Math.floor(whole / 60); const r = whole % 60;
  return lang === 'zh' ? (m ? `${m} 分 ${r} 秒` : `${r} 秒`) : (m ? `${m}m ${r}s` : `${r}s`);
};
const runSeconds = r => {
  const a = Date.parse(r?.started_at); const b = Date.parse(r?.finished_at);
  return Number.isFinite(a) && Number.isFinite(b) && b >= a ? (b - a) / 1000 : null;
};
function errorBox(e) {
  const code = e?.code || (e instanceof TypeError ? 'network' : 'http_error');
  return h('div', { class: 'notice bad', role: 'alert' },
    h('strong', {}, lbl('errors', code) === code ? t('error_title') : lbl('errors', code)),
    h('p', {}, h('span', { class: 'mono' }, `${e?.status ? `HTTP ${e.status} · ` : ''}${code}`), ' — ', e?.message || ''));
}
function diffView(text) {
  return h('pre', {}, String(text).split('\n').map((line, i, all) => h('span', {
    class: line.startsWith('+') && !line.startsWith('+++') ? 'add' : line.startsWith('-') && !line.startsWith('---') ? 'del' : null,
  }, line + (i < all.length - 1 ? '\n' : ''))));
}
// <details> whose open state survives re-renders (polling, language switch).
function foldable(key, summary, body, defaultOpen = false) {
  const open = state.open.has(key) || (defaultOpen && !state.closed.has(key));
  return h('details', { open, ontoggle: e => {
    if (e.target.open) { state.open.add(key); state.closed.delete(key); } else { state.open.delete(key); state.closed.add(key); }
  } }, h('summary', {}, summary), body);
}
const stream = (key, label, text, truncated) => (text ? foldable(key, `${label}${truncated ? ` ${t('truncated')}` : ''}`, h('pre', {}, text)) : null);

/* ------------------------------------------------------------------ state and routing */
const state = {
  health: null, healthError: null,
  cases: null, casesError: null,
  runs: null, runsError: null,
  examples: null, examplesError: null, exampleName: 'verified-fix',
  quick: { url: params.get('repo') || '', ref: params.get('ref') || '', sending: false, error: null, prepared: null },
  tryRepo: { url: params.get('repo') || '', busy: false, error: null, result: null },
  approval: {}, // case_id -> { name, seen, sending, error, justLocked }
  history: {},  // case_id -> { items, loading, loaded, error }
  correct: {},  // case_id -> { open, name, by, reason, ok, sending, error, done }
  runForm: {},  // case_id -> { level, repair, dep, sending, error }
  run: { id: null, data: null, error: null, patch: null, pollError: null },
  spotlight: null, // { id, data, error }: the featured run on the overview
  pick: null, // case_id chosen on the beginner "Choose a project" step
  evaluation: { source: 'results', data: null, metrics: null, error: null, loaded: false },
  open: new Set(), closed: new Set(),
};
const approvalOf = id => (state.approval[id] ||= { name: '', seen: false, sending: false, error: null, justLocked: false });
const historyOf = id => (state.history[id] ||= { items: null, loading: false, loaded: false, error: null });
const correctOf = id => (state.correct[id] ||= { open: false, name: '', by: '', reason: '', ok: false, sending: false, error: null, done: false });
const formOf = id => (state.runForm[id] ||= { level: 'functional', repair: 'none', dep: 'offline_snapshot', sending: false, error: null });
const caseById = id => state.cases?.find(c => c.case_id === id) || null;
const isLocked = c => c?.review?.status === 'approved' && c.review.acceptance_sha256 === c.acceptance_sha256;
const reviewBadge = c => (isLocked(c) ? badge('approved', lbl('review', 'approved')) : badge(c.review?.status || 'none', lbl('review', c.review?.status)));
const apiDown = () => Boolean(state.healthError);
// The published demo (web/demo/build.py) serves recorded runs as static files and can't run or approve anything.
const readOnly = () => state.health?.read_only === true;
const nightlyLink = () => h('a', { href: `https://johncat88425-del.github.io/fresh-eyes/nightly.html?lang=${lang}` }, t('nightly_link'));

function route() {
  const [page, id] = location.hash.replace(/^#\/?/, '').split('/');
  return { page: page || 'home', id: id ? decodeURIComponent(id) : null };
}
const go = hash => { location.hash = hash; };
window.addEventListener('hashchange', () => { onRoute(); window.scrollTo(0, 0); });
function onRoute() {
  const r = route();
  if ((r.page === 'runs' || r.page === 'answer') && r.id) { if (state.run.id !== r.id) openRun(r.id); } else stopPolling();
  if (r.page === 'evaluation' && !state.evaluation.loaded) loadEvaluation(state.evaluation.source);
  render();
}
// Re-render, unless someone is typing in the page: then wait until they leave the field.
let deferred = false;
function softRender() {
  const a = document.activeElement;
  if (a && a.closest('#view') && a.matches('input, select, textarea')) {
    if (!deferred) { deferred = true; a.addEventListener('focusout', () => { deferred = false; setTimeout(render); }, { once: true }); }
    return;
  }
  render();
}

/* ------------------------------------------------------------------ chrome */
function renderChrome() {
  document.documentElement.lang = lang === 'zh' ? 'zh-Hant' : 'en';
  const r = route();
  const navItem = (hash, page, text) => h('a', { href: hash, 'aria-current': r.page === page ? 'page' : null }, text);
  // The beginner pages show only Help and a way to the detailed view; the detailed pages keep their full menu.
  const simple = SIMPLE_PAGES.has(r.page) || !KNOWN_PAGES.has(r.page);
  document.body.classList.toggle('simple', simple);
  mount('nav', simple
    ? [h('button', { class: 'b-chip', type: 'button', onclick: openHelp }, '? ', t('b_help')), h('a', { class: 'b-chip', href: '#/dashboard' }, t('b_detailed'))]
    : [navItem('#/', 'home', t('nav_simple')), navItem('#/dashboard', 'dashboard', t('nav_overview')), navItem('#/runs', 'runs', t('nav_runs')), navItem('#/evaluation', 'evaluation', t('nav_evaluation'))]);
  const hl = state.health;
  const conn = document.getElementById('conn');
  conn.className = `conn ${state.healthError ? 'down' : hl ? 'ok' : ''}`;
  conn.replaceChildren(h('span', { class: 'dot', 'aria-hidden': 'true' }),
    state.healthError ? t('conn_down') : readOnly() ? t('demo_conn')
      : hl ? `${t('conn_ok')} · ${hl.execution_enabled ? t('runner_short_on') : t('runner_short_off')}` : t('loading'));
  document.getElementById('lang').textContent = t('lang');
  mount('banner', state.healthError ? h('div', { class: 'banner', role: 'alert' }, h('div', { class: 'wrap' },
    h('strong', {}, t('api_banner', { api: API_BASE })), h('span', {}, t('api_banner_hint'))))
    : readOnly() ? h('div', { class: 'banner info' }, h('div', { class: 'wrap' }, simple ? h('strong', {}, t('b_demo_banner'))
      : [h('strong', {}, t('demo_banner')), h('span', {}, t('demo_banner_hint'))]))
      : null);
  if (simple) return mount('footer', h('span', {}, t('b_footer')), nightlyLink(), h('a', { href: '#/dashboard' }, t('b_footer_detailed')));
  mount('footer', h('span', {}, readOnly() ? t('demo_footer', { at: when(hl.demo_built_at) }) : t('footer', { api: API_BASE })),
    h('span', {}, hl ? (hl.paid_calls_enabled ? t('paid_on') : t('paid_off')) : null, hl ? ' · ' : null, nightlyLink(), ' · ', h('a', { href: '#/states' }, t('nav_states'))));
}

function render() {
  renderChrome();
  const r = route();
  const views = { home: viewHome, repo: viewOwnRepo, start: r.id ? viewPromise : viewChoose, answer: viewAnswer, examples: viewExamples, try: viewTry,
    dashboard: viewOverview, cases: viewCase, runs: r.id ? viewRun : viewRuns, evaluation: viewEvaluation, states: viewStates };
  mount('view', (views[r.page] || viewHome)(r.id));
}

/* ------------------------------------------------------------------ beginner front door (B24) */
// What a newcomer sees first: one question and one main button per screen, plain words, big text, every status a
// symbol plus a word. Same data as the dashboard (now #/dashboard). Design and its checks: design/beginner/README.md.
const SIMPLE_PAGES = new Set(['home', 'repo', 'start', 'answer', 'examples', 'try']);
const KNOWN_PAGES = new Set([...SIMPLE_PAGES, 'dashboard', 'cases', 'runs', 'evaluation', 'states']);
function sv(tag, attrs, ...kids) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  el.append(...kids);
  return el;
}
const B_SYM = { ok: '✓', bad: '✗', person: '!', run: '…', plain: '✓' };
const bStatus = (kind, text, sym = B_SYM[kind]) => h('span', { class: `b-status b-${kind}` }, h('span', { 'aria-hidden': 'true' }, sym), text);
const caseName = c => repoName(c?.repo_url).split('/').pop() || c?.case_id || '—';
const casePlain = c => (STR[lang][`plain_case_${c.case_id}`] ? t(`plain_case_${c.case_id}`) : c.goal);
const finished = r => Boolean(r) && r.run_state !== 'queued' && r.run_state !== 'running';
const caseRunsOf = id => (state.runs || []).filter(r => r.case_id === id);
// The check a newcomer is shown for a project: a fix that held up, else the latest finished check.
const answerRunFor = id => { const done = caseRunsOf(id).filter(finished); return done.find(r => r.result_status === 'verified_fix' && verdictOf({ human_review: r.result_review }) !== 'rejected') || done[0] || null; };
const canTry = c => !readOnly() && !apiDown() && state.health?.execution_enabled === true && isLocked(c);
const bPage = (...kids) => h('div', { class: 'b-page' }, ...kids);

function bSteps(n) {
  return [h('p', { class: 'b-step-of' }, t('b_step_of', { n })),
    h('ol', { class: 'b-steps', 'aria-label': t('b_progress') }, ['b_step1', 'b_step2', 'b_step3'].map((k, i) =>
      h('li', { class: i + 1 < n ? 'done' : null, 'aria-current': i + 1 === n ? 'step' : null },
        h('span', { class: 'n' }, i + 1 < n ? '✓' : String(i + 1)), h('span', { class: 't' }, t(k)))))];
}
const bNav = (href, key, restart = true) => h('div', { class: 'b-nav' }, h('a', { href }, `← ${t(key)}`), restart ? h('a', { href: '#/' }, t('b_restart')) : null);

function bArt() {
  const st = (fill, stroke, w, extra = '') => `fill:${fill};stroke:${stroke};stroke-width:${w};stroke-linecap:round;stroke-linejoin:round;${extra}`;
  return sv('svg', { class: 'b-art', viewBox: '0 0 320 260', role: 'img', 'aria-label': t('b_art') },
    sv('rect', { x: 18, y: 26, width: 120, height: 160, rx: 12, style: st('var(--surface)', 'var(--b-muted)', 3) }),
    sv('path', { d: 'M40 60h70M40 86h76M40 112h60M40 138h70', style: st('none', 'var(--b-muted)', 5) }),
    sv('path', { d: 'M118 150l10 10 18-22', style: st('none', 'var(--b-ok)', 6) }),
    sv('rect', { x: 150, y: 96, width: 150, height: 100, rx: 10, style: st('var(--surface)', 'var(--text)', 3) }),
    sv('path', { d: 'M136 212h178l-12-16H148z', style: st('var(--border)', 'var(--text)', 3) }),
    sv('circle', { cx: 225, cy: 146, r: 30, style: 'fill:var(--b-ok-bg)' }),
    sv('path', { d: 'M210 146l11 11 20-22', style: st('none', 'var(--b-ok)', 7) }),
    sv('path', { d: 'M150 40c20-14 44-14 64 0', style: st('none', 'var(--accent)', 3, 'stroke-dasharray:6 8') }),
    sv('circle', { cx: 236, cy: 44, r: 16, style: 'fill:var(--accent)' }), sv('circle', { cx: 236, cy: 44, r: 6, style: 'fill:var(--accent-ink)' }));
}

function viewHome() {
  return bPage(
    h('section', { class: 'b-hero' },
      h('div', {}, h('h1', {}, t('b_hero')), h('p', { class: 'b-lead' }, t('b_hero_sub')),
        h('div', { class: 'b-actions' }, h('a', { class: 'b-btn', href: readOnly() ? '#/try' : '#/repo' }, `${t('own_repo_start')} →`),
          h('a', { class: 'b-btn secondary', href: '#/start' }, t('own_repo_examples'))),
        h('a', { class: 'b-quiet', href: '#/examples' }, t('b_see_example'))),
      h('div', {}, bArt())),
    h('h2', { class: 'sr-only' }, t('b_how')),
    h('ol', { class: 'b-three' }, [1, 2, 3].map(i => h('li', {},
      h('span', { class: 'n', 'aria-hidden': 'true' }, String(i)), h('strong', {}, t(`b_how${i}_t`)), t(`b_how${i}_d`)))));
}

// Step 1. Continue stays disabled until a card is chosen; choosing only updates the button, so focus stays on the card.
function viewChoose() {
  const head = [bNav('#/', 'b_back', false), ...bSteps(1), h('h1', {}, t('b_choose_title')), h('p', { class: 'b-lead' }, t('b_choose_lead'))];
  const own = h('p', { class: 'b-note' }, h('a', { href: readOnly() ? '#/try' : '#/repo' }, t('own_repo_start')));
  if (state.casesError) return bPage(head, own, errorBox(state.casesError));
  if (!state.cases) return bPage(head, h('p', {}, t('loading')));
  const pick = caseById(state.pick) ? state.pick : null;
  const hint = h('p', { class: 'b-hint', id: 'b-hint', tabindex: '-1', hidden: Boolean(pick) }, t('b_choose_hint'));
  const next = h('button', { class: 'b-btn', type: 'button', 'aria-disabled': pick ? 'false' : 'true', 'aria-describedby': 'b-hint', onclick: () => {
    if (!state.pick) { hint.hidden = false; hint.focus(); return; }
    go(`#/start/${encodeURIComponent(state.pick)}`);
  } }, `${t('b_continue')} →`);
  const choices = h('fieldset', { class: 'b-choices' }, h('legend', { class: 'sr-only' }, t('b_projects')),
    state.cases.map(c => h('label', { class: 'b-choice' },
      h('input', { type: 'radio', name: 'b-project', value: c.case_id, checked: pick === c.case_id, onchange: () => {
        state.pick = c.case_id; next.setAttribute('aria-disabled', 'false'); hint.hidden = true;
      } }),
      h('span', {}, h('span', { class: 'name' }, caseName(c)), h('span', { class: 'what' }, casePlain(c)),
        h('span', { class: 'tag' }, isLocked(c) ? bStatus('plain', t('b_checked_person')) : bStatus('person', t('b_not_checked')),
          c.group === 'boundary' ? [' ', bStatus('person', t('b_boundary_tag'))] : null)))));
  return bPage(head, own, choices, h('div', { class: 'b-actions' }, next), hint);
}

function viewOwnRepo() {
  return bPage(bNav('#/', 'b_back', false), h('h1', {}, t('own_repo_title')),
    h('p', { class: 'b-lead' }, t('own_repo_scope')), quickCheckCard(),
    h('p', { class: 'b-note' }, t('own_repo_functional')),
    h('a', { href: '#/try' }, t('b_try_own')),
    h('a', { href: '#/start' }, t('own_repo_examples')));
}

// The first locked example as before → after. For HTML checks the "after" is drawn from the note itself (heading,
// bold, italic); the real check compares the produced HTML elements with the approved list.
function bExample(c) {
  const f = (c.fixtures || [])[0];
  if (!f) return null;
  const [inKey, outKey] = f.validator === 'html_elements' ? ['b_in_note', 'b_out_page'] : f.validator === 'json' ? ['b_in_data', 'b_out_data'] : ['b_in_other', 'b_out_other'];
  const inline = text => text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/).filter(Boolean)
    .map(p => (p.startsWith('**') ? h('strong', {}, p.slice(2, -2)) : p.startsWith('*') ? h('em', {}, p.slice(1, -1)) : p));
  const out = f.validator === 'html_elements'
    ? h('div', { class: 'b-rendered' }, String(f.input).split('\n').filter(l => l.trim())
      .map(l => (/^#+\s/.test(l) ? h('div', { class: 'b-h' }, l.replace(/^#+\s+/, '')) : h('p', {}, inline(l)))))
    : h('pre', { class: 'b-paper b-flat' }, f.validator === 'json' ? JSON.stringify(f.expected, null, 2) : String(f.expected));
  return h('div', { class: 'b-io' },
    h('div', {}, h('div', { class: 'b-label' }, t(inKey)), h('pre', { class: 'b-paper' }, String(f.input).trimEnd())),
    h('span', { class: 'b-arrow', 'aria-hidden': 'true' }, '→'),
    h('div', {}, h('div', { class: 'b-label' }, t(outKey)), out));
}

// Step 2. A new check starts only when this copy can really run one (runner on, case locked, not the read-only demo);
// otherwise the button opens this project's latest real check and the page says so.
function viewPromise(id) {
  const head = [bNav('#/start', 'b_back_projects'), ...bSteps(2)];
  if (state.casesError) return bPage(head, errorBox(state.casesError));
  if (!state.cases) return bPage(head, h('p', {}, t('loading')));
  const c = caseById(id);
  if (!c) return bPage(head, h('h1', {}, t('case_not_found_page')), h('a', { class: 'b-btn', href: '#/start' }, t('b_back_projects')));
  state.pick = c.case_id;
  const f = formOf(c.case_id);
  const answer = answerRunFor(c.case_id);
  const action = canTry(c)
    ? h('button', { class: 'b-btn', type: 'button', disabled: f.sending, onclick: () => tryNow(c) }, f.sending ? t('b_starting') : `${t('b_try')} →`)
    : answer ? h('a', { class: 'b-btn', href: `#/answer/${encodeURIComponent(answer.run_id)}` }, `${t('b_see_result')} →`) : null;
  return bPage(head,
    h('h1', {}, t('b_promise_title')),
    h('p', { class: 'b-lead' }, t(STR[lang][`plain_case_${c.case_id}`] ? 'b_project_is' : 'b_project_goal', { name: caseName(c), what: casePlain(c) })),
    bExample(c),
    h('p', {}, isLocked(c) ? bStatus('plain', t('b_checked_person')) : bStatus('person', t('b_not_checked'))),
    h('p', {}, t(isLocked(c) ? 'b_locked_sentence' : 'b_unlocked_sentence')),
    c.group === 'boundary' ? h('div', { class: 'notice info' }, h('p', {}, t('b_boundary_promise'))) : null,
    canTry(c) ? null : h('p', { class: 'b-note' }, t(answer ? 'b_latest_note' : 'b_no_runs')),
    action ? h('div', { class: 'b-actions' }, action) : null,
    f.error ? errorBox(f.error) : null,
    foldable(`b-promise-${c.case_id}`, t('b_details'), h('div', { class: 'b-tech' },
      h('p', {}, `${t('b_code')}: `, h('a', { href: `${c.repo_url}/tree/${c.commit_sha}`, target: '_blank', rel: 'noopener noreferrer' }, repoName(c.repo_url))),
      h('p', {}, `${t('b_where')}: `, h('code', {}, c.readme_source)),
      isLocked(c) ? h('p', {}, `${t('b_checked_by')}: ${c.review.reviewed_by} · ${when(c.review.approved_at)}`) : null,
      h('p', {}, t('b_two_examples', { n: (c.fixtures || []).length })),
      h('p', {}, h('a', { href: `#/cases/${encodeURIComponent(c.case_id)}` }, t('b_case_detail'))))));
}
async function tryNow(c) {
  const f = formOf(c.case_id); const hl = state.health || {};
  // The setup this project's last check used, where that is still allowed; otherwise the offline package copy.
  const last = caseRunsOf(c.case_id)[0]?.dependency_mode;
  const dep = last && (hl.dependency_modes || []).includes(last) && (last !== 'pypi_live' || hl.live_pypi_enabled) ? last : 'offline_snapshot';
  f.sending = true; f.error = null; render();
  try {
    const created = await api.startRun({ case_id: c.case_id, verification_level: 'functional', repair_mode: 'none', dependency_mode: dep });
    f.sending = false; loadRuns(); go(`#/answer/${encodeURIComponent(created.run_id)}`);
    return;
  } catch (e) { f.error = e; }
  f.sending = false; render();
}

// Step 3 while a check runs: three plain stages, no logs.
function bProgress(run) {
  const phase = run.phase || run.evidence?.current_phase || '';
  const at = run.run_state === 'queued' ? 0 : /candidate|verif|repair|diagnos|patch/.test(phase) ? 2 : 1;
  return [h('h1', {}, t('b_trying_title')), h('p', { class: 'b-lead' }, t('b_trying_lead')),
    h('div', { class: 'b-meter', role: 'progressbar', 'aria-label': t('b_progress'), 'aria-valuemin': '0', 'aria-valuemax': '3', 'aria-valuenow': String(at) },
      h('span', { style: `width:${Math.round(((at + 0.5) / 3) * 100)}%` })),
    h('ol', { class: 'b-stages' }, ['b_stage1', 'b_stage2', 'b_stage3'].map((k, i) =>
      h('li', { class: i < at ? 'done' : i === at ? 'now' : null, 'aria-current': i === at ? 'step' : null },
        h('span', { class: 'm', 'aria-hidden': 'true' }, i < at ? '✓' : String(i + 1)),
        h('span', {}, t(k), i === at ? h('span', { class: 'sr-only' }, ` (${t('b_now')})`) : null))))];
}

const B_KIND = { already_passes: 'ok', verified_fix: 'fixed', unresolved: 'bad', needs_review: 'person', needs_input: 'input', infrastructure_error: 'infra' };
function beginnerKind(run) {
  if (run.result_status === 'verified_fix' && verdictOf({ human_review: run.result_review }) === 'rejected') return 'person';
  return B_KIND[run.result_status] || 'other';
}
function bWord(run) {
  const k = beginnerKind(run);
  return k === 'ok' ? bStatus('ok', t('b_w_ok')) : k === 'fixed' ? bStatus('ok', t('b_w_fixed')) : k === 'bad' ? bStatus('bad', t('b_w_bad'))
    : k === 'person' || k === 'input' ? bStatus('person', t('b_w_person')) : k === 'infra' ? bStatus('run', t('b_w_infra'), '↻')
      : bStatus('plain', lbl('result', run.result_status), '•');
}
function bDiff(text) {
  const lines = String(text).split('\n').filter(l => (l.startsWith('-') && !l.startsWith('---')) || (l.startsWith('+') && !l.startsWith('+++')));
  const shown = lines.slice(0, 6);
  return [h('div', { class: 'b-diff', role: 'group', 'aria-label': t('b_fix_label') }, shown.map(l => h('div', { class: l[0] === '+' ? 'new' : 'old' },
      h('span', { class: 'sign', 'aria-hidden': 'true' }, l[0] === '+' ? '+' : '−'), h('span', { class: 'sr-only' }, t(l[0] === '+' ? 'b_after' : 'b_before')), l.slice(1)))),
    lines.length > shown.length ? h('p', { class: 'b-note' }, t('b_fix_more', { n: lines.length - shown.length })) : null];
}
// At most six plain rows; the full report stays one click away.
function bRows(run) {
  const ev = run.evidence || {}; const attempts = ev.attempts || []; const rows = [];
  const row = (k, title, sub) => h('li', {}, h('span', { class: `m b-${k}`, 'aria-hidden': 'true' }, { ok: '✓', bad: '✗', person: '!', plain: '•' }[k]),
    h('span', {}, h('strong', {}, title), sub ? h('span', { class: 's' }, sub) : null));
  const result = a => [a.status === 'passed' ? 'ok' : 'bad', t(a.status === 'passed' ? 'b_r_ok' : 'b_r_bad')];
  const original = attempts.find(a => a.phase === 'original_readme');
  if (original) { const [k, sub] = result(original); rows.push(row(k, t('b_r_original'), sub)); }
  const calls = run.costs?.model_calls?.length || 0;
  if (ev.diagnosis || calls) rows.push(row('plain', t('b_r_ai'), t('b_r_ai_s', { n: calls, usd: run.costs?.model_usd == null ? t('unknown') : Number(run.costs.model_usd).toFixed(4) })));
  if (run.result_status === 'verified_fix' || attempts.some(a => a.phase === 'candidate_patch')) {
    rows.push(row('plain', t('b_r_changed'), t(run.repair_mode === 'manual_candidate' ? 'b_fix_person' : run.repair_mode === 'model' ? 'b_fix_ai' : 'b_r_changed_s')));
  } else if (calls) rows.push(row('person', t('b_r_ai_none'), t('b_r_ai_none_s')));
  const phaseKey = { candidate_patch: 'b_r_patched', fresh_verification: 'b_r_again' };
  for (const a of attempts.filter(a => a.phase !== 'original_readme')) { const [k, sub] = result(a); rows.push(row(k, t(phaseKey[a.phase] || 'b_r_other'), sub)); }
  rows.push(row('plain', t('b_r_time', { d: fmtDur(runSeconds(run)) }), t('b_r_when', { at: when(run.finished_at || run.created_at) })));
  return h('ol', { class: 'b-timeline' }, rows.slice(0, 6));
}

function viewAnswer(id) {
  const head = [bNav('#/start', 'b_back_projects'), ...bSteps(3)];
  const r = state.run;
  if (r.error) return bPage(head, r.error.code === 'run_not_found' ? h('h1', {}, t('run_missing')) : errorBox(r.error),
    h('div', { class: 'b-actions' }, h('a', { class: 'b-btn', href: '#/start' }, t('b_another'))));
  if (!r.data) return bPage(head, h('p', { class: 'b-lead' }, t('loading')));
  const run = r.data;
  const poll = r.pollError ? h('div', { class: 'notice warn' }, h('p', {}, t('poll_error'))) : null;
  if (!finished(run)) return bPage(head, poll, bProgress(run));
  const kind = beginnerKind(run);
  const big = (k, sym) => h('span', { class: `b-big b-${k}`, 'aria-hidden': 'true' }, sym);
  const mark = { ok: [big('ok', '✓')], fixed: [big('bad', '✗'), h('span', { class: 'b-arrow', 'aria-hidden': 'true' }, '→'), big('ok', '✓')],
    bad: [big('bad', '✗')], person: [big('person', '!')], input: [big('person', '!')], infra: [big('run', '↻')], other: [] }[kind];
  const title = { ok: 'b_ok_title', fixed: 'b_fixed_title', bad: 'b_bad_title', person: 'b_person_title', input: 'b_input_title', infra: 'b_infra_title', other: 'b_other_title' }[kind];
  const sub = { ok: t('b_ok_sub'), fixed: t('b_fixed_sub'), bad: t('b_bad_sub'), person: t(run.repair_mode === 'model' && run.result_status !== 'verified_fix' ? 'b_person_ai' : 'b_person_sub'),
    input: t('b_input_sub'), infra: t('b_infra_sub'), other: lbl('resultDesc', run.result_status) }[kind];
  const patch = r.patch?.text;
  const fixRun = kind === 'bad' || kind === 'person' ? caseRunsOf(run.case_id).find(x => x.result_status === 'verified_fix' && verdictOf({ human_review: x.result_review }) !== 'rejected' && x.run_id !== run.run_id) : null;
  const c = caseById(run.case_id);
  const boundary = c?.group === 'boundary';
  return bPage(head, poll,
    h('p', { class: 'b-note', style: 'margin:0 0 12px' }, t('b_project_label', { name: c ? caseName(c) : run.case_id })),
    h('div', { class: 'b-mark' }, mark, bWord(run)),
    h('h1', {}, t(title)),
    h('p', { class: 'b-lead' }, sub),
    resultReviewNotice(run),
    kind === 'fixed' && patch ? bDiff(patch) : null,
    kind === 'fixed' ? h('p', {}, bStatus('plain', t(run.repair_mode === 'manual_candidate' ? 'b_fix_person' : 'b_fix_ai'), '✎')) : null,
    // A saved AI proposal re-checked on its own: say so here too, not only in the detailed view.
    kind === 'fixed' && run.evidence?.candidate_provenance?.kind === 'unchanged_saved_model_candidate'
      ? h('p', { class: 'b-note' }, t('b_saved_ai_note'), ' ',
          h('a', { href: `#/runs/${encodeURIComponent(run.evidence.candidate_provenance.source_run_id)}` }, t('b_saved_ai_link'))) : null,
    kind === 'fixed' ? maintainerNote(run) : null,
    boundary && kind !== 'fixed' ? h('div', { class: 'notice info' }, h('p', {}, h('strong', {}, t('b_boundary_answer')), ' ',
      STR[lang][`b_boundary_${run.case_id}`] ? t(`b_boundary_${run.case_id}`) : null)) : null,
    kind === 'bad' && !boundary ? h('p', {}, t(run.repair_mode === 'none' || !run.repair_mode ? 'b_bad_nofix' : 'b_bad_untrusted')) : null,
    h('div', { class: 'b-actions' }, h('a', { class: 'b-btn', href: '#/start' }, t('b_another')),
      kind === 'fixed' && patch ? downloadButton(run, patch, '', 'b-btn secondary', t('b_download')) : null,
      fixRun ? h('a', { class: 'b-btn secondary', href: `#/answer/${encodeURIComponent(fixRun.run_id)}` }, t('b_see_fix')) : null),
    foldable(`b-answer-${run.run_id}`, t('b_details'), h('div', { class: 'b-tech' }, bRows(run),
      h('p', { style: 'margin-top:16px' }, h('a', { href: `#/runs/${encodeURIComponent(run.run_id)}` }, t('b_full_report'))))));
}

// "Try your own project": the README is read in this browser and planned with the backend's rules; nothing runs.
function viewTry() {
  const s = state.tryRepo;
  const head = [bNav('#/', 'b_back_home', false), h('h1', {}, t('try_title')), h('p', { class: 'b-lead' }, t('try_lead')),
    h('p', { class: 'b-note' }, t('try_scope'))];
  const form = h('form', { class: 'b-try', onsubmit: e => { e.preventDefault(); runTry(); } },
    h('label', { class: 'b-label', for: 'try-url' }, t('repo_url')),
    h('input', { id: 'try-url', type: 'text', inputmode: 'url', autocomplete: 'off', spellcheck: 'false',
      placeholder: 'https://github.com/owner/repo', value: s.url, oninput: e => { s.url = e.target.value; } }),
    h('div', { class: 'b-actions' }, h('button', { class: 'b-btn', type: 'submit', disabled: s.busy }, s.busy ? t('try_reading') : `${t('try_go')} →`)),
    h('p', { class: 'b-note' }, t('try_limit')));
  return bPage(head, form,
    s.error ? h('div', { class: 'notice warn', role: 'alert' }, h('p', {}, t(`try_err_${s.error}`))) : null,
    s.result ? tryResult(s.result) : null,
    h('div', { class: 'notice info' }, h('p', {}, t('try_honest'))),
    h('div', { class: 'b-actions' }, h('a', { class: 'b-btn secondary', href: '#/examples' }, t('try_see_real'))));
}
async function runTry() {
  const s = state.tryRepo;
  s.busy = true; s.error = null; s.result = null; render();
  try { s.result = await readRepoReadme(s.url); }
  catch (e) { s.error = e.code || 'network'; }
  s.busy = false; render();
}
function tryResult(r) {
  const p = { startup_candidates: [], ...r.plan };
  const line = src => Number(String(src).split(':L')[1]);
  const shown = step => h('li', {}, h('code', {}, step.argv.join(' ')), ' ',
    h('span', { class: 'small muted' }, t('try_line', { file: r.readme, n: line(step.source) })));
  const reasons = [];
  if (!r.readme) reasons.push(t('try_why_readme'));
  else if (p.status === 'unsupported') reasons.push(t(/256 KiB/.test(p.reason) ? 'try_why_big' : 'try_why_not_python'));
  else {
    if (!p.setup.length) reasons.push(t('try_why_no_install'));
    if (p.setup.length > 1) reasons.push(t('try_why_many_install', { n: p.setup.length }));
    if (!p.startup_candidates.length) reasons.push(t('try_why_no_start'));
    if (p.startup_candidates.length > 1) reasons.push(t('try_why_many_start', { n: p.startup_candidates.length }));
    if (p.ignored_command_sources.length) reasons.push(t(p.ignored_command_sources.length > 1 ? 'try_why_ignored_n' : 'try_why_ignored',
      { lines: p.ignored_command_sources.map(line).join(', ') }));
  }
  const status = p.status === 'ready' ? bStatus('ok', t('try_ready')) : p.status === 'needs_input' ? bStatus('person', t('try_needs')) : bStatus('bad', t('try_unsupported'));
  const at = `https://github.com/${r.owner}/${r.repo}/blob/${r.sha}/${encodeURIComponent(r.readme || '')}`;
  // A project we have really checked: point to that recording, which is a real run rather than a plan.
  const repoUrl = `https://github.com/${r.owner}/${r.repo}`.toLowerCase();
  const recorded = (state.runs || []).filter(finished).find(x => (x.repo_url || '').toLowerCase() === repoUrl && x.commit_sha === r.sha);
  return h('section', { class: 'b-try-result', 'aria-live': 'polite' },
    h('p', { class: 'b-note' }, `${r.owner}/${r.repo} · ${t('try_commit')} `, h('code', {}, r.sha.slice(0, 7)),
      r.readme ? [' · ', h('a', { href: at, target: '_blank', rel: 'noopener noreferrer' }, r.readme)] : null),
    h('p', {}, status),
    p.status === 'ready'
      ? h('ol', { class: 'b-plan' }, h('li', {}, t('try_step_install'), h('ul', {}, p.setup.map(shown))),
          h('li', {}, t('try_step_start'), h('ul', {}, shown(p.startup))), h('li', {}, t('try_step_check')))
      : h('ul', { class: 'b-plan' }, reasons.map(x => h('li', {}, x))),
    p.status !== 'ready' && (p.setup.length || p.startup_candidates.length)
      ? h('div', {}, h('p', { class: 'b-label' }, t('try_found')), h('ul', { class: 'b-plan' }, [...p.setup, ...p.startup_candidates].map(shown))) : null,
    p.status === 'ready' ? h('p', {}, h('button', { class: 'b-btn', type: 'button', onclick: () => {
      state.quick.url = `https://github.com/${r.owner}/${r.repo}`; state.quick.ref = r.sha; state.quick.prepared = null; go('#/repo');
    } }, t('try_run_local'))) : null,
    recorded ? h('p', {}, h('a', { class: 'b-btn secondary', href: `#/answer/${encodeURIComponent(recorded.run_id)}` }, `${t('try_recorded')} →`)) : null);
}

// The build's highlights first (the demo's chosen stories), then one finished check per project and outcome.
function bExampleRuns() {
  const runs = (state.runs || []).filter(finished);
  const list = (state.health?.demo_highlights || []).map(x => runs.find(r => r.run_id === x.run_id)).filter(Boolean);
  const seen = new Set(list.map(r => `${r.case_id}|${r.result_status}|${r.repair_mode}`));
  // Every project once before any project a second time, so the page isn't mostly md2html.
  const project = r => r.repo_url || r.case_id;
  const shown = new Set(list.map(project));
  for (const pass of [true, false]) {
    for (const r of runs) {
      if (list.length >= 12) break;
      const key = `${r.case_id}|${r.result_status}|${r.repair_mode}`;
      if (seen.has(key) || (pass && shown.has(project(r)))) continue;
      seen.add(key); shown.add(project(r)); list.push(r);
    }
  }
  return list;
}
function bExampleText(r) {
  if (r.result_status === 'verified_fix' && verdictOf({ human_review: r.result_review }) === 'rejected') return t('b_ex_person');
  if (caseById(r.case_id)?.group === 'boundary' && r.result_status !== 'verified_fix') return t('b_ex_boundary');
  if (r.result_status === 'startup_only') return t('b_ex_startup');
  const k = beginnerKind(r);
  if (k === 'fixed') return t(r.repair_mode === 'model' ? 'b_ex_fix_ai' : 'b_ex_fix_person');
  if (k === 'bad' && r.repair_mode === 'model') return t('b_ex_ai_failed');  // the AI tried and did not fix it
  if (k === 'person') return t(r.repair_mode === 'model' ? 'b_ex_ai_stop' : 'b_ex_person');
  return t(`b_ex_${k || 'other'}`);
}
function viewExamples() {
  const head = [bNav('#/', 'b_back_home', false), h('h1', {}, t('b_ex_title')), h('p', { class: 'b-lead' }, t('b_ex_lead'))];
  if (state.runsError) return bPage(head, errorBox(state.runsError));
  if (!state.runs) return bPage(head, h('p', {}, t('loading')));
  const list = bExampleRuns();
  return bPage(head,
    list.length ? h('ul', { class: 'b-rows' }, list.map(r => h('li', {},
      h('span', {}, h('span', { class: 'name' }, caseById(r.case_id) ? caseName(caseById(r.case_id)) : r.case_id || repoName(r.repo_url).split('/').pop()),
        h('span', { class: 'what' }, bExampleText(r)), bWord(r)),
      h('a', { class: 'b-btn', href: `#/answer/${encodeURIComponent(r.run_id)}` }, `${t('b_see_it')} →`)))) : h('p', { class: 'b-lead' }, t('b_ex_empty')),
    h('div', { class: 'b-actions' }, h('a', { class: 'b-btn secondary', href: '#/start' }, t('b_choose_self'))));
}

// Help: a native modal dialog, so Escape closes it and focus returns to the Help button.
function openHelp() {
  document.getElementById('b-help')?.remove();
  const icon = (...shapes) => sv('svg', { width: 48, height: 48, viewBox: '0 0 48 48', 'aria-hidden': 'true',
    style: 'fill:none;stroke:var(--accent);stroke-width:3;stroke-linecap:round;stroke-linejoin:round' }, ...shapes);
  const pics = [[icon(sv('path', { d: 'M12 5h17l8 8v30H12z' }), sv('path', { d: 'M18 20h13M18 27h13M18 34h9' })), 'b_pic1'],
    [icon(sv('rect', { x: 9, y: 10, width: 30, height: 21, rx: 2 }), sv('path', { d: 'M4 38h40l-4-7H8z' })), 'b_pic2'],
    [icon(sv('circle', { cx: 24, cy: 24, r: 18 }), sv('path', { d: 'M15 24l6 6 12-13' })), 'b_pic3']];
  const dlg = h('dialog', { class: 'b-help', id: 'b-help', 'aria-labelledby': 'b-help-title' }, h('div', { class: 'inner' },
    h('h2', { id: 'b-help-title' }, t('b_help_title')), h('p', {}, t('b_help_p1')), h('p', {}, t('b_help_p2')),
    h('div', { class: 'b-picture', 'aria-hidden': 'true' }, pics.flatMap(([svg, k], i) => [i ? h('span', { class: 'b-arrow' }, '→') : null, h('div', {}, svg, h('div', {}, t(k)))])),
    h('h3', {}, t('b_sym_title')),
    h('ul', { class: 'b-legend' },
      h('li', {}, bStatus('ok', t('b_w_ok')), ' ', t('b_sym_ok')),
      h('li', {}, bStatus('bad', t('b_w_bad')), ' ', t('b_sym_bad')),
      h('li', {}, bStatus('person', t('b_w_person')), ' ', t('b_sym_person'))),
    h('p', {}, t('b_help_safe'), readOnly() ? ` ${t('b_help_recorded')}` : null),
    h('div', { class: 'b-actions' }, h('button', { class: 'b-btn', type: 'button', onclick: () => dlg.close() }, t('b_got_it')))));
  dlg.addEventListener('close', () => dlg.remove());
  dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
  document.body.append(dlg);
  dlg.showModal();
}

/* ------------------------------------------------------------------ overview (dashboard, #/dashboard) */
// ① Primary + secondary: the one run that best shows what Fresh Eyes does gets the big module;
// the other numbers are small tiles to its right and below.
function featuredRunId() {
  const hl = state.health?.demo_highlights || [];
  const pick = hl.find(x => x.key === 'model_fix') || hl.find(x => x.key === 'fix');
  if (pick) return pick.run_id;
  const runs = state.runs || [];
  return (runs.find(r => r.result_status === 'verified_fix') || runs[0])?.run_id || null;
}
function loadSpotlight(id) {
  if (state.spotlight?.id === id) return;
  state.spotlight = { id, data: null, error: null };
  api.run(id).then(d => { if (state.spotlight.id === id) { state.spotlight.data = d; softRender(); } })
    .catch(e => { if (state.spotlight.id === id) { state.spotlight.error = e; softRender(); } });
}
function spotlightCard() {
  const id = featuredRunId();
  if (!id) return card(h('h2', {}, t('spot_title')), h('p', { class: 'empty' }, state.runs ? t('spot_none') : t('loading')));
  loadSpotlight(id);
  const run = state.spotlight.data;
  if (!run) return card(h('h2', {}, t('spot_title')), state.spotlight.error ? errorBox(state.spotlight.error) : h('p', { class: 'empty' }, t('loading')));
  const ev = run.evidence || {}; const attempts = ev.attempts || [];
  const steps = [];
  const original = attempts.find(a => a.phase === 'original_readme');
  if (original) steps.push([lbl('phase', 'original_readme'), original.status]);
  if (run.patch_available || (ev.repair_rounds || []).length) steps.push([run.repair_mode === 'manual_candidate' ? t('spot_patch_manual') : t('spot_patch_model'), 'patch']);
  for (const a of attempts.filter(a => a.phase !== 'original_readme')) steps.push([lbl('phase', a.phase), a.status]);
  const mark = s => (s === 'passed' ? badge('passed', lbl('attempt', 'passed')) : s === 'failed' ? badge('failed', lbl('attempt', 'failed')) : s === 'patch' ? badge('warn', t('patch')) : badge('neutral', lbl('attempt', s)));
  const flow = h('div', { class: 'flow' }, steps.flatMap(([name, status], i) => [i ? h('span', { class: 'arrow', 'aria-hidden': 'true' }, '→') : null,
    h('div', { class: 'step' }, h('span', { class: 't' }, `${i + 1}`), h('strong', { class: 'small' }, name), h('div', {}, mark(status)))]));
  return h('section', { class: `card primary result-head r-${run.result_status || 'none'}` },
    h('div', { class: 'card-head' }, h('h2', {}, t('spot_title')), h('span', { class: 'small muted mono' }, `${repoName(run.repo_url) || run.case_id} · ${short(run.run_id)}`)),
    h('div', { class: 'result-line' }, badge(run.result_status || 'none', lbl('result', run.result_status), null, 'lg'),
      run.repair_mode ? badge(run.repair_mode, lbl('repair', run.repair_mode)) : null,
      run.data_origin === 'recorded_replay' ? badge('info', t('replay_badge')) : null),
    h('p', { style: 'margin:0; font-size:16px' }, lbl('resultDesc', run.result_status)),
    steps.length ? flow : null,
    run.repair_mode === 'manual_candidate' ? h('p', { class: 'small muted', style: 'margin:0' }, t('spot_manual_note')) : null,
    h('div', {}, h('a', { class: 'btn', href: `#/runs/${encodeURIComponent(run.run_id)}` }, t('spot_open'))));
}
function overviewTiles() {
  const runs = state.runs || []; const cases = state.cases || [];
  const count = st => runs.filter(r => r.result_status === st).length;
  const fixes = runs.filter(r => r.result_status === 'verified_fix');
  const tile = (k, v, sub, href) => h(href ? 'a' : 'div', { class: 'tile', href }, h('span', { class: 'k' }, k), h('span', { class: 'v' }, v), sub ? h('span', { class: 's' }, sub) : null);
  return h('div', { class: 'tiles' },
    tile(t('tile_cases'), `${cases.filter(isLocked).length} / ${cases.length}`, t('tile_cases_s'), null),
    tile(t('tile_runs'), String(runs.length), t('tile_runs_s'), '#/runs'),
    tile(t('tile_fixes'), String(fixes.length), t('tile_fixes_s', { m: fixes.filter(r => r.repair_mode === 'manual_candidate').length, a: fixes.filter(r => r.repair_mode === 'model').length }), '#/runs'),
    tile(t('tile_review'), String(count('needs_review')), t('tile_review_s', { p: count('already_passes'), u: count('unresolved') }), '#/runs'));
}
function viewOverview() {
  const intro = h('div', { class: 'stack', style: 'gap:6px' }, h('h1', {}, t('hero_title')), h('p', { class: 'lead' }, t('hero_sub')));
  const how = h('div', { class: 'tiles below' }, [['how1_t', 'how1_d'], ['how2_t', 'how2_d'], ['how3_t', 'how3_d']].map(([a, b], i) =>
    h('div', { class: 'tile' }, h('span', { class: 'k' }, t('how_step', { n: i + 1 })), h('strong', {}, t(a)), h('span', { class: 's' }, t(b)))));
  const primary = h('div', { class: 'lay-primary' }, spotlightCard(), overviewTiles(), how);
  const lastRun = id => state.runs?.find(r => r.case_id === id);
  const cases = state.casesError ? errorBox(state.casesError)
    : !state.cases ? h('p', { class: 'empty' }, t('loading'))
      : !state.cases.length ? h('p', { class: 'empty' }, t('no_cases'))
        : h('div', { class: 'case-grid' }, state.cases.map(c => {
          const last = lastRun(c.case_id);
          return h('a', { class: 'case-card', href: `#/cases/${encodeURIComponent(c.case_id)}` },
            h('span', { class: 'name' }, repoName(c.repo_url)),
            h('span', { class: 'mono small muted' }, `${c.case_id} · ${short(c.commit_sha)}`),
            h('span', { class: 'row2' }, reviewBadge(c)),
            h('span', { class: 'row2 small muted' }, last ? [t('case_last'), ' ', badge(last.result_status || last.run_state, lbl('result', last.result_status)), ' ', when(last.created_at)] : t('case_never')));
        }));
  return [intro, primary,
    h('div', { class: 'grid-main' }, h('div', { class: 'stack' }, h('div', { class: 'section-head' }, h('h2', {}, t('cases_title'))), cases),
      readOnly() ? demoCard() : quickCheckCard()),
    h('div', { class: 'stack' }, h('div', { class: 'section-head' }, h('h2', {}, t('recent_runs')), h('a', { href: '#/runs', class: 'small' }, t('all_runs'))),
      card(runsTable((state.runs || []).slice(0, 5))))];
}

// Read-only demo: where to start reading. The runs are chosen by the build (health.demo_highlights).
function demoCard() {
  const links = (state.health?.demo_highlights || []).filter(x => x?.run_id && x?.key);
  return card(h('h2', {}, t('demo_title')), h('p', { class: 'small muted', style: 'margin:0' }, t('demo_intro')),
    links.length ? h('ol', { class: 'tight' }, links.map(x => h('li', {},
      h('a', { href: `#/runs/${encodeURIComponent(x.run_id)}` }, t(`demo_hl_${x.key}`)),
      h('div', { class: 'small muted' }, t(`demo_hl_${x.key}_d`))))) : null,
    h('p', { class: 'small muted', style: 'margin:0' }, t('demo_local')));
}
function quickCheckCard() {
  const q = state.quick; const hl = state.health;
  const available = hl?.startup_checks === true && !apiDown() && !readOnly();
  return card(
    h('h2', {}, t('s0')), h('p', { class: 'small muted', style: 'margin:0' }, t('s0_hint')),
    !available ? h('div', { class: 'notice info' }, h('p', {}, t(readOnly() ? 'own_repo_demo' : 'own_repo_unavailable'))) : null,
    h('form', { class: 'stack', onsubmit: e => { e.preventDefault(); submitQuick(); } },
      h('div', {}, h('label', { class: 'field', for: 'quick-url' }, t('repo_url')),
        h('input', { id: 'quick-url', type: 'text', inputmode: 'url', autocomplete: 'off', spellcheck: 'false', placeholder: t('repo_url_ph'), value: q.url, oninput: e => { q.url = e.target.value; } })),
      h('div', {}, h('label', { class: 'field', for: 'quick-ref' }, t('commit_opt')),
        h('input', { id: 'quick-ref', type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: t('commit_ph'), value: q.ref, oninput: e => { q.ref = e.target.value; } })),
      h('div', {}, h('button', { class: 'btn', type: 'submit', disabled: q.sending }, q.sending ? t('submitting') : t(available ? 'quick_submit' : 'own_repo_prepare'))),
      q.error ? (q.error.code === 'client' ? h('div', { class: 'notice warn' }, h('p', {}, q.error.message)) : errorBox(q.error)) : null),
    q.prepared ? localRepoInstructions(q.prepared) : null);
}

function localRepoInstructions({ url, ref }) {
  const local = new URL('http://127.0.0.1:5173/');
  local.searchParams.set('lang', lang); local.searchParams.set('repo', url);
  if (ref) local.searchParams.set('ref', ref);
  local.hash = '/repo';
  return h('div', { class: 'stack' }, h('h3', {}, t('own_repo_local_title')),
    h('p', {}, t('own_repo_no_run')),
    h('p', {}, h('a', { href: 'https://github.com/johncat88425-del/fresh-eyes#readme', target: '_blank', rel: 'noopener noreferrer' }, t('own_repo_setup'))),
    h('p', {}, t('own_repo_terminal1')),
    h('pre', {}, 'python3.12 scripts/fresh-eyes serve --enable-github-import --enable-runner --enable-live-pypi --enable-startup-checks'),
    h('p', {}, t('own_repo_terminal2')), h('pre', {}, 'python3.12 web/serve.py 5173'),
    h('a', { class: 'btn', href: local.href, target: '_blank', rel: 'noopener noreferrer' }, t('own_repo_open_local')),
    h('p', { class: 'small muted' }, t('own_repo_no_models')));
}
const GITHUB_REPO = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/?$/;
const FULL_SHA = /^[a-f0-9]{40}$/;
async function submitQuick() {
  const q = state.quick;
  const url = q.url.trim(); const ref = q.ref.trim().toLowerCase();
  if (!GITHUB_REPO.test(url)) { q.error = { code: 'client', message: t('bad_url') }; return render(); }
  if (ref && !FULL_SHA.test(ref)) { q.error = { code: 'client', message: t('bad_sha') }; return render(); }
  if (readOnly() || apiDown() || !state.health?.startup_checks) {
    q.error = null; q.prepared = { url: url.replace(/\/$/, ''), ref }; return render();
  }
  q.prepared = null;
  q.sending = true; q.error = null; render();
  try {
    const body = { repo_url: url.replace(/\/$/, ''), verification_level: 'startup', repair_mode: 'none', dependency_mode: state.health?.startup_checks ? 'pypi_live' : 'offline_snapshot' };
    if (ref) body.ref = ref;
    const created = await api.startRun(body);
    q.sending = false; loadRuns(); go(`#/runs/${encodeURIComponent(created.run_id)}`);
    return;
  } catch (e) { q.error = e; }
  q.sending = false; render();
}

// The backend's fixed check results (validators.py) in the reader's language; anything else is shown as sent.
const CHECK_REASONS = {
  'Expected functional output matched': 'chk_matched',
  'Functional output differs': 'chk_differs',
  'Output could not be parsed': 'chk_unparsed',
  'Missing output, failed process or truncated output': 'chk_missing',
  'Setup did not complete': 'chk_setup',
};
const checkReason = reason => (CHECK_REASONS[reason] ? t(CHECK_REASONS[reason]) : reason);

/* ------------------------------------------------------------------ case page */
function fixtureExpected(f) {
  if (f.validator === 'html_elements') return h('ul', { class: 'tight mono small' }, f.expected.map(([tag, text]) => h('li', {}, `<${tag}> ${text}`)));
  if (f.validator === 'json') return h('pre', {}, JSON.stringify(f.expected, null, 2));
  return h('pre', {}, String(f.expected));
}
function viewCase(id) {
  const crumb = h('div', { class: 'crumb' }, h('a', { href: '#/dashboard' }, t('crumb_overview')), '›', h('span', {}, id));
  if (state.casesError) return [crumb, h('h1', {}, id), errorBox(state.casesError)];
  if (!state.cases) return [crumb, h('p', { class: 'empty' }, t('loading'))];
  const c = caseById(id);
  if (!c) return [crumb, card(h('h1', { class: 'empty' }, t('case_not_found_page')))];
  const commitUrl = `${c.repo_url}/tree/${c.commit_sha}`;
  const head = h('div', { class: 'stack', style: 'gap:8px' },
    h('div', { class: 'result-line' }, h('h1', {}, repoName(c.repo_url)), reviewBadge(c)),
    h('div', { class: 'small muted' }, h('span', { class: 'mono' }, c.case_id), ' · ', h('span', { class: 'mono' }, short(c.commit_sha)), ' · ',
      h('a', { href: commitUrl, target: '_blank', rel: 'noopener noreferrer' }, t('open_github'))));
  // ④ Full canvas + floating panels: what the README promises (both inputs and their expected outputs) fills the
  // canvas; identity, lock state, environment and the last run float in its corners. Forms follow below.
  const caseRuns = (state.runs || []).filter(r => r.case_id === c.case_id);
  const last = caseRuns[0];
  const goTo = elId => () => document.getElementById(elId)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const panel = (pos, k, ...body) => h('div', { class: `float ${pos}` }, h('span', { class: 'k' }, k), ...body);
  const io = (c.fixtures || []).map(f => h('div', { class: 'io' },
    h('div', {}, h('div', { class: 'label' }, `${t('input')} · `, h('span', { class: 'mono' }, f.name)), h('pre', {}, f.input),
      // Files the command reads from its working folder are part of what the reviewer approves.
      ...Object.entries(f.cwd_files || {}).map(([name, text]) => [
        h('div', { class: 'label' }, `${t('cwd_file')} · `, h('span', { class: 'mono' }, name)), h('pre', {}, text)])),
    h('span', { class: 'arrow', 'aria-hidden': 'true' }, '→'),
    h('div', {}, h('div', { class: 'label' }, `${t('expected')} · `, h('span', { class: 'mono' }, f.validator)), fixtureExpected(f))));
  const canvas = h('section', { class: 'canvas', 'aria-label': t('case_promise') },
    panel('tl', t('readme_source'), h('span', { class: 'mono small' }, c.readme_source), h('a', { class: 'small', href: commitUrl, target: '_blank', rel: 'noopener noreferrer' }, t('open_github'))),
    panel('tr', t('panel_lock'), reviewBadge(c),
      isLocked(c) ? h('span', { class: 'small' }, `${c.review.reviewed_by} · ${when(c.review.approved_at)}`) : null,
      h('button', { type: 'button', class: 'btn secondary small', onclick: goTo('case-approval') }, isLocked(c) ? t('panel_lock_view') : t('panel_lock_go'))),
    h('h2', {}, t('case_promise')),
    h('p', { style: 'margin:0; font-size:16px; max-width:70ch' }, c.goal),
    h('div', { class: 'io-grid' }, io),
    h('p', { class: 'small muted', style: 'margin:0' }, `${t('acceptance_hash')}: `, h('span', { class: 'mono', style: 'overflow-wrap:anywhere' }, c.acceptance_sha256)),
    panel('bl', t('environment'), h('span', { class: 'mono small' }, `${c.environment?.platform ?? '—'} · ${c.environment?.image ?? '—'}`)),
    panel('br', t('panel_last'), last ? h('a', { href: `#/runs/${encodeURIComponent(last.run_id)}` }, badge(last.result_status || last.run_state, lbl('result', last.result_status))) : h('span', { class: 'small muted' }, t('case_never')),
      last ? h('span', { class: 'small muted' }, when(last.created_at)) : null));
  const below = h('div', { class: 'grid-main' },
    h('div', { class: 'stack', id: 'case-approval' }, approvalCard(c), state.health?.nightly_checks ? null : card(h('h2', {}, t('nightly_title')), h('p', { class: 'small muted', style: 'margin:0' }, t('nightly_off')), nightlyLink())),
    h('div', { class: 'stack' }, runCard(c), card(h('h2', {}, t('case_runs')), caseRuns.length ? h('div', { class: 'klist' }, caseRuns.map(r => h('a', { class: 'kcard', href: `#/runs/${encodeURIComponent(r.run_id)}` },
      h('span', { class: 'row2' }, badge(r.result_status || r.run_state, lbl('result', r.result_status)), r.repair_mode ? badge(r.repair_mode, lbl('repair', r.repair_mode)) : null),
      h('span', { class: 'small muted' }, `${short(r.run_id)} · ${r.dependency_mode ? lbl('dep', r.dependency_mode) : ''} · ${when(r.created_at)}`)))) : h('p', { class: 'empty' }, t('case_runs_empty')))));
  return [crumb, head, canvas, below];
}

function approvalCard(c) {
  const head = [h('h2', {}, t('s2')), h('p', { class: 'small muted', style: 'margin:0' }, t('s2_hint'))];
  const a = approvalOf(c.case_id);
  if (isLocked(c)) {
    return card(...head, h('div', { class: 'locked', role: a.justLocked ? 'status' : null },
      h('div', { class: 'title' }, '✓ ', t('locked')),
      kv([[t('locked_by'), c.review.reviewed_by], [t('locked_at'), when(c.review.approved_at)], [t('locked_hash'), h('span', { class: 'mono' }, c.review.acceptance_sha256)],
        c.review.review_revision && [t('hist_revision_k'), t('hist_rev', { n: c.review.review_revision })],
        c.review.correction && [t('hist_corrected_k'), `${c.review.correction.corrected_by} · ${when(c.review.correction.corrected_at)}`]]),
      h('p', { class: 'small', style: 'margin:0' }, t('locked_note'))),
    a.justLocked ? h('div', { class: 'notice info' }, h('p', {}, t('approve_done_hint'))) : null,
    state.health?.approval_history ? historyBlock(c) : null);
  }
  const dev = c.review?.status === 'development_reviewed' || c.review?.status === 'draft'
    ? h('div', { class: 'notice warn' }, h('p', {}, t('dev_note', { who: c.review.reviewed_by || '—' }))) : null;
  if (readOnly()) return card(...head, dev, h('div', { class: 'notice neutral' }, h('p', {}, t('demo_no_approve'))));
  return card(...head, dev,
    h('form', { class: 'stack', onsubmit: e => { e.preventDefault(); approve(c); } },
      h('div', {}, h('label', { class: 'field', for: 'reviewer' }, t('reviewer')),
        h('input', { id: 'reviewer', type: 'text', autocomplete: 'name', placeholder: t('reviewer_ph'), value: a.name, oninput: e => { a.name = e.target.value; } })),
      h('label', { class: 'check', for: 'seen' },
        h('input', { id: 'seen', type: 'checkbox', checked: a.seen, onchange: e => { a.seen = e.target.checked; } }), h('span', {}, t('seen'))),
      h('div', {}, h('button', { class: 'btn', type: 'submit', disabled: a.sending || apiDown() }, a.sending ? t('approving') : t('approve'))),
      apiDown() ? h('div', { class: 'notice bad' }, h('p', {}, t('approve_need_api'))) : null,
      a.error ? (a.error.code === 'client' ? h('div', { class: 'notice warn' }, h('p', {}, a.error.message)) : errorBox(a.error)) : null,
      a.error?.code === 'acceptance_changed' || a.error?.code === 'approval_exists' ? h('div', {}, h('button', { class: 'btn secondary small', type: 'button', onclick: loadCases }, t('reload_cases'))) : null));
}
async function approve(c) {
  const a = approvalOf(c.case_id);
  if (!a.name.trim() || !a.seen) { a.error = { code: 'client', message: t('need_name') }; return render(); }
  a.sending = true; a.error = null; render();
  try {
    const updated = await api.approve(c.case_id, c.acceptance_sha256, a.name.trim());
    // Only the server's reply decides whether the case is locked.
    if (updated?.case_id) state.cases = state.cases.map(x => (x.case_id === updated.case_id ? updated : x));
    else await loadCases();
    Object.assign(a, { name: '', seen: false, error: null, justLocked: isLocked(caseById(c.case_id)) });
  } catch (e) { a.error = e; }
  a.sending = false; render();
}

// Approval history (append-only on the server) and the explicit reviewer-name correction.
// The normal approve button above keeps using POST /approval; corrections go through their own endpoint.
const EVENT_CLASS = { approved: 'approved', legacy_snapshot: 'neutral', correction: 'warn', imported: 'info' };
async function loadHistory(caseId) {
  const hs = historyOf(caseId);
  hs.loading = true; hs.error = null; softRender();
  try { const { items } = await api.approvalHistory(caseId); hs.items = items || []; } catch (e) { hs.error = e; }
  hs.loading = false; hs.loaded = true; softRender();
}
const latestRevision = hs => (hs.items?.length ? Math.max(...hs.items.map(i => i.revision)) : null);
function historyBlock(c) {
  const hs = historyOf(c.case_id);
  if (!hs.loaded && !hs.loading && !apiDown()) queueMicrotask(() => loadHistory(c.case_id));
  const items = hs.items || [];
  const current = latestRevision(hs);
  const list = items.length ? h('ol', { class: 'history' }, items.map(i => {
    const r = i.review || {};
    return h('li', {},
      h('div', { class: 'result-line' }, badge(EVENT_CLASS[i.event_type] || 'neutral', lbl('event', i.event_type)),
        h('span', { class: 'small' }, t('hist_rev', { n: i.revision })), i.revision === current ? badge('approved', t('hist_current')) : null),
      kv([[t('locked_by'), r.reviewed_by || '—'], [t('locked_at'), when(r.approved_at)], [t('hist_recorded'), when(i.recorded_at)],
        r.correction && [t('hist_corrected_by'), r.correction.corrected_by],
        r.correction && [t('hist_reason'), r.correction.reason],
        r.correction?.supersedes_revision && [t('hist_supersedes_k'), t('hist_rev', { n: r.correction.supersedes_revision })]]));
  })) : null;
  const body = h('div', { class: 'stack', style: 'gap:10px' },
    hs.error ? errorBox(hs.error) : null,
    hs.loading && !items.length ? h('p', { class: 'empty' }, t('loading')) : null,
    hs.loaded && !hs.error && !items.length ? h('p', { class: 'empty' }, t('hist_empty')) : null,
    list,
    items.some(i => i.event_type === 'legacy_snapshot') ? h('div', { class: 'notice neutral' }, h('p', {}, t('hist_legacy_note'))) : null,
    h('div', { class: 'row' }, h('button', { class: 'btn secondary small', type: 'button', disabled: hs.loading || apiDown(), onclick: () => loadHistory(c.case_id) }, t('hist_reload'))),
    state.health?.approval_corrections ? correctionBlock(c, hs) : null);
  return foldable(`hist-${c.case_id}`, `${t('hist_title')}${items.length ? ` · ${items.length}` : ''}`, body);
}
function correctionBlock(c, hs) {
  const f = correctOf(c.case_id);
  const base = latestRevision(hs);
  if (!f.open) {
    return h('div', { class: 'stack', style: 'gap:6px' },
      f.done ? h('div', { class: 'notice info', role: 'status' }, h('p', {}, t('corr_done'))) : null,
      h('div', {}, h('button', { class: 'btn secondary small', type: 'button', disabled: apiDown() || base == null, onclick: () => { Object.assign(f, { open: true, done: false, error: null }); render(); } }, t('corr_open'))));
  }
  const field = (id, label, key, props = {}) => h('div', {}, h('label', { class: 'field', for: id }, label),
    h(props.multiline ? 'textarea' : 'input', { id, type: props.multiline ? null : 'text', rows: props.multiline ? 3 : null, maxlength: props.max, placeholder: props.ph, value: f[key], oninput: e => { f[key] = e.target.value; } }));
  return h('form', { class: 'stack correction', onsubmit: e => { e.preventDefault(); submitCorrection(c); } },
    h('h3', {}, t('corr_open')),
    h('p', { class: 'small muted', style: 'margin:0' }, t('corr_intro')),
    kv([[t('locked_by'), c.review.reviewed_by], [t('corr_base_k'), base != null ? t('hist_rev', { n: base }) : '—']]),
    field(`corr-name-${c.case_id}`, t('corr_new'), 'name', { max: 100 }),
    field(`corr-by-${c.case_id}`, t('corr_by'), 'by', { max: 100 }),
    field(`corr-reason-${c.case_id}`, t('corr_reason'), 'reason', { max: 1000, ph: t('corr_reason_ph'), multiline: true }),
    h('label', { class: 'check', for: `corr-ok-${c.case_id}` }, h('input', { id: `corr-ok-${c.case_id}`, type: 'checkbox', checked: f.ok, onchange: e => { f.ok = e.target.checked; } }), h('span', {}, t('corr_confirm'))),
    h('div', { class: 'row' },
      h('button', { class: 'btn', type: 'submit', disabled: f.sending || apiDown() }, f.sending ? t('submitting') : t('corr_submit')),
      h('button', { class: 'btn secondary', type: 'button', disabled: f.sending, onclick: () => { Object.assign(f, { open: false, error: null }); render(); } }, t('corr_cancel'))),
    f.error ? (f.error.code === 'client' ? h('div', { class: 'notice warn' }, h('p', {}, f.error.message)) : errorBox(f.error)) : null,
    f.error?.code === 'approval_changed' || f.error?.code === 'acceptance_changed' ? h('div', {}, h('button', { class: 'btn secondary small', type: 'button', onclick: () => { f.error = null; loadCases(); loadHistory(c.case_id); } }, t('hist_reload'))) : null);
}
async function submitCorrection(c) {
  const f = correctOf(c.case_id); const hs = historyOf(c.case_id);
  const name = f.name.trim(); const by = f.by.trim(); const reason = f.reason.trim();
  const base = latestRevision(hs);
  if (!name || !by || !reason || !f.ok) { f.error = { code: 'client', message: t('corr_need') }; return render(); }
  if (name === c.review.reviewed_by) { f.error = { code: 'client', message: t('corr_same') }; return render(); }
  if (base == null) { f.error = { code: 'client', message: t('corr_need_history') }; return render(); }
  f.sending = true; f.error = null; render();
  try {
    const updated = await api.correctApproval(c.case_id, { acceptance_sha256: c.acceptance_sha256, reviewed_by: name, corrected_by: by, reason, expected_revision: base });
    if (updated?.case_id) state.cases = state.cases.map(x => (x.case_id === updated.case_id ? updated : x));
    else await loadCases();
    Object.assign(f, { open: false, name: '', by: '', reason: '', ok: false, done: true });
    loadHistory(c.case_id);
  } catch (e) { f.error = e; }
  f.sending = false; render();
}

function runCard(c) {
  const f = formOf(c.case_id); const hl = state.health || {};
  const ownerPlan = hl.model_trial_plan;
  const modelAvailable = !!(hl.model_calls_enabled && ownerPlan?.available && ownerPlan.case_id === c.case_id && ownerPlan.acceptance_sha256 === c.acceptance_sha256);
  if (!c.manual_candidate_available && f.repair === 'manual_candidate') f.repair = 'none';
  // A plan that was just consumed (or switched off) must not stay selected: the next submit would be refused.
  if (!modelAvailable && f.repair === 'model') f.repair = 'none';
  const planForCase = ownerPlan && ownerPlan.case_id === c.case_id;
  const planStatus = !planForCase ? null : ownerPlan.consumed ? t('model_plan_used')
    : !hl.model_calls_enabled ? t('model_plan_off') : ownerPlan.acceptance_sha256 !== c.acceptance_sha256 ? t('model_plan_other_check') : null;
  const modes = hl.dependency_modes || ['offline_snapshot'];
  if (f.dep === 'pypi_live' && !hl.live_pypi_enabled) f.dep = 'offline_snapshot';
  if (readOnly()) return card(h('h2', {}, t('s3')), h('div', { class: 'notice neutral' }, h('p', {}, t('demo_no_run'))));
  return card(h('h2', {}, t('s3')), h('p', { class: 'small muted', style: 'margin:0' }, t('s3_hint')),
    state.health && !state.health.execution_enabled ? h('div', { class: 'notice warn' }, h('p', {}, t('runner_off_notice'))) : null,
    !isLocked(c) && c.review?.status !== 'development_reviewed' ? h('div', { class: 'notice warn' }, h('p', {}, t('not_locked_notice'))) : null,
    h('form', { class: 'stack', onsubmit: e => { e.preventDefault(); submitRun(c); } },
      h('div', {}, h('label', { class: 'field', for: 'level' }, t('level')),
        h('select', { id: 'level', disabled: f.repair === 'model', onchange: e => { f.level = e.target.value; } }, ['functional', 'startup'].map(v => h('option', { value: v, selected: f.level === v }, lbl('level', v))))),
      h('div', {}, h('label', { class: 'field', for: 'repair' }, t('repair')),
        h('select', { id: 'repair', onchange: e => { f.repair = e.target.value; if (f.repair === 'model') { f.dep = ownerPlan.dependency_mode; f.level = 'functional'; } render(); } },
          h('option', { value: 'none', selected: f.repair === 'none' }, t('repair_none_opt')),
          h('option', { value: 'manual_candidate', selected: f.repair === 'manual_candidate', disabled: !c.manual_candidate_available },
            c.manual_candidate_available ? t('repair_manual_opt') : t('repair_manual_na')),
          h('option', { value: 'model', selected: f.repair === 'model', disabled: !modelAvailable }, t('repair_model_opt')))),
      planStatus ? h('div', { class: 'notice neutral' }, h('p', {}, planStatus)) : null,
      f.repair === 'model' && modelAvailable ? h('div', { class: 'notice info' }, h('p', {}, t('model_plan_hint', {
        amount: ownerPlan.budget_usd, rounds: ownerPlan.max_patch_rounds, calls: ownerPlan.max_model_requests
      }))) : null,
      h('div', {}, h('label', { class: 'field', for: 'dep' }, t('dependency_mode')),
        h('select', { id: 'dep', disabled: f.repair === 'model', 'aria-describedby': 'dep-hint', onchange: e => { f.dep = e.target.value; } },
          modes.map(m => h('option', { value: m, selected: f.dep === m, disabled: m === 'pypi_live' && !hl.live_pypi_enabled },
            m === 'pypi_live' && !hl.live_pypi_enabled ? t('dep_live_na') : lbl('dep', m)))),
        h('p', { class: 'small muted', id: 'dep-hint', style: 'margin:4px 0 0' }, t('dep_hint'))),
      h('div', {}, h('button', { class: 'btn', type: 'submit', disabled: f.sending || (f.repair === 'model' && !modelAvailable) }, f.sending ? t('submitting') : t('submit'))),
      f.error ? errorBox(f.error) : null));
}
async function submitRun(c) {
  const f = formOf(c.case_id);
  f.sending = true; f.error = null; render();
  try {
    const created = await api.startRun({ case_id: c.case_id, verification_level: f.level, repair_mode: f.repair, dependency_mode: f.dep });
    f.sending = false; loadHealth(); loadRuns(); go(`#/runs/${encodeURIComponent(created.run_id)}`);
    return;
  } catch (e) { f.error = e; }
  f.sending = false; render();
}

/* ------------------------------------------------------------------ runs list */
function runsTable(runs, { hideCase = false } = {}) {
  if (state.runsError) return errorBox(state.runsError);
  if (!state.runs) return h('p', { class: 'empty' }, t('loading'));
  if (!runs.length) return h('p', { class: 'empty' }, t('history_empty'));
  return h('div', { class: 'table-scroll' }, h('table', {},
    h('thead', {}, h('tr', {}, h('th', {}, t('run_id')), hideCase ? null : h('th', {}, t('run_case')), h('th', {}, t('run_state')), h('th', {}, t('result_status')), h('th', {}, t('dependency_mode')), h('th', {}, t('created')))),
    h('tbody', {}, runs.map(r => h('tr', {},
      h('td', { class: 'mono small' }, h('a', { href: `#/runs/${encodeURIComponent(r.run_id)}` }, short(r.run_id))),
      hideCase ? null : h('td', { class: 'mono small' }, r.case_id && r.case_available !== false ? h('a', { href: `#/cases/${encodeURIComponent(r.case_id)}` }, r.case_id) : h('span', {}, r.case_id || repoName(r.repo_url) || '—')),
      h('td', {}, badge(r.run_state, lbl('state', r.run_state))),
      h('td', {}, badge(r.result_status || 'none', lbl('result', r.result_status))),
      h('td', { class: 'small' }, r.dependency_mode ? lbl('dep', r.dependency_mode) : h('span', { class: 'muted' }, t('not_recorded'))),
      h('td', { class: 'small muted' }, when(r.created_at)))))));
}
// ⑤ Kanban columns by result: where a run sits says what happened to it. ⑥ Swimlanes on a time axis: one lane per
// case, one bar per run, so who ran when and for how long reads in one glance. The table stays as the third view.
const RUN_COLUMNS = [
  ['active', r => r.run_state === 'queued' || r.run_state === 'running'],
  ['already_passes', r => r.result_status === 'already_passes'],
  ['verified_fix', r => r.result_status === 'verified_fix'],
  ['needs_review', r => r.result_status === 'needs_review' || r.result_status === 'needs_input'],
  ['unresolved', r => r.result_status === 'unresolved'],
  ['other', () => true],
];
const SEG_CLASS = { verified_fix: 'r-ok', already_passes: 'r-ok', unresolved: 'r-bad', needs_review: 'r-warn', needs_input: 'r-warn', infrastructure_error: 'r-infra' };
function runsBoard(runs) {
  const cols = RUN_COLUMNS.map(([key]) => [key, []]);
  for (const r of runs) cols[RUN_COLUMNS.findIndex(([, test]) => test(r))][1].push(r);
  const shown = cols.filter(([key, list]) => list.length || ['already_passes', 'verified_fix', 'needs_review', 'unresolved'].includes(key));
  return h('div', { class: 'board' }, shown.map(([key, list]) => h('section', { class: 'col', 'aria-label': t(`col_${key}`) },
    h('div', { class: 'col-head' }, h('span', {}, t(`col_${key}`)), badge('neutral', String(list.length))),
    list.length ? list.map(r => h('a', { class: 'kcard', href: `#/runs/${encodeURIComponent(r.run_id)}` },
      h('strong', { class: 'mono small' }, r.case_id || repoName(r.repo_url) || '—'),
      h('span', { class: 'row2' }, r.repair_mode ? badge(r.repair_mode, lbl('repair', r.repair_mode)) : null, r.dependency_mode ? badge('neutral', lbl('dep', r.dependency_mode)) : null),
      h('span', { class: 'small muted' }, `${short(r.run_id)} · ${when(r.created_at)}`))) : h('p', { class: 'empty small' }, t('col_empty')))));
}
// Bars that would overlap (runs seconds apart) go on extra rows inside the lane, so no run is hidden behind another.
function laneTrack(id, list, { at, until, pos, showNow, now }) {
  const MIN = 1.2; // percent of the track a bar occupies at least (its minimum drawn width plus a gap)
  const rows = []; const placed = [...list].sort((a, b) => at(a) - at(b)).map(r => {
    const left = pos(at(r)); const right = Math.max(pos(until(r)), left + MIN);
    let row = rows.findIndex(end => end <= left);
    if (row === -1) { row = rows.length; rows.push(0); }
    rows[row] = right;
    return { r, left, width: Math.max(pos(until(r)) - left, 0), row };
  });
  return h('div', { class: 'track', style: `height:${22 + rows.length * 24}px` },
    showNow ? h('span', { class: 'nowline', style: `left:${pos(now)}%`, title: t('lanes_now') }) : null,
    placed.map(({ r, left, width, row }) => {
      const label = `${id} · ${lbl('result', r.result_status) || lbl('state', r.run_state)} · ${when(r.started_at || r.created_at)}${r.finished_at ? ` – ${when(r.finished_at)}` : ''}`;
      return h('a', { class: `lane-bar ${SEG_CLASS[r.result_status] || (r.run_state === 'running' || r.run_state === 'queued' ? 'r-info' : 'r-neutral')}`,
        href: `#/runs/${encodeURIComponent(r.run_id)}`, title: label, 'aria-label': label,
        style: `left:${left}%; width:${width}%; top:${11 + row * 24}px` });
    }));
}
function runsLanes(runs) {
  const at = r => Date.parse(r.started_at || r.created_at);
  const until = r => Date.parse(r.finished_at) || (r.run_state === 'running' || r.run_state === 'queued' ? Date.now() : at(r));
  const timed = runs.filter(r => Number.isFinite(at(r)));
  if (!timed.length) return h('p', { class: 'empty' }, t('history_empty'));
  let lo = Math.min(...timed.map(at)); let hi = Math.max(...timed.map(until));
  const pad = Math.max((hi - lo) * 0.04, 60_000); lo -= pad; hi += pad;
  const pos = ms => ((ms - lo) / (hi - lo)) * 100;
  const now = Date.now(); const showNow = now >= lo && now <= hi;
  const caseIds = [...new Set([...(state.cases || []).map(c => c.case_id), ...timed.map(r => r.case_id)])].filter(id => timed.some(r => r.case_id === id));
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => lo + f * (hi - lo));
  return h('div', { class: 'stack', style: 'gap:8px' },
    h('div', { class: 'axis', 'aria-hidden': 'true' }, ticks.map(ms => h('span', { style: `left:${pos(ms)}%` }, when(new Date(ms).toISOString()).replace(/:\d\d (BST|GMT)$/, ' $1')))),
    h('div', { class: 'lanes', role: 'list' }, caseIds.map(id => h('div', { class: 'lane', role: 'listitem' },
      id && timed.some(r => r.case_id === id && r.case_available !== false) ? h('a', { class: 'lane-name mono', href: `#/cases/${encodeURIComponent(id)}` }, id) : h('span', { class: 'lane-name mono' }, id || repoName(timed.find(r => r.case_id === id)?.repo_url) || '—'),
      laneTrack(id, timed.filter(r => r.case_id === id), { at, until, pos, showNow, now })))),
    h('div', { class: 'legend' }, [['r-ok', 'legend_ok'], ['r-warn', 'legend_review'], ['r-bad', 'legend_bad'], ['r-infra', 'legend_infra'], ['r-info', 'legend_running']].map(([cls, key]) =>
      h('span', {}, h('i', { class: `lane-bar ${cls}` }), t(key)))),
    h('p', { class: 'small muted', style: 'margin:0' }, t('lanes_note')));
}
function viewRuns() {
  let view = state.runsView || params.get('view'); // ?view=board|lanes|table in a shared link
  if (!view) { try { view = localStorage.getItem('fe-runs-view'); } catch { /* storage unavailable */ } }
  if (!['board', 'lanes', 'table'].includes(view)) view = 'board';
  state.runsView = view;
  const set = v => () => { state.runsView = v; try { localStorage.setItem('fe-runs-view', v); } catch { /* storage unavailable */ } render(); };
  const toggle = h('div', { class: 'seg', role: 'group', 'aria-label': t('runs_view') },
    ['board', 'lanes', 'table'].map(v => h('button', { type: 'button', 'aria-pressed': String(view === v), onclick: set(v) }, t(`runs_view_${v}`))));
  const runs = state.runs || [];
  const body = state.runsError ? errorBox(state.runsError) : !state.runs ? h('p', { class: 'empty' }, t('loading')) : !runs.length ? h('p', { class: 'empty' }, t('history_empty'))
    : view === 'board' ? runsBoard(runs) : view === 'lanes' ? card(runsLanes(runs)) : card(runsTable(runs));
  return [h('div', { class: 'stack', style: 'gap:6px' }, h('h1', {}, t('runs_title')), h('p', { class: 'lead' }, t('runs_hint')), h('div', {}, toggle)), body];
}

/* ------------------------------------------------------------------ run report */
let pollTimer = null; let pollAbort = null;
function stopPolling() { clearTimeout(pollTimer); pollTimer = null; pollAbort?.abort(); pollAbort = null; }
function openRun(id) { stopPolling(); state.run = { id, data: null, error: null, patch: null, pollError: null }; pollRun(id); }
async function pollRun(id) {
  stopPolling();
  const r = state.run;
  pollAbort = new AbortController();
  try {
    const run = await api.run(id, pollAbort.signal);
    if (state.run.id !== id) return;
    r.data = run; r.pollError = null; r.error = null;
    if (run.run_state === 'queued' || run.run_state === 'running') pollTimer = setTimeout(() => pollRun(id), POLL_MS);
    else loadRuns();
    if (run.patch_available && !r.patch) loadPatch(id);
  } catch (e) {
    if (e.name === 'AbortError' || state.run.id !== id) return;
    if (!r.data) r.error = e; else { r.pollError = e; pollTimer = setTimeout(() => pollRun(id), POLL_MS * 2); }
  }
  if ((route().page === 'runs' || route().page === 'answer') && route().id === id) softRender();
}
async function loadPatch(id) {
  state.run.patch = { text: null, error: null };
  try { state.run.patch.text = await api.patch(id); } catch (e) { state.run.patch.error = e; }
  if (state.run.id === id) softRender();
}

function viewRun(id) {
  const crumb = h('div', { class: 'crumb' }, h('a', { href: '#/dashboard' }, t('crumb_overview')), '›', h('a', { href: '#/runs' }, t('nav_runs')), '›', h('span', { class: 'mono' }, short(id)));
  const r = state.run;
  if (r.error) return [crumb, r.error.code === 'run_not_found' ? card(h('h1', { class: 'empty' }, t('run_missing'))) : errorBox(r.error)];
  if (!r.data) return [crumb, h('p', { class: 'empty' }, t('loading'))];
  return [crumb, r.pollError ? h('div', { class: 'notice warn' }, h('p', {}, t('poll_error'))) : null, ...reportView(r.data, { patch: r.patch })];
}

// The AI repair path at a glance (WIN-6): one step per stage, taken only from this run's record, including exactly
// where and why it stopped. Runs without a model diagnosis don't show it.
function aiPath(run) {
  const ev = run.evidence || {}; const d = ev.diagnosis || run.source_model_diagnosis;
  if (!d) return null;
  const rounds = Array.isArray(ev.repair_rounds) ? ev.repair_rounds : [];
  const { attempt, modelOf, verifiedByAi, humanRejected } = repairPathFacts(run);
  // Model names come from the record (the call ledger or the diagnosis), never from the route: group D uses Ultra twice.
  const steps = [];
  const original = attempt('original_readme');
  steps.push([original ? (original.status === 'passed' ? 'ok' : 'bad') : 'skip', t('path_readme'), original ? lbl('attempt', original.status) : t('path_not_run')]);
  const cat = d.classification?.category;
  const classifier = modelOf('classify', d.classification_model);
  steps.push([cat ? 'ok' : 'skip', classifier ? t('path_classify', { m: classifier }) : t('path_classify_any'), cat ? t('path_category', { c: cat }) : t('path_not_run')]);
  // Tavily lookups (WIN-11) sit between classification and diagnosis; only real recorded searches are shown.
  for (const w of (Array.isArray(d.web_research) ? d.web_research : [])) {
    const hosts = [...new Set((w.results || []).map(r => { try { return new URL(r.url).hostname; } catch { return null; } }).filter(Boolean))];
    steps.push([w.status === 'ok' && hosts.length ? 'ok' : 'stop', t('path_research'),
      w.status === 'ok' ? (hosts.length ? t('path_research_found', { n: w.results.length, h: hosts.join(', ') }) : t('path_research_none'))
        : t('path_research_unavailable', { r: w.reason || w.status })]);
  }
  const explanation = d.explanation || rounds.map(r => r.diagnosis?.explanation).find(Boolean);
  const first = explanation ? explanation.split(/(?<=\.)\s/)[0] : '';
  const diagnosis = explanation ? ['ok', first.length > 160 ? `${first.slice(0, 159).trimEnd()}…` : first]
    : d.reason === 'budget_exhausted' ? ['stop', t('path_budget')]
      : d.reason === 'incomplete_output' ? ['stop', t('path_incomplete')]
        : d.reason ? ['stop', stopText(d.reason)]
          : cat ? ['stop', t('path_hint_only')] : ['skip', t('path_not_reached')];
  // A model that was never called isn't named: the budget guard or a hint-only classification stopped before it.
  const diagnoser = modelOf('diagnose', d.diagnostic_model);
  steps.push([diagnosis[0], diagnoser ? t('path_diagnose', { m: diagnoser }) : t('path_diagnose_any'), diagnosis[1]]);
  if (explanation || rounds.some(r => r.candidate_patch)) {
    const state = rounds.some(r => r.status === 'verified_fix') ? 'ok' : rounds.some(r => r.status === 'rejected' || r.status === 'validation_failed') ? 'bad' : 'stop';
    // Records from before repair rounds existed (e5150fec) keep the proposal's outcome only in the run's reason.
    const legacyRejected = !rounds.length && /rejected/i.test(run.reason || '');
    steps.push([rounds.length ? state : legacyRejected ? 'bad' : 'stop', t('path_propose'), rounds.length
      ? rounds.map(r => `${t('path_round', { n: r.round, s: lbl('round', r.status) })}${r.reason ? ` (${refusalPlain(r.reason)})` : r.status === 'no_candidate' ? ` (${t('path_no_candidate')})` : ''}`).join(' · ')
      : run.reason || t('path_no_candidate')]);
  }
  for (const [phase, key] of [['candidate_patch', 'path_fresh'], ['fresh_verification', 'path_again']]) {
    const a = attempt(phase);
    steps.push([a ? (a.status === 'passed' ? 'ok' : 'bad') : 'skip', t(key), a ? lbl('attempt', a.status) : t('path_not_reached')]);
  }
  // A test double or labelled example never reads as a real AI fix on the card itself.
  const notReal = run.data_origin === 'example' || run.data_origin === 'engineering_test' || ev.data_origin === 'engineering_test' || !!ev.environment?.test_double;
  const sym = { ok: '✓', bad: '✗', stop: '■', skip: '–' };
  return h('section', { class: 'card ai-path', 'aria-labelledby': `path-${run.run_id || 'x'}` },
    h('div', { class: 'card-head' }, h('h2', { id: `path-${run.run_id || 'x'}` }, t('path_title')),
      h('span', { class: 'row' }, humanRejected ? badge('failed', t('review_rejected_cell')) : verifiedByAi ? badge('verified_fix', t('path_verified_ai')) : badge(run.result_status || 'none', lbl('result', run.result_status)),
        notReal ? badge('example', run.data_origin === 'example' ? t('example_badge') : t('test_double')) : null)),
    h('ol', { class: 'path' }, steps.map(([state, title, detail], i) => h('li', { class: `p-${state}` },
      h('span', { class: 'm', 'aria-hidden': 'true' }, sym[state]),
      h('span', { class: 'sr-only' }, `${t(`path_state_${state}`)}: `),
      h('strong', {}, `${i + 1}. ${title}`), h('span', { class: 'd' }, detail)))),
    h('p', { class: 'small muted', style: 'margin:0' }, t('path_note')));
}

// Shared by real runs and the labelled examples on the state preview page.
// A note the maintainer could be sent, drafted by Nemotron from this run's evidence (cli: maintainer-note). The model
// writes only the title and body; the verification sentence and the diff come from the record. Never sent from here.
function maintainerNote(run) {
  const n = run.maintainer_note;
  if (!n || n.status !== 'draft' || n.posted !== false || typeof n.title !== 'string' || typeof n.body !== 'string') return null;
  const name = /Nemotron-3-(Nano|Super|Ultra)/i.exec(n.model || '');
  const model = name ? `Nemotron 3 ${name[1][0].toUpperCase()}${name[1].slice(1).toLowerCase()}` : n.model;
  const text = `${n.title}\n\n${n.body}\n\n${n.footer || ''}`.trim();
  const copied = h('span', { class: 'small muted', role: 'status' });
  const copy = () => navigator.clipboard?.writeText(`${text}\n\n${n.diff || ''}`.trim()).then(() => { copied.textContent = t('note_copied'); }, () => {});
  return card(h('div', { class: 'section-head' }, h('h2', {}, t('note_title')), badge('info', t('note_badge', { model }))),
    h('p', { class: 'small muted' }, t('note_explain')),
    h('div', { class: 'note-draft' }, h('p', {}, h('strong', {}, n.title)), ...n.body.split(/\n{2,}/).map(p => h('p', {}, p)),
      n.footer ? h('p', { class: 'small muted' }, n.footer) : null),
    h('div', { class: 'b-actions' }, h('button', { type: 'button', class: 'btn secondary', onclick: copy }, t('note_copy')), copied));
}

function reportView(run, { patch = null } = {}) {
  const ev = run.evidence || {};
  const isExample = run.data_origin === 'example';
  const isEngineering = run.data_origin === 'engineering_test' || ev.data_origin === 'engineering_test' || !!ev.environment?.test_double;
  const result = run.result_status ?? null;
  const active = run.run_state === 'queued' || run.run_state === 'running';
  const finished = run.run_state === 'completed' || run.run_state === 'failed';
  const dep = ev.dependency_mode || run.dependency_mode;
  const costs = run.costs || {};
  const key = run.run_id || 'example';

  const header = h('section', { class: `card result-head r-${result || 'none'}` },
    // The visible title is the result badge; screen readers also get a real page heading.
    h('h1', { class: 'sr-only' }, t('report_heading', { c: run.case_id || repoName(run.repo_url) || '—', r: lbl('result', result) })),
    h('div', { class: 'result-line' },
      badge(result || 'none', lbl('result', result), null, 'lg'),
      badge(run.run_state || 'none', lbl('state', run.run_state)),
      run.repair_mode ? badge(run.repair_mode, lbl('repair', run.repair_mode)) : null,
      isExample ? badge('example', t('example_badge')) : null,
      isEngineering ? badge('example', t('test_double')) : null,
      run.data_origin === 'recorded_replay' ? badge('info', t('replay_badge')) : null,
      active ? h('span', { class: 'live' }, `${lbl('phase', run.phase || ev.current_phase)} · ${t('run_live')}`) : null),
    h('p', { style: 'margin:0; font-size:16px' }, lbl('resultDesc', result)),
    run.run_state === 'completed' && result !== 'already_passes' && result !== 'verified_fix' ? h('p', { class: 'small muted', style: 'margin:0' }, t('completed_note')) : null,
    isExample && run.example_notice ? h('div', { class: 'notice neutral' }, h('p', {}, run.example_notice)) : null,
    isEngineering ? h('div', { class: 'notice neutral' }, h('p', {}, t('engineering_run_note'))) : null,
    isExample && run.fixture_notice ? h('div', { class: 'notice neutral' }, h('p', {}, run.fixture_notice)) : null,
    run.data_origin === 'recorded_replay' ? h('div', { class: 'notice info' }, h('p', {}, t('replay_note'))) : null,
    run.recording_scope === 'boundary_baseline' ? h('div', { class: 'notice neutral' }, h('p', {}, t('boundary_baseline_note'))) : null,
    ev.candidate_provenance?.kind === 'unchanged_saved_model_candidate' ? h('div', { class: 'notice info' },
      h('p', {}, t('saved_model_candidate_note')),
      h('a', { href: `#/runs/${encodeURIComponent(ev.candidate_provenance.source_run_id)}` }, t('saved_model_candidate_source'))) : null,
    resultReviewNotice(run),
    run.reason ? h('div', { class: `notice ${result === 'infrastructure_error' ? 'neutral' : result === 'already_passes' || result === 'verified_fix' ? 'info' : 'warn'}` }, h('strong', {}, t('reason')), h('p', {}, stopText(run.reason))) : null,
    h('div', { class: 'facts' },
      run.repo_url ? h('div', {}, h('span', { class: 'k' }, 'Repo'), h('span', { class: 'mono' }, repoName(run.repo_url))) : null,
      run.case_id ? h('div', {}, h('span', { class: 'k' }, t('run_case')), isExample || run.case_available === false ? h('span', { class: 'mono' }, run.case_id) : h('a', { class: 'mono', href: `#/cases/${encodeURIComponent(run.case_id)}` }, run.case_id)) : null,
      run.commit_sha ? h('div', {}, h('span', { class: 'k' }, t('commit')), h('span', { class: 'mono' }, short(run.commit_sha))) : null,
      h('div', {}, h('span', { class: 'k' }, t('dependency_mode')), dep ? lbl('dep', dep) : h('span', { class: 'muted' }, t('not_recorded'))),
      run.verification_level ? h('div', {}, h('span', { class: 'k' }, t('level_short')), lbl('level', run.verification_level)) : null,
      run.evaluation_group ? h('div', {}, h('span', { class: 'k' }, t('eval_group_k')), lbl('group', run.evaluation_group)) : null,
      run.started_at ? h('div', {}, h('span', { class: 'k' }, t('started')), when(run.started_at)) : null,
      finished ? h('div', {}, h('span', { class: 'k' }, t('run_duration')), fmtDur(runSeconds(run))) : null));

  // Timeline: README run and its error, then the patch, then the patched and final clean runs.
  const attempts = ev.attempts || [];
  const rounds = Array.isArray(ev.repair_rounds) && ev.repair_rounds.length ? ev.repair_rounds : null;
  const items = [];
  if (rounds) {
    // One group per repair round: its diagnosis, the proposal with its diff, then that round's runs.
    // Every round starts again from the original commit; a later round never builds on an earlier one.
    const total = ev.max_patch_rounds || rounds.length;
    for (const a of attempts.filter(a => a.round == null)) items.push(attemptItem(a, key, run.verification_level));
    for (const r of rounds) {
      items.push(roundHeader(r, total));
      if (r.diagnosis) items.push(diagnosisItem(run, ev, r.diagnosis, r));
      if (r.candidate_patch) items.push(candidateItem(run, r.candidate_patch, `${key}-r${r.round}`, r));
      for (const a of attempts.filter(a => a.round === r.round)) items.push(attemptItem(a, key, run.verification_level));
    }
    for (const a of attempts.filter(a => a.round != null && !rounds.some(r => r.round === a.round))) items.push(attemptItem(a, key, run.verification_level));
  } else {
    const patchNode = patchItem(run, ev, patch);
    const diagNode = ev.diagnosis ? diagnosisItem(run, ev) : null;
    const candNode = ev.candidate_patch ? candidateItem(run, ev.candidate_patch, key) : null;
    for (const a of attempts) {
      items.push(attemptItem(a, key, run.verification_level));
      if (a.phase === 'original_readme') items.push(diagNode, candNode, patchNode);
    }
    if (!attempts.some(a => a.phase === 'original_readme')) items.push(diagNode, candNode, patchNode);
  }
  const timeline = card(h('h2', {}, t('timeline_title')),
    items.some(Boolean) ? h('ol', { class: 'timeline' }, items) : h('p', { class: 'empty' }, finished || isExample ? t('no_attempts') : t('loading')));

  const ledger = ev.diagnosis?.ledger;
  const calls = (costs.model_calls || []).length ? costs.model_calls : (ledger?.calls || []);
  // Engineering test doubles carry made-up rates; never show those as a cost.
  const testDouble = Boolean(ledger) && !Number(ledger.model_calls || 0) && Number(ledger.engineering_test_calls || 0) > 0;
  const modelUsd = ledger && !(costs.model_calls || []).length ? (ledger.calculated_usd != null ? Number(ledger.calculated_usd) : null) : costs.model_usd;
  const costCard = card(h('h2', {}, t('costs')), kv([
    [t('model_cost'), testDouble ? t('cost_test_double') : modelUsd === 0 && !calls.length ? t('no_model_calls_cost') : calls.length && modelUsd != null ? t('cost_pending', { v: money(modelUsd) }) : money(modelUsd)],
    ledger && [t('billed'), money(ledger.billed_usd)],
    [t('runner_cost'), money(costs.runner_usd)],
    [t('model_calls'), testDouble ? `0 · ${t('test_double')} ${calls.length}` : String(calls.length)],
    [t('paid_calls'), costs.paid_calls_enabled ? t('on') : t('off')],
    costs.billing_status && !ledger && [t('billing'), h('span', {}, lbl('billing', costs.billing_status), ' ', h('span', { class: 'mono small muted' }, costs.billing_status))],
  ]));
  const evidence = card(h('h2', {}, t('evidence_title')), kv([
    [t('run_id'), h('span', { class: 'mono' }, run.run_id || '—')],
    run.acceptance_sha256 && [t('acceptance_hash'), h('span', { class: 'mono' }, run.acceptance_sha256)],
    run.review?.status && [t('review'), lbl('review', run.review.status)],
    run.created_at && [t('created'), when(run.created_at)],
    run.finished_at && [t('finished'), when(run.finished_at)],
  ]));
  // ③ Main column + sticky side rail: the timeline is read top to bottom while the result, a step index,
  // costs and evidence stay in view.
  const steps = items.filter(Boolean);
  const jump = steps.length > 2 ? card(h('h2', {}, t('jump_title')), h('ol', { class: 'jump' }, steps.map(li => {
    const head = li.querySelector('.tl-head'); const dot = li.querySelector('.tl-dot');
    const kind = li.classList.contains('tl-round') ? 'round' : dot?.classList.contains('passed') ? 'passed' : dot?.classList.contains('failed') ? 'failed' : '';
    return h('li', {}, h('button', { type: 'button', class: 'linkish', onclick: () => li.scrollIntoView({ behavior: 'smooth', block: 'start' }) },
      h('span', { class: `dot ${kind}`, 'aria-hidden': 'true' }), (head?.firstChild?.textContent || head?.textContent || '').trim()));
  }))) : null;
  const glance = card(h('div', { class: 'result-line' }, badge(result || 'none', lbl('result', result))),
    h('p', { class: 'small', style: 'margin:0' }, lbl('resultDesc', result)));
  return [header, aiPath(run), maintainerNote(run), h('div', { class: 'lay-rail' }, timeline, h('aside', { class: 'rail stack', 'aria-label': t('rail_label') }, glance, jump, costCard, evidence))];
}

// Runner set-up steps (creating the venv, the workspace probe) rather than README commands.
function isEnvStep(s) {
  const argv = s.argv || [];
  return /(^|\/)python3?(\.\d+)?$/.test(argv[0] || '') && ((argv.includes('-m') && argv.includes('venv')) || argv.includes('-c'));
}
function stepTable(list, key) {
  return h('div', { class: 'table-scroll' }, h('table', {},
    h('thead', {}, h('tr', {}, h('th', {}, t('documented_argv')), h('th', {}, t('executed_argv')), h('th', {}, t('exit_code')), h('th', { class: 'num' }, t('duration')))),
    h('tbody', {}, list.map((s, i) => h('tr', {},
      h('td', { class: 'mono small' }, (s.argv || []).join(' ')),
      h('td', { class: 'mono small' }, (s.executed_argv || []).join(' '), stream(`${key}-o${i}`, t('stdout'), s.stdout, s.truncated), stream(`${key}-e${i}`, t('stderr'), s.stderr, s.truncated)),
      h('td', { class: 'mono small' }, String(s.exit_code ?? '—')),
      h('td', { class: 'mono small num' }, s.duration_s != null ? `${s.duration_s}s` : '—'))))));
}
function attemptItem(a, runKey, level) {
  // The same phase can repeat once per repair round, so the round is part of the key.
  const key = `${runKey}-${a.phase}${a.round != null ? `-r${a.round}` : ''}`;
  const res = a.result || {};
  const steps = res.steps || [];
  const outputs = res.outputs || [];
  const checks = a.validation?.checks || [];
  const envSteps = steps.filter(isEnvStep);
  const readmeSteps = steps.filter(s => !isEnvStep(s));
  const envFailed = envSteps.filter(s => s.exit_code !== 0).length;
  const dotClass = a.status === 'passed' ? 'passed' : a.status === 'failed' ? 'failed' : 'other';
  return h('li', {},
    h('span', { class: `tl-dot ${dotClass}`, 'aria-hidden': 'true' }, a.status === 'passed' ? '✓' : a.status === 'failed' ? '✕' : '·'),
    h('div', { class: 'tl-head' }, lbl('phase', a.phase), a.round != null ? h('span', { class: 'small muted' }, t('round_n', { n: a.round })) : null,
      badge(a.status || 'none', lbl('attempt', a.status))),
    h('div', { class: 'tl-body' },
      readmeSteps.length ? h('div', { class: 'stack', style: 'gap:6px' }, h('h3', {}, t('steps')), stepTable(readmeSteps, `${key}-r`)) : null,
      outputs.length ? h('div', { class: 'stack', style: 'gap:6px' }, h('h3', {}, t(level === 'startup' ? 'startup_outputs' : 'outputs')), outputs.map((o, i) => {
        const check = checks.find(c => c.name === o.name);
        return h('div', { class: 'stack', style: 'gap:4px' },
          h('div', { class: 'out-row' }, h('span', { class: 'mono' }, o.name), h('span', { class: 'mono muted' }, `exit ${o.exit_code ?? '—'}`),
            check ? badge(check.passed ? 'passed' : 'failed', lbl('attempt', check.passed ? 'passed' : 'failed')) : null,
            check ? h('span', { class: 'muted' }, checkReason(check.reason)) : null),
          o.content ? stream(`${key}-c${i}`, t('output_content'), o.content, o.truncated) : null,
          o.process?.stderr ? stream(`${key}-s${i}`, t('stderr'), o.process.stderr, o.process.truncated) : null);
      })) : null,
      !outputs.length && checks.length ? h('ul', { class: 'tight small' }, checks.map(c => h('li', {}, `${c.name}: ${c.passed ? '✓' : '✗'} ${checkReason(c.reason)}`))) : null,
      envSteps.length ? foldable(`${key}-env`, `${t('env_check')} · ${envFailed ? t('env_failed', { n: envSteps.length, f: envFailed }) : t('env_ok', { n: envSteps.length })}`,
        stepTable(envSteps, `${key}-v`), envFailed > 0) : null));
}
// Which repair round a ledger stage belongs to: classify/diagnose are round 1, classify_r2/diagnose_r2 round 2.
const stageRound = stage => { const m = /_r(\d+)$/.exec(stage || ''); return m ? Number(m[1]) : 1; };
const ROUND_CLASS = { verified_fix: 'passed', validation_failed: 'failed', rejected: 'failed', no_candidate: 'warn', testing: 'running' };
const stopText = code => (code && LABELS[lang].stop?.[code]) || refusalPlain(code);
// A patch-checker refusal in plain words when we recognise it; otherwise the text as recorded.
function refusalPlain(reason) {
  const found = refusalKey(reason);
  return found ? t(found.key, found.vars) : (LABELS[lang].stop?.[reason] || reason);
}
function refusal(reason) {
  const found = refusalKey(reason);
  return found ? [h('p', {}, t(found.key, found.vars)), h('p', { class: 'small muted' }, `${t('rej_technical')}: ${reason}`)] : [h('p', {}, reason)];
}
function roundHeader(r, total) {
  return h('li', { class: 'tl-round' },
    h('span', { class: 'tl-dot round', 'aria-hidden': 'true' }, String(r.round)),
    h('div', { class: 'tl-head' }, t('round_title', { n: r.round, total }), badge(ROUND_CLASS[r.status] || 'neutral', lbl('round', r.status))));
}
function downloadButton(run, text, suffix = '', cls = 'btn secondary small', label = t('download_patch')) {
  return h('button', { class: cls, type: 'button', onclick: () => {
    const url = URL.createObjectURL(new Blob([text], { type: 'text/x-diff' }));
    const a = h('a', { href: url, download: `${run.case_id || 'fresh-eyes'}-${(run.run_id || '').slice(0, 8)}${suffix}.patch` });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  } }, label);
}
// d is the diagnosis to show; r is its repair round (absent for records written before rounds existed).
function diagnosisItem(run, ev, d = ev.diagnosis, r = null) {
  const allCalls = d.ledger?.calls || [];
  // The ledger is cumulative across rounds; each round shows only its own calls.
  const calls = r ? allCalls.filter(c => stageRound(c.stage) === r.round) : allCalls;
  // Model IDs come from the record, never from the route: in group D both stages are Ultra.
  const classifyModel = d.classification_model || calls.find(c => /^classify/.test(c.stage))?.returned_model;
  const diagnoseModel = d.diagnostic_model || calls.find(c => /^diagnose/.test(c.stage))?.returned_model;
  const modelLine = id => (id ? h('div', { class: 'mono small muted' }, id) : null);
  let outcomeNotice = null;
  if (!r) {
    const cp = ev.candidate_patch;
    const applied = cp ? cp.status === 'accepted_for_validation' : (ev.attempts || []).some(a => a.phase === 'candidate_patch');
    const rejectedWhy = cp?.status === 'rejected' ? cp.reason : run.reason || ev.reason;
    const outcome = applied ? t('cand_accepted') : rejectedWhy ? t('cand_rejected', { r: stopText(rejectedWhy) }) : t('cand_none');
    // Older records kept only the rejection reason, not the proposal itself. Never reconstruct it.
    const missing = !cp && d.status === 'candidate' && !applied;
    outcomeNotice = h('div', { class: `notice ${applied ? 'info' : 'warn'}` }, h('strong', {}, t('diag_candidate')), h('p', {}, outcome),
      cp ? h('p', { class: 'small' }, t('cprop_below')) : missing ? h('p', { class: 'small' }, t('cprop_missing')) : null);
  } else if (r.status === 'no_candidate') {
    outcomeNotice = h('div', { class: 'notice warn' }, h('strong', {}, t('diag_candidate')),
      h('p', {}, d.reason || r.reason ? t('round_no_candidate', { r: stopText(d.reason || r.reason) }) : t('round_model_declined')));
  }
  const callRows = calls.map(c => h('tr', {},
    h('td', {}, lbl('stage', c.stage), c.data_origin === 'engineering_test' ? h('div', {}, badge('neutral', t('test_double'))) : null),
    h('td', { class: 'mono small' }, c.returned_model || c.requested_model || '—'),
    h('td', { class: 'mono small num' }, `${c.usage?.prompt_tokens ?? '—'} / ${c.usage?.completion_tokens ?? '—'}`, c.usage?.reasoning_tokens ? h('div', { class: 'muted' }, `${t('call_reasoning')} ${c.usage.reasoning_tokens}`) : null),
    h('td', { class: 'mono small num' }, c.latency_ms != null ? fmtDur(c.latency_ms / 1000) : '—'),
    h('td', { class: 'mono small num' }, c.data_origin === 'engineering_test' ? t('test_rate') : c.calculated_usd != null ? money(Number(c.calculated_usd)) : t('unknown'))));
  return h('li', {},
    h('span', { class: 'tl-dot model', 'aria-hidden': 'true' }, 'AI'),
    h('div', { class: 'tl-head' }, t('diag_title'), r ? h('span', { class: 'small muted' }, t('round_n', { n: r.round })) : null,
      d.data_origin === 'nebius_api' ? badge('info', 'Nebius Token Factory') : d.data_origin === 'engineering_test' ? badge('neutral', t('test_double')) : null),
    h('div', { class: 'tl-body' },
      d.classification ? kv([[t('diag_class'), [badge('neutral', d.classification.category), ' ', d.classification.reason, modelLine(classifyModel)]]]) : null,
      d.explanation ? h('div', { class: 'stack', style: 'gap:4px' }, h('h3', {}, t('diag_explain')), modelLine(diagnoseModel), h('p', { style: 'margin:0' }, d.explanation)) : null,
      outcomeNotice,
      calls.length ? h('div', { class: 'stack', style: 'gap:6px' }, h('h3', {}, t('calls_title')), h('div', { class: 'table-scroll' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, t('call_stage')), h('th', {}, t('call_model')), h('th', { class: 'num' }, t('call_tokens')), h('th', { class: 'num' }, t('call_latency')), h('th', { class: 'num' }, t('call_cost')))),
        h('tbody', {}, callRows)))) : null));
}
// The candidate exactly as proposed, before (or instead of) being applied. Shown as text only.
// With a round record r, that round's own diff (from the evidence) follows the proposal.
function candidateItem(run, cp, runKey, r = null) {
  const key = `${runKey}-cand`;
  const p = cp.patch && typeof cp.patch === 'object' ? cp.patch : {};
  const edits = Array.isArray(p.edits) ? p.edits : [];
  const setup = Array.isArray(p.setup) ? p.setup : [];
  const applied = cp.status === 'accepted_for_validation';
  const str = v => (typeof v === 'string' ? v : JSON.stringify(v));
  const diff = typeof r?.patch?.diff === 'string' ? r.patch.diff : null;
  const verified = r?.status === 'verified_fix';
  return h('li', {},
    h('span', { class: `tl-dot ${cp.status === 'rejected' ? 'failed' : 'patch'}`, 'aria-hidden': 'true' }, cp.status === 'rejected' ? '✕' : '±'),
    h('div', { class: 'tl-head' }, t('cprop_title'), r ? h('span', { class: 'small muted' }, t('round_n', { n: r.round })) : null,
      run.repair_mode ? badge(run.repair_mode, lbl('repair', run.repair_mode)) : null,
      badge(cp.status === 'rejected' ? 'failed' : applied ? 'info' : 'warn', lbl('cand', cp.status)),
      applied ? null : badge('neutral', t('cprop_not_applied_badge')),
      r && applied ? badge(verified ? 'passed' : 'warn', verified ? t('patch_verified') : t('patch_unverified')) : null),
    h('div', { class: 'tl-body' },
      applied ? h('p', { class: 'small muted', style: 'margin:0' }, t('cprop_applied_note'))
        : h('div', { class: 'notice warn' }, h('p', {}, t('cprop_not_applied'))),
      cp.status === 'rejected' && cp.reason ? h('div', { class: 'notice bad' }, h('strong', {}, t('cprop_reason')), ...refusal(cp.reason)) : null,
      edits.length ? edits.map((e, i) => h('div', { class: 'stack', style: 'gap:4px' },
        h('h3', {}, t('cprop_edit', { i: i + 1, n: edits.length }), ' ', h('span', { class: 'mono' }, str(e?.path ?? '—')),
          Number.isInteger(e?.start) ? h('span', { class: 'small muted' }, ` · ${t('cprop_lines', { s: e.start, e: e.end })}`) : null),
        h('div', { class: 'small muted' }, t('cprop_old')), h('pre', {}, h('span', { class: 'del' }, str(e?.old ?? ''))),
        h('div', { class: 'small muted' }, t('cprop_new')), h('pre', {}, h('span', { class: 'add' }, str(e?.new ?? ''))))) : h('p', { class: 'empty' }, t('cprop_no_edits')),
      setup.length ? h('div', { class: 'stack', style: 'gap:4px' }, h('h3', {}, t('cprop_setup')),
        h('pre', {}, setup.map(s => (Array.isArray(s?.argv) ? s.argv.map(str).join(' ') : str(s))).join('\n'))) : null,
      diff ? h('div', { class: 'stack', style: 'gap:6px' }, h('h3', {}, t('round_diff')), diffView(diff),
        run.data_origin === 'example' ? null : h('div', { class: 'row', style: 'align-items:center' },
          downloadButton(run, diff, `-round${r.round}`), h('span', { class: 'small muted' }, t('patch_not_published')))) : null,
      foldable(`${key}-raw`, t('cprop_raw'), h('pre', {}, JSON.stringify(cp.patch ?? null, null, 2)))));
}
function patchItem(run, ev, patch) {
  const isExample = run.data_origin === 'example';
  const text = isExample ? ev.patch?.diff : patch?.text;
  if (!run.patch_available && !text) return null;
  const verified = run.result_status === 'verified_fix';
  const download = text && !isExample ? downloadButton(run, text) : null;
  return h('li', {},
    h('span', { class: 'tl-dot patch', 'aria-hidden': 'true' }, '±'),
    h('div', { class: 'tl-head' }, t('patch'),
      run.repair_mode === 'manual_candidate' ? badge('manual_candidate', lbl('repair', 'manual_candidate')) : null,
      badge(verified ? 'passed' : 'warn', verified ? t('patch_verified') : t('patch_unverified'))),
    h('div', { class: 'tl-body' },
      run.repair_author ? h('p', { class: 'small muted', style: 'margin:0' }, `${t('repair_author')}: ${run.repair_author}`) : null,
      text ? diffView(text) : patch?.error ? errorBox(patch.error) : h('p', { class: 'empty' }, t('patch_loading')),
      download ? h('div', { class: 'row', style: 'align-items:center' }, download, h('span', { class: 'small muted' }, t('patch_not_published'))) : null));
}

/* ------------------------------------------------------------------ evaluation */
// Metrics come from evaluation.js (pure, tested in web/tests/); this page only displays them.
const frac = (n, d) => h('span', { class: 'metric-n' }, `${n} / ${d}`);
const timeText = (known, unknownCount) => (known == null ? t('unknown')
  : unknownCount ? t('known_subtotal', { v: fmtDur(known), n: unknownCount }) : fmtDur(known));
const costText = (known, unknownCount) => (known == null ? t('unknown')
  : unknownCount ? t('known_subtotal', { v: money(known), n: unknownCount }) : money(known));
const issueStatus = s => (s === 'in_progress' ? lbl('state', 'running') : s ? lbl('result', s) : t('not_run'));
const issueText = i => t(`issue_${i.code}`, { c: i.case_id, g: i.group ?? '', n: i.count ?? '', s: issueStatus(i.status) });

// Team review of a claimed fix. A rejected fix keeps its engine result but is never presented as human-accepted.
function reviewBadge2(r) {
  const v = verdictOf(r); const hr = r.human_review || {};
  const title = v === 'pending' ? null : [hr.reviewer, hr.reviewed_at && when(hr.reviewed_at), hr.note].filter(Boolean).join(' · ');
  return v === 'accepted' ? badge('passed', t('review_accepted'), null, '', title)
    : v === 'rejected' ? badge('failed', t('review_rejected_cell'), null, '', title) : badge('neutral', t('review_pending'));
}
function resultReviewNotice(run) {
  if (!run.evaluation_group || run.result_status !== 'verified_fix') return null;
  const v = verdictOf({ human_review: run.result_review }); const hr = run.result_review || {};
  const vars = { who: hr.reviewer || '—', at: hr.reviewed_at ? when(hr.reviewed_at) : '—', note: hr.note || '' };
  return v === 'accepted' ? h('div', { class: 'notice info' }, h('p', {}, t('result_review_accepted', vars)))
    : v === 'rejected' ? h('div', { class: 'notice bad' }, h('p', {}, t('result_review_rejected', vars)))
      : h('div', { class: 'notice warn' }, h('p', {}, t('result_review_pending')));
}
function viewEvaluation() {
  const ev = state.evaluation;
  const toggle = h('div', { class: 'seg', role: 'group', 'aria-label': t('s5') },
    ['results', 'example'].map(src => h('button', { type: 'button', 'aria-pressed': String(ev.source === src),
      onclick: () => { if (ev.source !== src) loadEvaluation(src); } }, src === 'results' ? t('eval_real') : t('eval_example'))));
  const head = h('div', { class: 'stack', style: 'gap:8px' },
    h('div', { class: 'result-line' }, h('h1', {}, t('s5')), ev.data?.data_origin === 'example' ? badge('example', t('example_badge')) : null),
    h('p', { class: 'lead' }, t('s5_hint')), h('div', {}, toggle));
  const groupList = card(h('h2', {}, t('eval_groups')), h('ul', { class: 'tight small' }, GROUPS.map(g => h('li', {}, lbl('group', g)))));
  if (!ev.loaded) return [head, h('p', { class: 'empty' }, t('loading'))];
  if (ev.error) return [head, h('div', { class: 'notice bad' }, h('strong', {}, t('eval_load_error')), h('p', {}, ev.error))];
  if (!ev.data) return [head, card(h('p', { class: 'empty' }, readOnly() ? t('demo_eval_empty') : t('eval_empty'))), groupList];

  const d = ev.data; const m = ev.metrics;
  const table = (heads, rows) => h('div', { class: 'table-scroll' }, h('table', {},
    h('thead', {}, h('tr', {}, heads.map(([text, cls]) => h('th', { class: cls }, text)))), h('tbody', {}, rows)));
  const num = v => h('td', { class: 'num' }, v);
  const extra = (...parts) => h('span', { class: 'small muted' }, parts.filter(Boolean).join(' · '));

  const metricRows = [
    [t('m_original'), frac(m.aPassed.length, m.aValid.length), extra(m.holdout.length - m.aValid.length ? t('excluded', { n: m.holdout.length - m.aValid.length }) : '')],
    ...['B', 'C', 'D'].map(g => [t('m_fix', { g }), frac(m.fix[g].n, m.fix[g].d), extra(m.fix[g].unpaired ? t('unpaired', { n: m.fix[g].unpaired }) : '',
      m.fix[g].n ? t('fix_review_note', { a: m.fix[g].accepted, r: m.fix[g].rejected, p: m.fix[g].pending }) : '')]),
    [t('m_healthy'), frac(m.healthy.n, m.healthy.d), extra()],
    ...['B', 'C', 'D'].map(g => [t('m_misjudge', { g }), frac(m.review[g].n, m.review[g].d), extra(m.review[g].pending ? t('pending_reviews', { n: m.review[g].pending }) : '')]),
    [t('m_boundary'), frac(m.boundaryRows.filter(r => r.match).length, m.boundaryRows.length), extra()],
  ];
  const metrics = card(h('h2', {}, t('eval_metrics')), h('div', { class: 'table-scroll' }, h('table', {},
    h('tbody', {}, metricRows.map(([k, v, x]) => h('tr', {}, h('td', {}, k), h('td', { class: 'num' }, v), h('td', {}, x)))))));
  const freeze = card(h('h2', {}, t('eval_freeze')), kv([
    [t('frozen_at'), when(d.frozen_at)],
    [t('code_version'), h('span', { class: 'mono' }, d.freeze?.code_version ?? '—')],
    [t('prompts_hash'), h('span', { class: 'mono' }, short(d.freeze?.prompts_sha256))],
    [t('runner'), h('span', { class: 'mono' }, d.freeze?.runner ?? '—')],
    [t('dependency_mode'), d.freeze?.dependency_mode ? lbl('dep', d.freeze.dependency_mode) : h('span', { class: 'muted' }, t('not_recorded'))],
    d.limits && [t('limit_case'), d.limits.case_model_usd != null ? money(d.limits.case_model_usd) : t('unknown')],
    d.limits && [t('limit_batch'), d.limits.batch_model_usd != null ? money(d.limits.batch_model_usd) : t('unknown')],
    d.limits?.time_limit_s != null && [t('limit_time'), fmtDur(d.limits.time_limit_s)],
  ]));
  const stops = m.batchStops.length ? h('div', { class: 'notice bad' }, h('strong', {}, t('batch_stops_title')),
    h('ul', { class: 'tight small' }, m.batchStops.map(b => h('li', { class: 'mono' }, typeof b === 'string' ? b : JSON.stringify(b))))) : null;
  // Not-run groups, as the export states them: one line per group and reason, never shown as 0/0 results.
  const unrunGroups = GROUPS.map(g => {
    const rows = m.unrun.filter(u => u.group === g);
    return rows.length ? [g, rows.length, [...new Set(rows.map(u => u.reason || t('unknown')))].join(' / ')] : null;
  }).filter(Boolean);
  const unrunView = unrunGroups.length ? card(h('h2', {}, t('unrun_title')), h('ul', { class: 'tight small' },
    unrunGroups.map(([g, n, reason]) => h('li', {}, h('strong', {}, lbl('group', g)), ' · ', t('unrun_cases', { n }), h('div', { class: 'muted' }, reason))))) : null;
  const inProgress = m.inProgress.length ? h('div', { class: 'notice info' }, h('strong', {}, t('in_progress')),
    h('ul', { class: 'tight small' }, m.inProgress.map(r => h('li', {}, `${r.case_id} · ${r.group} · ${lbl('state', r.run_state)}`)))) : null;
  const notes = [...m.issues.map(issueText), ...m.aNotes.map(issueText)];
  const notesView = notes.length ? h('div', { class: 'notice warn' }, h('strong', {}, t('excluded_runs')), h('ul', { class: 'tight small' }, notes.map(n => h('li', {}, n)))) : null;

  const cell = (c, g) => {
    const r = m.of(c.case_id)[g];
    if (!r) {
      if (m.mismatched.has(`${c.case_id}:${g}`)) return h('td', {}, badge('warn', t('hash_mismatch')));
      const skipped = m.unrun.find(u => u.case_id === c.case_id && u.group === g);
      return h('td', {}, skipped ? badge('neutral', t('unrun_badge')) : h('span', { class: 'muted small' }, t('not_run')));
    }
    // Link to the report when this run is one the page can open (the demo packs evaluation runs).
    const status = badge(r.result_status || 'none', lbl('result', r.result_status));
    const openable = r.run_id && state.runs?.some(x => x.run_id === r.run_id);
    return h('td', {}, h('div', { class: 'cell' }, openable ? h('a', { href: `#/runs/${encodeURIComponent(r.run_id)}` }, status) : status,
      h('span', { class: 'meta' }, `${fmtDur(r.duration_s)} · ${money(r.cost_usd)}`),
      r.result_status === 'verified_fix' ? reviewBadge2(r) : null));
  };
  const matrix = card(h('h2', {}, t('eval_matrix')), table([[t('case'), null], ...GROUPS.map(g => [lbl('group', g), null])],
    m.holdout.map(c => h('tr', {}, h('td', { class: 'mono small' }, c.case_id), GROUPS.map(g => cell(c, g))))));
  const timeCost = card(h('h2', {}, t('eval_time_cost')),
    table([['', null], [t('runs'), 'num'], [t('total_time'), 'num'], [t('median_time'), 'num'], [t('unknown_times'), 'num'], [t('model_usage_cost'), 'num'], [t('known_cost'), 'num'], [t('unknown_costs'), 'num']],
      m.timeCost.map(r => h('tr', {}, h('td', {}, lbl('group', r.g)), num(String(r.runs)),
        num(timeText(r.knownTime, r.unknownTime)), num(r.medianTime == null ? t('unknown') : fmtDur(r.medianTime)), num(String(r.unknownTime)),
        num(costText(r.knownModel, r.unknownModel)), num(costText(r.knownCost, r.unknownCost)), num(String(r.unknownCost))))),
    h('p', { class: 'small muted', style: 'margin:0' }, t('median_note')));
  const cvd = card(h('h2', {}, t('eval_cvd')), m.cvdCases
    ? table([['', null], [t('verified'), 'num'], [t('total_time'), 'num'], [t('unknown_times'), 'num'], [t('known_cost'), 'num'], [t('unknown_costs'), 'num']],
      m.cvd.map(r => h('tr', {}, h('td', {}, lbl('group', r.g)), num(frac(r.verified, m.cvdCases)),
        num(timeText(r.knownTime, r.unknownTime)), num(String(r.unknownTime)), num(costText(r.knownCost, r.unknownCost)), num(String(r.unknownCost)))))
    : h('p', { class: 'empty' }, t('not_run')));
  const boundary = card(h('h2', {}, t('eval_boundary')), !m.boundaryRows.length ? h('p', { class: 'empty' }, t('not_run')) : table([[t('case'), null], [t('expected_state'), null], [t('actual_state'), null], [t('match'), null]],
    m.boundaryRows.map(r => h('tr', {}, h('td', { class: 'mono small' }, r.case_id),
      h('td', {}, badge(r.expected || 'none', lbl('result', r.expected))),
      h('td', {}, r.actual ? badge(r.actual, lbl('result', r.actual)) : h('span', { class: 'muted small' }, t('not_run'))),
      h('td', {}, r.match == null ? '—' : h('span', { class: r.match ? 'match-yes' : 'match-no' }, r.match ? '✓' : '✗'))))));
  const notice = d.data_origin === 'example' && d.example_notice ? h('div', { class: 'notice neutral' }, h('p', {}, d.example_notice)) : null;
  // ② Horizontal bands, read top to bottom: the headline numbers, then the groups side by side, then every case.
  const rejectedAll = ['B', 'C', 'D'].reduce((a, g) => a + m.review[g].n, 0);
  const verifiedAll = ['B', 'C', 'D'].reduce((a, g) => a + m.review[g].d, 0);
  const statTile = (k, n, d, sub) => h('div', { class: 'tile' }, h('span', { class: 'k' }, k), h('span', { class: 'v' }, `${n} / ${d}`), sub ? h('span', { class: 's' }, sub) : null);
  const band1 = h('section', { class: 'band', 'aria-labelledby': 'band1' }, h('h2', { class: 'band-label', id: 'band1' }, t('band_numbers')),
    h('div', { class: 'tiles below' },
      statTile(t('m_original'), m.aPassed.length, m.aValid.length, null),
      ...['B', 'C', 'D'].map(g => statTile(t('m_fix', { g }), m.fix[g].n, m.fix[g].d,
        m.fix[g].n ? t('fix_review_note', { a: m.fix[g].accepted, r: m.fix[g].rejected, p: m.fix[g].pending }) : m.fix[g].d ? null : t('not_run'))),
      statTile(t('m_rejected_all'), rejectedAll, verifiedAll, null)));
  const barRow = (label, n, d) => h('div', { class: 'bar-row' }, h('span', {}, label),
    h('div', { class: 'meter', role: 'img', 'aria-label': `${n} / ${d}` }, h('span', { style: `width:${d ? Math.round((n / d) * 100) : 0}%` })),
    h('span', { class: 'metric-n' }, d ? `${n} / ${d}` : t('not_run')));
  const compare = card(h('h3', {}, t('band_compare_bars')), h('div', { class: 'bars' },
    barRow(`${lbl('group', 'A')} · ${t('bar_pass')}`, m.aPassed.length, m.aValid.length),
    ...['B', 'C', 'D'].map(g => barRow(`${lbl('group', g)} · ${t('bar_fix')}`, m.fix[g].n, m.fix[g].d))),
    h('p', { class: 'small muted', style: 'margin:0' }, t('bar_note')));
  const band2 = h('section', { class: 'band', 'aria-labelledby': 'band2' }, h('h2', { class: 'band-label', id: 'band2' }, t('band_compare')), compare, timeCost, cvd);
  const band3 = h('section', { class: 'band', 'aria-labelledby': 'band3' }, h('h2', { class: 'band-label', id: 'band3' }, t('band_detail')),
    matrix, unrunView, boundary, h('div', { class: 'grid-main' }, metrics, freeze));
  return [head, notice, stops, inProgress, notesView, band1, band2, band3, h('p', { class: 'small muted', style: 'margin:0' }, t('eval_limits'))];
}
async function loadEvaluation(source) {
  const ev = state.evaluation;
  Object.assign(ev, { source, loaded: false, error: null, data: null, metrics: null });
  if (route().page === 'evaluation') render();
  try {
    const res = await fetch(source === 'example' ? 'examples/evaluation-example.json' : 'evaluation.json', { cache: 'no-store' });
    if (res.status === 404 && source === 'results') ev.data = null;
    else if (!res.ok) throw new Error(`HTTP ${res.status}`);
    else { const d = await res.json(); ev.metrics = computeEvaluation(d); ev.data = d; } // throws on a file that isn't an evaluation file
  } catch (e) { ev.error = String(e.message || e); }
  ev.loaded = true;
  if (route().page === 'evaluation') render();
}

/* ------------------------------------------------------------------ state preview */
function viewStates() {
  const head = h('div', { class: 'stack', style: 'gap:8px' }, h('div', { class: 'result-line' }, h('h1', {}, t('s4')), badge('example', t('example_badge'))),
    h('p', { class: 'lead' }, t('states_intro')));
  if (!state.examples) return [head, state.examplesError ? errorBox(state.examplesError) : h('p', { class: 'empty' }, t('loading'))];
  const names = Object.keys(state.examples);
  if (!names.includes(state.exampleName)) state.exampleName = names[0];
  const picker = h('div', { style: 'max-width:340px' }, h('label', { class: 'field', for: 'example' }, t('pick_example')),
    h('select', { id: 'example', onchange: e => { state.exampleName = e.target.value; render(); } }, names.map(n => h('option', { value: n, selected: n === state.exampleName }, n))));
  const ex = state.examples[state.exampleName];
  return [head, state.examplesError ? errorBox(state.examplesError) : null, picker, ...(ex ? reportView(ex) : [])];
}

/* ------------------------------------------------------------------ loading */
async function loadHealth() {
  try { state.health = await api.health(); state.healthError = null; } catch (e) { state.healthError = e; }
  softRender();
}
async function loadCases() {
  state.casesError = null;
  try { const { items } = await api.cases(); state.cases = items || []; } catch (e) { state.casesError = e; }
  softRender();
}
async function loadRuns() {
  try { const { items } = await api.runs(); state.runs = items || []; state.runsError = null; } catch (e) { state.runsError = e; }
  softRender();
}
// Labelled examples that live only in web/examples (never in /api/runs or results), shown next to the API's.
const LOCAL_EXAMPLES = ['two-round-success', 'two-round-budget-stop'];
async function loadExamples() {
  const found = {};
  try {
    const { items } = await api.examples();
    const entries = await Promise.all(Object.entries(items || {}).map(async ([name, v]) => [name, v && typeof v === 'object' && v.run_state ? v : await api.example(name)]));
    Object.assign(found, Object.fromEntries(entries));
  } catch (e) { state.examplesError = e; }
  await Promise.all((api.readOnly ? [] : LOCAL_EXAMPLES).map(async name => {
    try {
      const res = await fetch(`examples/${name}.json`, { cache: 'no-store' });
      const d = res.ok ? await res.json() : null;
      if (d?.data_origin === 'example') found[name] = d; // only labelled examples
    } catch { /* missing locally: skip */ }
  }));
  if (Object.keys(found).length) state.examples = found;
  if (route().page === 'states') softRender();
}

document.getElementById('lang').addEventListener('click', () => {
  lang = lang === 'zh' ? 'en' : 'zh';
  try { localStorage.setItem('fe-lang', lang); } catch { /* storage unavailable */ }
  render();
});
// Every 10 s: while the API is unreachable, retry so the banner clears on its own; while it's up, check it
// quietly so a backend that stops mid-session shows the banner (re-rendering only when that changes).
setInterval(async () => {
  if (state.healthError) { loadHealth(); loadCases(); loadRuns(); return; }
  if (readOnly()) return;
  try { state.health = await api.health(); } catch (e) { state.healthError = e; softRender(); }
}, 10000);

onRoute();
loadHealth(); loadCases(); loadRuns(); loadExamples();
