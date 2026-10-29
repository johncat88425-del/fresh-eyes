# Nightly README installation and startup monitoring

Fresh Eyes includes a daily GitHub Actions monitor. It reads the current default-branch README of Textualize/rich-cli, pins that source commit, installs through PyPI-only egress in a fresh non-root Linux container, and runs the README's unambiguous help/version command. No Nebius, Tavily or other model key is loaded. No repair or functional-output verification is attempted.

- [Public execution history](https://johncat88425-del.github.io/fresh-eyes/nightly.html?lang=en) reads GitHub's current public workflow metadata. It never dispatches a run.
- [Workflow and all runs](https://github.com/johncat88425-del/fresh-eyes/actions/workflows/nightly-readme.yml) show the actual event and job summaries.
- The daily schedule is **02:17 Europe/London**. GitHub may delay or skip a scheduled run; public-repository schedules can be disabled after 60 days without activity. See [GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

The workflow can also be dispatched manually. A manual validation run is labelled `manual`, never `scheduled`. The history page says no scheduled execution has been observed until an actual `schedule` event appears. If GitHub metadata is unavailable, the page reports that uncertainty rather than inventing a result.

## Use it in your own public repository

1. Copy [the maintainer template](../examples/nightly-readme.yml) into `.github/workflows/nightly-readme.yml` on your default branch.
2. Keep the tested Fresh Eyes implementation pinned. Switch to the immutable submission tag only after that version has been released and tested.
3. Enable Actions and manually run the workflow once. Inspect the summary and artifact before relying on the schedule.
4. Check the Actions history after the first scheduled time. Installing only a workflow file does not prove a scheduled execution happened.

The template checks your own checkout using the reusable Action. Its scope is small Python CLIs with one supported pip installation and one explicit help/version command in README code blocks. Ambiguous instructions stop as `needs_input`; they are never guessed. Default behavior fails the workflow for any result that does not establish successful startup. Container execution uses read-only repository permissions and no persisted checkout credentials. No issue, email or maintainer message is sent by Fresh Eyes.

Standard public GitHub-hosted runners are used by this project's demonstration. This setup does not create a paid hosting account, add a payment method, use a larger runner or enable any provider. GitHub's own account notification preferences remain under the account owner's control. If you adapt it to private/custom runners, check your own plan first.

## Reports and outcomes

Every central-monitor run preserves its own artifact for 90 days, subject to repository retention settings. Downloading artifacts may require GitHub sign-in. Workflow history and job summary links remain separate from the static replay viewer.

- `monitor.json`: actual event, timestamps, outcome, source commit, Fresh Eyes workflow commit and the pinned base image. `functional_output_verified` and `repair_attempted` are false.
- `result.json`: startup result, documented commands and available process output.
- `report/report.json` and `report/report.html`: full execution evidence when the startup path produced it. The worker captures `pip_freeze` when execution reaches that stage; an early failure may have no dependency inventory.
- `summary.md`: human-readable result. `manifest.json` lists SHA-256 for all saved evidence files except itself.

The monitor returns 0 only for `startup_only` with recorded execution; 1 for a README failure or an unsupported/ambiguous check; 2 for an infrastructure error or inconsistent evidence. A red workflow by itself is not a diagnosis that the README broke. Read the report's outcome.

Changes to the README commit and changes to installed packages are both possible between runs. A different outcome alone does not prove dependency drift. Repeated success is not evidence of functional correctness, an AI repair or an AI success rate.

The application API still reports `nightly_checks: false`: it does not schedule the registered cases' locked functional acceptance. This separate monitor is a GitHub Actions installation/startup feature.
