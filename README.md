# Fresh Eyes

Check whether the README's first workflow actually works, then verify any repair
again in a clean environment. Built with NVIDIA Nemotron on Nebius Token Factory.

- [Open the free recorded viewer](https://johncat88425-del.github.io/fresh-eyes/?lang=en#/)
- [Download the complete MIT source and local test build](fresh-eyes-source.zip)
- [Release file hashes](release-files.json)

The repository contains a portable viewer and a verified source archive. The ZIP
contains the complete source tree, tests, local API/frontend, runner and rebuild
scripts, rather than a compiled binary. Internal collaboration notes, account
support drafts, keys and databases are excluded. Extract the archive first; the
commands below clone and extract it before testing.

```sh
git clone https://github.com/johncat88425-del/fresh-eyes.git fresh-eyes-release
cd fresh-eyes-release
python3.12 -m zipfile -e fresh-eyes-source.zip source
cd source/fresh-eyes
PYTHONPATH=src python3.12 -B -m unittest discover -s tests
python3.12 web/demo/build.py
node --test web/tests/*.test.mjs
python3.12 -m http.server 3001 --bind 127.0.0.1 --directory dist/demo
```

The B24 source passed 235 Python and 27 frontend engineering tests after clean
extraction on the owner's Mac. A preceding B22 build also passed an isolated Linux
check on the same Mac; this B24 frontend has not repeated that Linux check.
Another physical machine remains untested. Python 3.12 and Node are prerequisites; tests and viewer rebuild need no
provider account or API key. A new repository execution needs Linux Docker.
Real model execution also needs explicit local activation and an owner key/budget.

The viewer opens with the B24 beginner flow; Detailed view opens the complete reports.

## What is actually demonstrated

The free viewer replays nine development records from three distinct repositories.
It runs no backend, repository or model. Only a **manual** README patch has passed
both clean verification runs. No AI-generated patch has passed yet. The latest
Nano→Ultra run correctly classified the defect but stopped on incomplete model
output; both the failure and usage are visible. Development replays are not formal
holdout scores. The four-case holdout evaluation is not yet executed.

The observed md2html defect is an omitted package-install step, not demonstrated
dependency drift. Usage-based cost estimates remain separate from actual bills.
Default local API startup disables paid calls and repository execution.

The single HTML viewer is rebuilt by `web/demo/single_file.py`. It embeds the
same validated recordings and interface, with no network API fallback. Source
export manifests describe the build step; publication is recorded separately.
