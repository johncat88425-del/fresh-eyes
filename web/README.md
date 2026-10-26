# Fresh Eyes frontend

Plain HTML and ES modules. No build step, no packages, no keys.

```sh
python3.12 scripts/fresh-eyes serve      # API on 127.0.0.1:8765 (backend owner)
python3 web/serve.py                     # frontend on http://localhost:5173/
```

`web/serve.py` serves only `web/`, `api/client.js` (as `/client.js`) and `evaluation/results.json` (as `/evaluation.json`), so nothing else in the project, including `.env`, is reachable from the browser. Port 5173 is one of the API's default allowed origins. To point at another API instance, add `?api=`, for example `http://localhost:5173/?api=http://127.0.0.1:50629`.

## Files

| File | What it holds |
|---|---|
| `index.html` | Page shell and all styles (light and dark themes) |
| `app.js` | Pages, routing, API calls, polling |
| `i18n.js` | Every UI string in Chinese and English, plus status labels |
| `evaluation.js` | Evaluation metrics and data checks; no DOM |
| `tests/evaluation.test.mjs` | `node --test web/tests/*.test.mjs` (synthetic data only) |
| `examples/evaluation-example.json` | Invented, labelled example for the evaluation page |

## Pages

The site opens on a beginner front door (below). The detailed pages' header has four destinations (Simple view, Overview, Runs, Evaluation), a connection indicator and the language switch. If the API can't be reached, a red banner says so on every page and approval is disabled; the page retries every 10 seconds.

| Route | Page | API |
|---|---|---|
| `#/dashboard` | **Overview** (was `#/` before B24): what Fresh Eyes does in three steps, the Level 1 quick check, registered cases with their last result, recent runs | `GET /api/health`, `/api/cases`, `/api/runs` |
| `#/cases/{case_id}` | **Case**: what the README promises (goal, README lines, environment, acceptance hash, both inputs and expected outputs); approval; run form; nightly checks (shown as not available while `health.nightly_checks` is false); this case's runs | `POST /api/cases/{id}/approval`, `GET /api/cases/{id}/approval/history`, `POST /api/cases/{id}/approval/correction`, `POST /api/runs` |
| `#/runs` | **Runs**: every real run | `GET /api/runs` |
| `#/runs/{run_id}` | **Run report**: result with a one-line explanation, progress, facts; timeline; costs; evidence | `GET /api/runs/{id}` every 1.5 s while queued or running; `GET /api/runs/{id}/patch` |
| `#/evaluation` | **Evaluation**: metrics computed from `evaluation/results.json`, or the labelled example | `/evaluation.json` |
| `#/states` | **Preview every state** (linked from the footer): the backend's static examples through the same report view | `GET /api/examples` |

## Rules the UI keeps

- **Approval** needs a name and the "I have read both" box. A case shows **Locked** only from the server's reply (`review.status === "approved"` and its `acceptance_sha256` equal to the case's current hash). After locking, the page reminds the reviewer to record the approval in `cases/CASE_NOTES.md`. A 409 `acceptance_changed` or `approval_exists` offers a reload.
- **Approval history and corrections** (only when `health.approval_history` / `approval_corrections` are true): a locked case shows its revision and, folded, every history event (approved, legacy snapshot, correction, imported) with reviewer, times, and for a correction who made it, why, and which revision it replaces. A legacy snapshot comes with a note that history overwritten by the old API can't be rebuilt. "Correct the reviewer name" is a separate form (new name, who is correcting, reason, a confirmation box) that sends `expected_revision` from the latest history entry; the normal Approve button still uses `POST /approval`. On `409 approval_changed` the typed fields stay and a reload button refreshes the case and history.
- **Progress and result are separate** everywhere; a finished run that didn't pass says so.
- **Run report reading order:** the original README run and its error, then the patch, then the patched run and the final clean re-run. In each run the README command comes first; the venv and workspace probe are folded into "Environment check", which opens by itself if any of those steps failed. Full argv and logs stay one click away, and the open/closed state survives polling.
- **Patch:** labelled "Manual candidate patch" when `repair_mode` is `manual_candidate`, and "Unverified" unless the result is `verified_fix`. "Download .patch" saves the exact text from the patch endpoint; downloading publishes nothing.
- **Model diagnosis:** when the run's evidence has a `diagnosis` block, the timeline adds a "Model diagnosis" step after the original run: Nano's classification, Ultra's explanation, what happened to the candidate patch (rejected with the reason, or applied), and a call table (stage, returned model ID, tokens, reasoning tokens, time, cost from usage). Costs then come from that ledger and read "(from usage, pending reconciliation)", with "Billed" shown separately (unknown while `billed_usd` is null).
- **Candidate proposal:** when the evidence has `candidate_patch`, the timeline adds "Candidate patch proposal" after the diagnosis: its status (proposed / rejected / applied for validation), a "Not applied" badge unless it was applied, the rejection reason, every edit (path, text to find, replacement), proposed setup steps, and the raw JSON. A model run whose diagnosis says it proposed a candidate but has no `candidate_patch` (runs recorded before proposals were saved, such as `e5150fec…`) says the proposal wasn't kept; nothing is reconstructed.
- **Repair rounds:** when the evidence has `repair_rounds`, the timeline is grouped by round. It shows the original README run first. Then, for each round:
  - a "Repair round n of N" marker with the round's status (verified, applied but failed the check, proposal rejected, no candidate, testing);
  - that round's diagnosis, with only that round's ledger calls (`classify`/`diagnose` are round 1, `*_r2` round 2);
  - the round's own proposal and diff;
  - the attempts whose `round` matches. The same phase may repeat, so the round is part of every fold key.

  Model IDs come from `classification_model` / `diagnostic_model` or the ledger, never from the route name, because in group D both steps are Ultra. A round with no candidate says why (for example "Budget used up"). Engineering test doubles are badged, their rates show as "Test rate", and the cost card says there's no real cost. Records without `repair_rounds` keep the older single-round layout. The state preview also loads the two labelled examples in `web/examples/two-round-*.json`, which never appear in `/api/runs` or results.
