<!-- For the Devpost "testing instructions" field and the public README. Before submitting, select
     the final fixed release tag, check the run IDs still exist in the published build,
     and update "What we do not claim" if the formal evaluation produced new results. B, 2026-10-21. -->
# Testing Fresh Eyes

**Watch the public 2:52 development demonstration:** https://youtu.be/UnVHDBiDfKY. English narration and uploaded English captions; eight timestamp links. It shows one development repository (md2html), not a formal evaluation score.

The recorded viewer and local engineering tests in steps 1–2 are free and need no provider account or key. Optional live model repair in step 3 requires a separately activated, paid provider key.

## 1. See real recorded checks (2 minutes, nothing to install)

Open **https://johncat88425-del.github.io/fresh-eyes/**.

- Press **Explore recorded real checks first**, pick a project, then press **See the result**. Or press **See a real example first**, or **Check your own GitHub project** to read any public repository's README (see below).
- **Detailed view** (top right; at the bottom of the page on phones) shows the full evidence: every command, log, output check, patch, and each Nemotron call with its tokens and usage-based cost.
- Seven runs worth opening:
  - md2html: the README as written fails. The `md2html` command is never installed. https://johncat88425-del.github.io/fresh-eyes/#/runs/10175eee750c464fa7ce86ad4c84f037
  - md2html: **an uninterrupted Nano→Ultra repair** (`pip install .`), followed by candidate and independent clean verification in the same run, with no human patch editing or saved proposal reuse. 2 model calls / US$0.00546874 from usage. https://johncat88425-del.github.io/fresh-eyes/#/runs/5c225204bcd946228a7ef4700c8c0c8c
  - md2html: **an earlier saved README proposal written by Nemotron**, verified on 24 October. Nano classified the failure, Tavily looked up the project's docs, and Ultra proposed a line-anchored patch (`pip install -e .`). The unchanged patch passed in a fresh environment and again in an independent clean re-run. Its original trial was interrupted by an error in our own runner, so the same patch was re-checked without a new model call; the page says so and links the original trial with its calls and costs. https://johncat88425-del.github.io/fresh-eyes/#/runs/5ac511039adb4977b32ef129e86ee006
  - md2html: a one-line README fix (`pip install .`), written by a person, passed in two fresh environments. https://johncat88425-del.github.io/fresh-eyes/#/runs/563db9062b7b4e48a4490e41b6792ff6
  - md2html: Nemotron diagnosed the same failure over two repair rounds. Nano classified it and Ultra explained it, but no proposal could be applied safely, so the run stopped and asks a person. https://johncat88425-del.github.io/fresh-eyes/#/runs/e9a04f641acb4c71b7ff0932e58f87e8
  - envdoctor: the first advertised `pip install envdoctor` failed because PyPI had no distribution; Ultra changed it to `pip install .`, and both human-approved functional inputs passed twice. The README already offered source installation. This N=1 challenge was locked before execution but before the full scan finished; it is not a formal score. https://johncat88425-del.github.io/fresh-eyes/#/runs/2c3377fca8be40e69ce3d1994a0f15ab
  - mistletoe: the README already works. https://johncat88425-del.github.io/fresh-eyes/#/runs/a7bf8fc4969e44ec9436ea79cc7cee00

This site includes 24 real replay records from 19–26 October 2026, plus separately labelled synthetic state examples. The 26 October envdoctor refresh is published. It does not run anything new and calls no model: a recording is not a live run.

## 2. Run it yourself, no key needed (about 10 minutes)

Needs Python 3.12 and Node.js 18 or newer. No other packages.

```sh
git clone https://github.com/johncat88425-del/fresh-eyes.git fresh-eyes-release
cd fresh-eyes-release
# Preliminary candidate: main; choose a fixed final tag before submission.
python3.12 web/demo/build.py --replace
PYTHONPATH=src python3.12 -B -m unittest discover -s tests
node --test web/tests/*.test.mjs
```

The build rebuilds the same viewer into `dist/demo/` from the versioned recordings; the web tests then check it too. To open it, run `python3 -m http.server 3001 --directory dist/demo` and go to http://localhost:3001/.

To use the local app with its API (execution is off and no keys are read), use two terminals:

```sh
python3.12 scripts/fresh-eyes serve
python3.12 web/serve.py
```

Then open http://localhost:5173/.

## 3. Optional: run real README checks (advanced)

Running a third-party repository needs a container runtime. The project owner has tested local runs on macOS and the reusable startup Action on GitHub-hosted Linux. For the macOS VM setup use `python3.12 scripts/start-local-runtime.py`, then `python3.12 scripts/run-development.py --case mistletoe`; see the root README. These runs follow the README only and call no model.

Model repair (NVIDIA Nemotron on Nebius Token Factory) needs your own Nebius API key, is billed to your account, and must be switched on explicitly through an owner-armed, bounded plan. The recorded model runs in step 1 include the saved calls; they do not demonstrate a new live repair. Free hosted live model access is not provided in this candidate.

## What we do not claim

- There is no hosted execution service. The website offers recorded checks and browser-only README planning.
- Run 5c225204… is an uninterrupted real development repair. The older 5ac51103… is a separately verified saved proposal after a runner interruption. Both concern the same md2html repository. The later envdoctor repair is a second distinct repository. Earlier failures remain visible; they are not formal evaluation scores.
- The four-case formal A baseline ran on 25 October: all four original READMEs already passed, with zero model calls. B/C/D have not run; no formal AI repair success rate is claimed. An earlier Ultra timeout has no per-request response; its application-budget reconciliation is recorded separately from the original failed run.
## Bring your own repository

The public homepage has a primary **Check your own GitHub project** action (`#/try`, live browser-only README planning); the local application also offers real startup checks (`#/repo`), separate from the four registered development functional cases. The public Pages form reads the pinned GitHub README with command line citations and prepares a local testing link; Pages itself does not execute new repositories. After cloning the repository and preparing Linux Docker, run the API with `--enable-github-import --enable-runner --enable-live-pypi --enable-startup-checks`, then `python3.12 web/serve.py 5173` in another terminal. Open `http://127.0.0.1:5173/#/repo` and paste a public GitHub URL. No model key is required.

The current scope is small Python CLI repositories with explicit README pip installation and help/version commands. An ambiguous README returns a needs-input report. A successful startup check does not mean functional output was verified or an AI patch was produced. A real Textualize/rich-cli startup check is published as run `ebd915311a8f4f4d9b2373afdc718c9f` (https://johncat88425-del.github.io/fresh-eyes/#/runs/ebd915311a8f4f4d9b2373afdc718c9f), outside the registered functional cases, with zero model calls. A publicly hosted execution backend is still an outstanding delivery limitation.



## Check a repository with the GitHub Action

Open https://github.com/johncat88425-del/fresh-eyes-action-demo/actions . The green working run is 40249414162; the expected red run is 40249414171. The red summary shows `shout --help` failing with exit 127 because installing only requirements never installed the command. These are two team-written engineering fixtures, not extra repaired projects.

To use the Action in your own public repository, follow the current root README and pin implementation `dfcac778f241dc598aea1fe2597d7a254d7154ee` (or the final submission tag). Needs a Linux runner with Python 3.12+ and Docker. No API key or model is needed. This checks installation and startup, not functional output or AI repair.

The current source tree is directly browsable at the repository root (step 2 uses it). `fresh-eyes-source.zip` in the repository is an older v8 archive; do not use it for testing. Exact public source commit 8723eb25 passed 371 Python and 44 frontend tests on an independent GitHub host: https://github.com/johncat88425-del/fresh-eyes/actions/runs/40250425935 .
