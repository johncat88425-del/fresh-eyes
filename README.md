# Fresh Eyes

Check whether a README's first workflow works, then verify a proposed repair twice in clean environments. Built with NVIDIA Nemotron on Nebius Token Factory.

- [Open the free recorded viewer](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/)
- [Download the complete MIT source](fresh-eyes-source.zip)
- [Release file hashes](release-files.json)

This repository distributes the complete Python/JavaScript source, tests, API, runner, and rebuild tools as a ZIP. Extract before running; no provider account or key is needed for tests and the recorded viewer.

```sh
git clone https://github.com/johncat88425-del/fresh-eyes.git fresh-eyes-release
cd fresh-eyes-release
python3.12 -m zipfile -e fresh-eyes-source.zip source
cd source/fresh-eyes
python3.12 web/demo/build.py
PYTHONPATH=src python3.12 -B -m unittest discover -s tests
node --test web/tests/*.test.mjs
python3.12 -m http.server 3001 --bind 127.0.0.1 --directory dist/demo
```

Needs Python 3.12 and Node 18+. The integrated source passed 349 Python and 43 frontend engineering tests with zero skips. Its exact teammate ZIP also passed a fresh Linux container check on the same physical Mac, without network, host mounts or credentials. Another physical computer remains untested. A new repository execution needs a separately prepared Linux Docker environment; paid model use needs explicit local activation and a budget.

## Try your own GitHub repository

The public homepage opens `#/try`: a visitor can paste a GitHub URL, read its pinned README, see installation/help commands with line numbers, and learn why an ambiguous workflow stops. This is a browser-only document lookup, not a repository execution or AI call. A separate local `#/repo` form accepts a URL and optional commit. The local application really installs and starts supported public Python CLI projects from their README; it is not restricted to the three registered examples. A browser-submitted Textualize/rich-cli check is retained in the source with startup_only status and zero model calls.

The Pages planner can prepare a prefilled local execution link; it has no execution server. Seven different repositories are represented across the gallery, including three boundary baselines and a startup-only project; this is not seven repaired projects. After preparing Linux Docker, start the API with `python3.12 scripts/fresh-eyes serve --enable-github-import --enable-runner --enable-live-pypi --enable-startup-checks`, then run the frontend in another terminal. On the supplied Lima setup, append `--docker scripts/docker-local`. These commands enable no model. Ambiguous READMEs stop for input. Startup success does not imply correct functional output or an AI repair.

## What the viewer demonstrates

Twenty-three real development records, three registered development cases plus three reviewed boundary cases, and ten separately labelled synthetic state examples. Opening the site runs no repository, backend or model.

**A real uninterrupted Nano-to-Ultra run repaired the md2html README.** The original README workflow failed. Nano classified the failure, Ultra proposed replacing the install step with `pip install .`, and the patch passed a fresh acceptance run and an independent clean re-run in the same pipeline. No manual patch edit or saved candidate was used. This run made two real model calls with reported usage calculated at US$0.00546874; the bill is unreconciled. It made no Tavily call.

- [Uninterrupted real AI repair](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/runs/5c225204bcd946228a7ef4700c8c0c8c)
- [Same batch: missing-input misclassification](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/runs/2978ec943b5e4bc3ae5d82a0d9cdb4e2)
- [Same batch: rejected setup and later classification stop](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/runs/0c0141f29d584085ba26c2b9050d593c)

All three development attempts are retained: six calls, known usage estimate US$0.01295260. They are not a formal success rate; generic evidence context changed between attempts 1 and 2.

An earlier unchanged Nemotron proposal also passed separate clean verification after its original trial stopped on our runner error. The viewer preserves that interruption and links its original model ledger. This earlier verification made no new model call and is separate from the new uninterrupted run.

- [Earlier saved AI proposal verification](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/runs/5ac511039adb4977b32ef129e86ee006)
- [Its original interrupted trial and ledger](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/runs/1a5f5135c92f4d15a5ff6dabe0a78b64)
- [Ultra timeout retained with unknown usage](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/runs/18dd07f0164142fc80df20d362c7a86b)

A manual patch passed on the same repository. These records still represent only one distinct repaired project. Its defect is an omitted package installation step, not observed dependency drift. Boundary baselines and startup-only checks are excluded from repair rates. Formal four-case evaluation has not run.

Across twelve repair development trials, twenty-three repair calls were dispatched; twenty-two have reported usage totalling US$0.06203302. A separate Nano maintainer-note draft used US$0.00006552. One older timed-out Ultra call has unknown usage and retains a US$0.029654 reservation; it was not retried. Usage estimates and reservations are not reconciled bills. All paid execution is off and the new three-attempt grant is closed. Three diagnosis-time Tavily searches and one component check used four basic credits. A team-written, human-reviewed md2html issue has been posted upstream (https://github.com/neobuilding/md2html/issues/1); no maintainer adoption is claimed. The model-generated maintainer-note draft has not been posted as-is.

The single HTML viewer embeds validated recordings and has no network fallback for replay records. Only an explicit visitor submission to the README planner makes anonymous, credential-free GitHub reads; it does not run code. Keys, private account state and databases are excluded. Publication is separate from source generation; dated source notes may describe an unpublished local candidate at build time. No hosted live model service, nightly monitoring or Token Factory Sandboxes execution is claimed.
