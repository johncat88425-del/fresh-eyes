# Fresh Eyes

Check whether a README's first workflow works, then verify a proposed repair twice in clean environments. Built with NVIDIA Nemotron on Nebius Token Factory. MIT licensed.

- [Try your public GitHub README](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/try): reads a pinned README and cites commands; no new code execution or model call.
- [See the real uninterrupted AI repair](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/runs/5c225204bcd946228a7ef4700c8c0c8c).
- [Current development video](https://youtu.be/UnVHDBiDfKY): one development repository, not a formal evaluation score.

## Clone, build and test

Needs Python 3.12+ and Node.js 18+. No Python packages, account or API key are needed for the tests and recorded viewer.

```sh
git clone https://github.com/johncat88425-del/fresh-eyes.git
cd fresh-eyes
python3.12 web/demo/build.py --replace
PYTHONPATH=src python3.12 -B -m unittest discover -s tests
node --test web/tests/*.test.mjs
python3.12 -m http.server 3001 --bind 127.0.0.1 --directory dist/demo
```

Open http://127.0.0.1:3001/. Build before frontend tests so the generated-asset checks run. This working tree includes the application, tests and byte-identical replay data already in the public v8 archive. It excludes internal handoff documents, raw report folders, account state and new private evidence. The earlier complete source archive remains available as `fresh-eyes-source.zip`.

## Run the local application

In two terminals, run `python3.12 scripts/fresh-eyes serve` and `python3.12 web/serve.py 5173`. Open http://127.0.0.1:5173/. Defaults enable no repository execution or paid provider and read no model key.

With a prepared Linux Docker engine and Git, use `python3.12 scripts/fresh-eyes serve --enable-github-import --enable-runner --enable-live-pypi --enable-startup-checks`, then open http://127.0.0.1:5173/#/repo. Supported scope: small public Python CLIs with an unambiguous pip install and help/version command in their README. Ambiguous projects stop for a human choice. Startup success is not functional verification or an AI repair.

## Manual cloud check

[Fresh Eyes check](https://github.com/johncat88425-del/fresh-eyes/actions/workflows/fresh-eyes-check.yml) runs the same bounded README startup check on a standard public GitHub runner, without provider secrets or models. A maintainer must dispatch it; alternatively use your own public fork. There is no issue trigger or automatic comment. Check the actual workflow result before treating anything as executed. Public Pages itself has no execution backend.

## Evidence and limits

The v8 replay data contains 23 real records and ten labelled synthetic examples. It represents one distinct AI-repaired development repository, md2html. The original failure, new Nano/Ultra patch, candidate checks and independent fresh checks belong to the same uninterrupted run. No human edited its patch. Two calls cost US$0.00546874 when calculated from reported usage; billing is unreconciled. Failed and interrupted attempts stay visible.

The four original formal A cases ran on 25 October and all already passed without models. B/C/D have not run; those healthy cases do not demonstrate repair gain. Their new raw evaluation capture is kept in the owner workspace and is not silently included in this source publication. Unknown usage remains reserved; paid execution is off.

A human locks functional inputs and expected outputs before repair. Nano classifies; Ultra diagnoses and proposes limited README/dependency edits. Optional Tavily results are untrusted evidence. The model cannot change acceptance. Only a validated candidate passing both fresh functional runs becomes verified_fix. Third-party code runs non-root with resource limits, no host mounts and controlled PyPI-only egress in live mode.

No hosted live AI repair, nightly monitoring, Token Factory Sandboxes execution, broad repair success rate or maintainer adoption is claimed.

## Use Fresh Eyes in your own CI

The reusable Action checks the README of your own checkout, without an API key or model call:

```yaml
name: README still works
on: [push, pull_request]
jobs:
  readme:
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - uses: johncat88425-del/fresh-eyes@dfcac778f241dc598aea1fe2597d7a254d7154ee
```

Pin a tested commit for a stable integration. Needs Python 3.12+ and Linux Docker on the runner. Public GitHub standard runners are free; private or custom runners depend on your own plan. A documented startup failure returns exit 1 and fails CI; runner failure returns 2. Ambiguous READMEs run nothing and are reported separately (`fail-on: unresolved,needs_input` can fail those too). No repair is attempted.

Locally, with Docker: `python3 scripts/readme-check.py /path/to/your/checkout --out /tmp/a-new-fresh-eyes-check`. Use a new output directory each time. Known local credential/runtime files are excluded. Real Docker checks of labelled working/broken engineering fixtures passed; they are tests of the tool, not additional repaired projects. See the workflow runs for independent cloud verification.

[Real CI demo: one expected green pass and one expected red failure](https://github.com/johncat88425-del/fresh-eyes-action-demo). These are labelled team-written fixtures, not additional real-world repair successes.