- **Recorded data:** a run with `data_origin: "recorded_replay"` gets a "Recorded real run · replay" badge and a note that it isn't a new run.
- **Costs:** `null` shows "unknown"; a model cost of 0 with no model calls reads "$0 (no model calls)"; `billing_status` shows a readable label next to the raw code.
- **Dependencies:** options come from `health.dependency_modes`; `pypi_live` is selectable only when `health.live_pypi_enabled` is true.
- **Quick check (Level 1):** URL and optional 40-character SHA are checked in the browser. An enabled startup API runs the documented install/help workflow; otherwise the page offers local setup steps. `#/try` independently reads public GitHub README commands in the visitor browser without executing them.
- **Evaluation** data checks and metric definitions: [evaluation/README.md](../evaluation/README.md).
- README text, logs, output and diffs are inserted as text, never HTML. Times are UK time (Europe/London) with BST/GMT.

## Read-only demo

A version for judges that needs no backend: the same pages, reading recorded real runs from static JSON.

```sh
python3.12 web/demo/build.py            # writes dist/demo/; --replace rebuilds an earlier demo
python3 -m http.server 3001 --bind 127.0.0.1 --directory dist/demo   # look at it locally
```

- **What goes in (26 October):** four registered development cases (including Chris-approved envdoctor), three reviewed boundary case cards, 24 real development/startup/boundary recordings and ten labelled synthetic examples. Two different AI-repaired repositories keep separate highlights. Owner builds can additionally include four private formal A captures; those are excluded from the public v10 source and viewer. Replay count is not a count of repaired repositories.
- **What it can't do:** `web/demo/static-client.js` has the same interface as `api/client.js` but refuses every write (`read_only_demo`). With `health.read_only`, the page shows a blue "Read-only demo" banner and replaces the quick check with "Start here" links (chosen by the build in `health.demo_highlights`). There's no approve, correct or run form. The public evaluation page says formal reports are not included; the owner workspace separately has the historical A baseline.
- **Safety check:** the build stops if any output file contains a local path (`/Users/`, `/home/`, `C:\Users\`) or anything shaped like a credential. The one allowed `/home/` is an example path in charset_normalizer's package description, quoted in pip's log. `data/manifest.json` lists every file's SHA-256.
- **Rebuild inputs:** the curated JSON and manual diff live in `web/demo/recordings/` with a hash manifest and are included in the source handoff. Include them in the future public repo. Normal builds need no `reports/`, backend, database, keys, VM or network. `--refresh-recordings` is the explicit owner-only export from existing reports; it makes no model calls. See [build and integrity details](demo/README.md).
- **Holdout runs after the evaluation (B, 2026-10-21):** `--evaluation-records DIR` (repeatable) adds the runs of an evaluation output folder that appear in the formal `evaluation/results.json`. It checks each `report.json` (and `model-ledger.json` for model slots) against its binding's hashes, and stops if anything differs or no formal results file exists. The evaluation table then links each result to its report, and the report shows its evaluation group. Steps: [evaluation/B_RUNBOOK_1024.md](../evaluation/B_RUNBOOK_1024.md).
- Hash routes (`#/runs/…`) work on any static host. Nothing is published by the build itself.

## Checked

- **2026-10-18:** against an isolated API copy, every flow, including the error states.
- **2026-10-19 (redesign):** against A's live API, read-only (no approval or run submitted). All six pages load in Chinese and English; the md2html run report shows the four timeline steps in order; no horizontal scroll at 375 px with every log expanded; the API-down banner and disabled approval appear when `?api=` points nowhere. Banner contrast is 6.5:1 (light) and 7.5:1 (dark).
- **2026-10-20 (history, correction, candidate):** against scratch-data API instances only (synthetic reviewer names; the real approvals were read, never written). Approve → history shows revision 1; a concurrent correction made outside the page → the form gets `409 approval_changed`, keeps its fields, reloads to revision 2; resubmit → revision 3 with corrector, reason and "Replaces revision 2". An old-style database shows a legacy snapshot with its note. A stale page approving under a second name gets `409 approval_exists` with a reload button. The candidate view was checked with one invented, labelled rejected proposal, and with the recorded model run, which correctly says its proposal wasn't kept. No horizontal scroll at 375 px.

## Beginner front door (B24, 2026-10-21)

For someone who has never used a computer much: one question and one main button per screen, plain words, text at least 18 px (body 20 px), buttons at least 56 px, every status a symbol plus a word, text contrast at least 7:1 in light and dark. The design came first as a clickable prototype: [design/beginner/README.md](../design/beginner/README.md). The header shows only **Help** (a native dialog) and **Detailed view**; on phones the detailed view is linked from the footer.

| Route | Screen | What it does |
|---|---|---|
| `#/` | Welcome | **Start**, or "See a real example first" |
| `#/start` | Step 1 of 3 | One card per registered case. **Continue** stays disabled until one is chosen (pressing it shows the hint instead) |
| `#/start/{case_id}` | Step 2 of 3 | The case's first locked example as before → after, and whether a person checked it. **Try the instructions** appears only when this copy can really run a check (not read-only, runner on, case locked); it starts a README-only run (`repair_mode: none`, never the model), with the dependency mode of the case's last run if still allowed, else the offline snapshot. Otherwise **See the result** opens the latest real check and the page says new checks can't run here |
| `#/answer/{run_id}` | Step 3 of 3 | While queued or running: three plain stages, polled like the run report. When finished, by result: works, missing a step and fixed (the changed lines, who wrote the fix, **Download the fix**), doesn't work, stopped and asks a person (model runs say the AI stopped instead of guessing), needs input, test computer problem. "Show details" has at most six plain rows (original run, AI calls with usage-based cost, the change, each re-run, time) and links the full report |
| `#/examples` | Real examples | The build's highlights, then one finished check per case and outcome |

Plain project descriptions are `plain_case_{case_id}` strings in `i18n.js`; a case without one shows its goal. Unknown routes open the welcome page.

## Layouts (B22, 2026-10-21)

Each page uses the dashboard layout that fits its content; every layout falls back to one column below 900 px (760 px for the canvas), with no horizontal page scroll at 375 px.

| Page | Layout | What goes where |
|---|---|---|
| Overview | ① Primary + secondary | Big "featured run" card showing the fix as a step flow (demo: the build's `fix` highlight, or `model_fix` if one exists; local: the latest verified fix, else the latest run). 2×2 tiles on the right: locked cases, real runs, fixes verified (by hand · by model), runs needing review. The three steps sit as tiles below |
| Evaluation | ② Horizontal bands | Band 1 headline numbers as tiles; band 2 groups side by side (passed / total bars, time and cost, C vs D); band 3 every case (matrix, groups not run, boundary, metric table, freeze) |
| Run report | ③ Main + sticky side rail | Timeline in the main column. The right rail stays in view while scrolling (`position: sticky`): result, a "jump to a step" list (buttons, so hash routing is untouched), costs, evidence |
| Case | ④ Canvas + floating panels | The README's promise and each input → expected pair fill the canvas; README source, acceptance lock, environment and latest run float in its corners. Approval and run forms follow below. On phones the panels stack |
| Runs | ⑤ Kanban + ⑥ swimlanes (+ table) | Switch Board / Swimlanes / Table (remembered; `?view=` in a link). The board has one column per result. Swimlanes have one lane per case and one bar per run on a time axis; runs that would overlap move to an extra row, so none is hidden; there's a "now" line when now is in range |

## Rules the UI keeps

- **Approval** needs a name and the "I have read both" box. A case shows **Locked** only from the server's reply (`review.status === "approved"` and its `acceptance_sha256` equal to the case's current hash). After locking, the page reminds the reviewer to record the approval in `cases/CASE_NOTES.md`. A 409 `acceptance_changed` or `approval_exists` offers a reload.
- **Approval history and corrections** (only when `health.approval_history` / `approval_corrections` are true): a locked case shows its revision and, folded, every history event (approved, legacy snapshot, correction, imported) with reviewer, times, and for a correction who made it, why, and which revision it replaces. A legacy snapshot comes with a note that history overwritten by the old API can't be rebuilt. "Correct the reviewer name" is a separate form (new name, who is correcting, reason, a confirmation box) that sends `expected_revision` from the latest history entry; the normal Approve button still uses `POST /approval`. On `409 approval_changed` the typed fields stay and a reload button refreshes the case and history.
- **Progress and result are separate** everywhere; a finished run that didn't pass says so.
- **Run report reading order:** the original README run and its error, then the patch, then the patched run and the final clean re-run. In each run the README command comes first; the venv and workspace probe are folded into "Environment check", which opens by itself if any of those steps failed. Full argv and logs stay one click away, and the open/closed state survives polling.
- **Patch:** labelled "Manual candidate patch" when `repair_mode` is `manual_candidate`, and "Unverified" unless the result is `verified_fix`. "Download .patch" saves the exact text from the patch endpoint; downloading publishes nothing.
- **Model diagnosis:** when the run's evidence has a `diagnosis` block, the timeline adds a "Model diagnosis" step after the original run: Nano's classification, Ultra's explanation, what happened to the candidate patch (rejected with the reason, or applied), and a call table (stage, returned model ID, tokens, reasoning tokens, time, cost from usage). Costs then come from that ledger and read "(from usage, pending reconciliation)", with "Billed" shown separately (unknown while `billed_usd` is null).
- **Candidate proposal:** when the evidence has `candidate_patch`, the timeline adds "Candidate patch proposal" after the diagnosis: its status (proposed / rejected / applied for validation), a "Not applied" badge unless it was applied, the rejection reason, every edit (path, text to find, replacement), proposed setup steps, and the raw JSON. A model run whose diagnosis says it proposed a candidate but has no `candidate_patch` (runs recorded before proposals were saved, such as `e5150fec…`) says the proposal wasn't kept; nothing is reconstructed.
- **Repair rounds:** when the evidence has `repair_rounds`, the timeline is grouped by round. It shows the original README run first. Then, for each round:
  - a "Repair round n of N" marker with the round's status (verified, applied but failed the check, proposal rejected, no candidate, testing);
  - that round's diagnosis, with only that round's ledger calls (`classify`/`diagnose` are round 1, `*_r2` round 2);
  - the round's own proposal and diff;
  - the attempts whose `round` matches. The same phase may repeat, so the round is part of every fold key.

  Model IDs come from `classification_model` / `diagnostic_model` or the ledger, never from the route name, because in group D both steps are Ultra. A round with no candidate says why (for example "Budget used up"). Engineering test doubles are badged, their rates show as "Test rate", and the cost card says there's no real cost. Records without `repair_rounds` keep the older single-round layout. The state preview also loads the two labelled examples in `web/examples/two-round-*.json`, which never appear in `/api/runs` or results.
- **Recorded data:** a run with `data_origin: "recorded_replay"` gets a "Recorded real run · replay" badge and a note that it isn't a new run.
- **Costs:** `null` shows "unknown"; a model cost of 0 with no model calls reads "$0 (no model calls)"; `billing_status` shows a readable label next to the raw code.
- **Dependencies:** options come from `health.dependency_modes`; `pypi_live` is selectable only when `health.live_pypi_enabled` is true.
- **Quick check (Level 1):** URL and optional 40-character SHA are checked in the browser. An enabled startup API runs the documented install/help workflow; otherwise the page offers local setup steps. `#/try` independently reads public GitHub README commands in the visitor browser without executing them.
- **Evaluation** data checks and metric definitions: [evaluation/README.md](../evaluation/README.md).
- README text, logs, output and diffs are inserted as text, never HTML. Times are UK time (Europe/London) with BST/GMT.

## Read-only demo

A version for judges that needs no backend: the same pages, reading recorded real runs from static JSON.

```sh
python3.12 web/demo/build.py            # writes dist/demo/; --replace rebuilds an earlier demo
python3 -m http.server 3001 --bind 127.0.0.1 --directory dist/demo   # look at it locally
```

- **What goes in:** three registered dev cases, with approval snapshots checked against current acceptance hashes; six real development replays (live PyPI 3, json2yaml offline 1, two separate model trials 2), ten labelled API state examples and the labelled evaluation example. The latest two-round record preserves its rejected proposal and absent second candidate. The two-round engineering examples stay out.
- **What it can't do:** `web/demo/static-client.js` has the same interface as `api/client.js` but refuses every write (`read_only_demo`). With `health.read_only`, the page shows a blue "Read-only demo" banner and replaces the quick check with "Start here" links (chosen by the build in `health.demo_highlights`). There's no approve, correct or run form. The public evaluation page says formal reports are not included; the owner workspace separately has the historical A baseline.
- **Safety check:** the build stops if any output file contains a local path (`/Users/`, `/home/`, `C:\Users\`) or anything shaped like a credential. The one allowed `/home/` is an example path in charset_normalizer's package description, quoted in pip's log. `data/manifest.json` lists every file's SHA-256.
- **Rebuild inputs:** the curated JSON and manual diff live in `web/demo/recordings/` with a hash manifest and are included in the source handoff. Include them in the future public repo. Normal builds need no `reports/`, backend, database, keys, VM or network. `--refresh-recordings` is the explicit owner-only export from existing reports; it makes no model calls. See [build and integrity details](demo/README.md).
- **Holdout runs after the evaluation (B, 2026-10-21):** `--evaluation-records DIR` (repeatable) adds the runs of an evaluation output folder that appear in the formal `evaluation/results.json`. It checks each `report.json` (and `model-ledger.json` for model slots) against its binding's hashes, and stops if anything differs or no formal results file exists. The evaluation table then links each result to its report, and the report shows its evaluation group. Steps: [evaluation/B_RUNBOOK_1024.md](../evaluation/B_RUNBOOK_1024.md).
- Hash routes (`#/runs/…`) work on any static host. Nothing is published by the build itself.

## Checked

- **2026-10-18:** against an isolated API copy, every flow, including the error states.
- **2026-10-19 (redesign):** against A's live API, read-only (no approval or run submitted). All six pages load in Chinese and English; the md2html run report shows the four timeline steps in order; no horizontal scroll at 375 px with every log expanded; the API-down banner and disabled approval appear when `?api=` points nowhere. Banner contrast is 6.5:1 (light) and 7.5:1 (dark).
- **2026-10-21 (B24 beginner front door):** read-only demo build, every beginner route and the moved detailed pages in Chinese and English at 375 and 320 px: no sideways scrolling, no script errors, every page has a heading. Clicked through choose → promise → answer → details and the examples. Against the synthetic QA server only (`web/tests/qa_model_server.py`): the runner-on path shows **Try the instructions**, a refused start shows the API error, and a run seen as queued → running → finished moves through the three stages and then shows its answer (a scratch proxy reported the early reads as running; nothing real was run, no model was called). Help opened with a real click closes with Escape and returns focus. Not checked: Safari, real phones, a screen reader, a real (non-synthetic) run.
- **2026-10-20 (history, correction, candidate):** against scratch-data API instances only (synthetic reviewer names; the real approvals were read, never written). Approve → history shows revision 1; a concurrent correction made outside the page → the form gets `409 approval_changed`, keeps its fields, reloads to revision 2; resubmit → revision 3 with corrector, reason and "Replaces revision 2". An old-style database shows a legacy snapshot with its note. A stale page approving under a second name gets `409 approval_exists` with a reload button. The candidate view was checked with one invented, labelled rejected proposal, and with the recorded model run, which correctly says its proposal wasn't kept. No horizontal scroll at 375 px.
# 自己的 GitHub 入口（A，10/24）

首頁主按鈕和 `#/repo` 現在提供 URL／選填 commit；三個已驗證案例改為另一個入口。本機 API 啟用 `--enable-github-import --enable-runner --enable-live-pypi --enable-startup-checks` 後，表單真的送出 README 安裝／help／version 檢查，不呼叫模型。未啟用或唯讀展示時只產生本機啟動說明與預填連結，不送出執行請求、不冒稱已檢查。支援範圍是小型公開 Python CLI，不是任意語言／Web 服務或任意 repo 自動修好。



B try-own-project整合：公開首頁主入口為 `#/try`（即時讀GitHub README，只有計畫）；本機啟動檢查為 `#/repo`。展示有3個開發案例＋3個邊界案例，真實例子頁另含rich-cli啟動，共7個不同專案；不是7個成功修補。
