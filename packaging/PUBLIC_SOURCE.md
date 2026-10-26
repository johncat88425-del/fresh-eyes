# Public source preparation

This prepares a local ZIP candidate. It does not create a repository, push code or publish a site.

The owner first builds the complete private teammate handoff from the original recorded evidence, then exports a new public candidate:

```sh
python3.12 scripts/package-handoff.py
python3.12 scripts/package-public-source.py \
  --output dist/fresh-eyes-public-source-new.zip
```

Choose a new output filename each time. The public exporter verifies the handoff manifest and every original file before preparing the candidate. It refuses unexpected host paths in code, omits JSON support metadata containing concrete local home/temp paths, and redacts such paths in documentation. It never rewrites an execution report JSON, model ledger, binding or patch to make it appear newly valid. Original omitted metadata remains in the owner's workspace.

`PUBLIC_EXPORT.md` explains the scope. `public-source-manifest.json` lists every retained file hash, each omitted file's original hash and reason, and original/exported hashes for transformed documents and links. Links to withheld metadata point to the explanation. The derived `.gitignore` allows exactly the retained public report files into Git; future local run directories, credentials and databases remain ignored. This prevents a clone from silently losing the reports that the source ZIP includes.

Extract into a new directory, then check it without `.env`, VM or API credentials:

```sh
python3.12 web/demo/build.py --replace
PYTHONPATH=src python3.12 -B -W error::ResourceWarning -m unittest discover -s tests -q
node --test web/tests/*.test.mjs
python3.12 -m http.server 3001 --bind 127.0.0.1 --directory dist/demo
```

The demo needs only versioned public recordings and the observed approval snapshot. Python tests use synthetic repositories and model doubles. Running the API normally also leaves paid models and repository execution disabled. Real repository execution requires a separately prepared Docker environment; follow the root README rather than assuming Chris's Lima tooling exists on another computer.

Before a public release, inspect the candidate and its manifest, complete the actual human reviews/evaluation, and verify the final repository, demo and video access. None of those actions is performed by these packaging commands.
