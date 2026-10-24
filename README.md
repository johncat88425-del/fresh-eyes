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

Needs Python 3.12 and Node 18+. The candidate passed 335 Python and 37 frontend engineering tests with zero skips. Its teammate source also passed a fresh Linux container check on the same physical Mac, without network, host mounts or credentials. Another physical computer remains untested. A new repository execution needs a separately prepared Linux Docker environment; paid model use needs explicit local activation and a budget.

## What the viewer demonstrates

Twenty real development records, three registered development repositories, and ten separately labelled synthetic state examples. Opening the site runs no repository, backend or model.

**One unchanged Nemotron-generated md2html proposal passed separate clean verification.** Nano classified the failure; Tavily retrieved the project's README; Ultra proposed the README patch. The original model trial stopped on an error in our own runner. We kept that failure and verified the exact saved proposal without another model call: the original workflow failed, the patched workflow passed, and an independent fresh run passed. The linked records disclose this interruption. This is not an uninterrupted end-to-end success or a formal evaluation score.

- [Verified saved AI proposal](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/runs/5ac511039adb4977b32ef129e86ee006)
- [Original model trial and its ledger](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/runs/1a5f5135c92f4d15a5ff6dabe0a78b64)
- [Later Ultra timeout, retained with unknown usage](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/runs/18dd07f0164142fc80df20d362c7a86b)

A manual patch also passed on the same md2html repository; these are not two distinct repaired projects. Its defect is an omitted package installation step, not observed dependency drift. Boundary baselines and startup-only checks are excluded from repair rates. Formal four-case evaluation has not run.

Seventeen repair calls were dispatched; sixteen have reported usage totalling US$0.04908042. A separate Nano maintainer-note draft used US$0.00006552. One timed-out Ultra call has unknown usage and retains a US$0.029654 reservation; no retry or fallback followed it. Usage estimates and reservations are not reconciled bills. Further paid dispatch is locked. Three diagnosis-time Tavily searches and one component check used four basic credits. No maintainer note has been sent.

The single HTML viewer embeds validated recordings and has no network API fallback. Keys, account state and databases are excluded. Publication is separate from source generation; dated source notes may describe this as an unpublished local candidate at build time. No hosted live model service, nightly monitoring or Token Factory Sandboxes execution is claimed.
