"""Curated public replay inputs; never load keys, execute repos or call a provider."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from fresh_eyes.contracts import digest

FORMAT = "fresh-eyes-public-recordings-v1"
FORBIDDEN = [re.compile(p) for p in (
    r"/Users/", r"/home/(?!default/projects/charset_normalizer/)", r"[A-Za-z]:\\Users\\",
    r"NEBIUS_API_KEY\s*=", r"\bsk-[A-Za-z0-9]{8,}", r"Bearer\s+[A-Za-z0-9._-]{8,}",
    r"api[_-]?key\"?\s*[:=]\s*\"[^\"]{8,}")]
RUN_ID = re.compile(r"[a-f0-9]{32}")


def checked_text(raw, label):
    text = raw.decode("utf-8")
    if any(pattern.search(text) for pattern in FORBIDDEN):
        raise ValueError("Possible local path or credential in public artifact: " + label)
    return text


def validate_record(record):
    ev = record.get("evidence") or {}
    if record.get("recording_scope") == "boundary_baseline" and (
        record.get("counted_in_fix_rate") is not False or ev.get("model_calls") != 0 or
        record.get("repair_mode") != "none" or record.get("patch_available") or
        record.get("result_status") in {"verified_fix", "already_passes"}):
        raise ValueError("Boundary baseline is not a model repair or repair-rate result")
    startup = record.get("verification_level") == "startup"
    if startup:
        if (ev.get("data_origin") != "github_startup_execution" or ev.get("verification_level") != "startup" or
            ev.get("executed") is not True or record.get("case_id") is not None or ev.get("case_id") is not None or
            record.get("acceptance_sha256") is not None or ev.get("acceptance_sha256") is not None or
            ev.get("human_approval_granted") is not False or record.get("review", {}).get("status") != "draft" or
            record.get("result_status") not in {"startup_only", "unresolved", "infrastructure_error"} or
            record.get("repair_mode") != "none" or record.get("patch_available") or ev.get("model_calls") != 0):
            raise ValueError("Startup replay cannot assert functional approval, repair or model use")
    if (not RUN_ID.fullmatch(record.get("run_id", "")) or record.get("data_origin") != "execution" or
        record.get("run_state") not in {"completed", "failed"} or
        ev.get("data_origin") in {"example", "engineering_test"} or ev.get("environment", {}).get("test_double") or
        not RUN_ID.fullmatch(ev.get("run_id", "")) or
        any(record.get(k) != ev.get(k) for k in ("case_id", "acceptance_sha256")) or
        (not startup and not re.fullmatch(r"[a-f0-9]{64}", record.get("acceptance_sha256", ""))) or
        record.get("result_status") != ev.get("status") or not ev.get("finished_at")):
        raise ValueError("A public replay must match completed real development evidence")
    ledger = ev.get("model_ledger") or (ev.get("diagnosis") or {}).get("ledger") or {}
    calls = ledger.get("calls", [])
    actual = sum(bool(c.get("dispatch_started_at")) for c in calls)
    if (any(c.get("data_origin") != "nebius_api" for c in calls) or ledger.get("engineering_test_calls", 0) or
        actual != ev.get("model_calls", 0) or (ledger and ledger.get("model_calls") != actual)):
        raise ValueError("Replay model evidence contains synthetic or inconsistent calls")


def model_fix_highlights(records):
    """A later repo adds a story; another case in one repo does not add a repo."""
    newest = {}
    for record in records:
        repo = (record.get("repo_url") or record["case_id"]).rstrip("/").removesuffix(".git").casefold()
        if (record.get("finished_at") or "") >= (newest.get(repo, {}).get("finished_at") or ""):
            newest[repo] = record
    return [{"key": "model_fix" if i == 0 else "model_fix_more", "run_id": record["run_id"]}
            for i, record in enumerate(sorted(newest.values(), key=lambda r: r.get("finished_at") or ""))]


def collect(project):
    """Owner-only refresh from a fixed whitelist of existing recorded results."""
    project = Path(project)
    files, rows, patches, sources = {}, [], [], []
    def source(rel):
        path = project / rel
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing ordinary recording input: " + rel)
        raw = path.read_bytes()
        checked_text(raw, rel)
        sources.append({"path": rel, "sha256": hashlib.sha256(raw).hexdigest()})
        return raw
    def add_record(record, raw=None):
        validate_record(record)
        run_id = record["run_id"]
        if any(row["run_id"] == run_id for row in rows):
            raise ValueError("Duplicate replay run ID")
        rel = "runs/" + run_id + ".json"
        raw = raw if raw is not None else (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
        checked_text(raw, rel)
        files[rel] = raw
        # Older API jobs and engine executions have separate IDs; preserve both.
        rows.append({"run_id": run_id, "evidence_run_id": record["evidence"]["run_id"], "file": rel,
                     "case_id": record["case_id"], "result_status": record["result_status"]})
    live = "reports/live-pilot-20261019-88770a71/"
    for name in ("md2html-original", "md2html-manual-candidate", "mistletoe-original"):
        raw = source(live + name + ".json")
        add_record(json.loads(raw), raw)
    diff = source(live + "md2html-manual-candidate.diff")
    fixed = next(row for row in rows if row["result_status"] == "verified_fix")
    files["patches/" + fixed["run_id"] + ".diff"] = diff
    patches.append({"run_id": fixed["run_id"], "file": "patches/" + fixed["run_id"] + ".diff"})
    raw = source("reports/offline-pilot-json2yaml-dc16deb4/json2yaml-original.json")
    add_record(json.loads(raw), raw)
    old = json.loads(source("reports/model-pilot-20261019-once/report.json"))
    old_ledger = json.loads(source("reports/model-pilot-20261019-once/model-ledger.json"))
    if (old["diagnosis"]["ledger"]["calls"] != old_ledger["calls"] or old["model_calls"] != old_ledger["model_calls"]):
        raise ValueError("Original model report and ledger disagree")
    add_record({"schema_version": 1, "data_origin": "execution", "record_source": "owner_local_engine",
        "api_persisted": False, "evaluation_eligible": False, "run_id": old["run_id"], "case_id": old["case_id"],
        "repo_url": old["repo"]["url"], "commit_sha": old["repo"]["commit"], "run_state": "completed",
        "result_status": old["status"], "phase": old.get("current_phase"), "reason": old.get("reason"),
        "repair_mode": "model", "repair_author": old.get("repair_author"), "patch_available": False,
        "verification_level": "functional", "dependency_mode": old.get("dependency_mode"),
        "acceptance_sha256": old["acceptance_sha256"], "review": old["review"],
        "created_at": old["started_at"], "started_at": old["started_at"], "finished_at": old["finished_at"], "evidence": old,
        "costs": {"currency": "USD", "model_calls": old_ledger["calls"],
                  "model_usd": float(old_ledger["calculated_usd"]) if old_ledger["calculated_usd"] is not None else None,
                  "billed_usd": None, "runner_usd": None, "billing_status": "usage_calculated_pending_reconciliation",
                  "paid_calls_enabled": False}})
    latest, model_fixes = None, []
    for record_path in sorted((project / "reports").glob("model-trial-*/run-record.json")):
        trial = record_path.parent.relative_to(project).as_posix() + "/"
        raw = source(trial + "run-record.json")
        record = json.loads(raw)
        report_raw = source(trial + "report.json")
        binding = json.loads(source(trial + "trial-binding.json"))
        if (record["evidence"] != json.loads(report_raw) or binding.get("scope") != "development" or
            binding.get("evaluation_eligible") is not False or binding.get("run_state") != record.get("run_state") or
            binding.get("run_id") != record.get("run_id") or binding.get("result_status") != record.get("result_status") or
            hashlib.sha256(report_raw).hexdigest() != binding.get("report_sha256")):
            raise ValueError("Development trial binding, report or identity changed")
        ledger_path = project / trial / "model-ledger.json"
        if ledger_path.is_file():
            ledger_raw = source(trial + "model-ledger.json")
            if (hashlib.sha256(ledger_raw).hexdigest() != binding.get("ledger_sha256") or
                record["evidence"].get("model_ledger") != json.loads(ledger_raw)):
                raise ValueError("Development trial model ledger changed")
        elif record["evidence"].get("model_calls") != 0:
            raise ValueError("Development trial has model calls but no ledger")
        add_record(record, raw)
        if record.get("patch_available"):
            diff = source(trial + "candidate.diff")
            if hashlib.sha256(diff).hexdigest() != record["evidence"].get("patch", {}).get("sha256"):
                raise ValueError("Development trial patch differs from its evidence")
            name = "patches/" + record["run_id"] + ".diff"
            files[name] = diff
            patches.append({"run_id": record["run_id"], "file": name})
        if record["result_status"] == "verified_fix" and record.get("repair_author") == "model":
            attempts = record["evidence"].get("attempts", [])
            if (record.get("repair_mode") != "model" or record["evidence"].get("model_calls", 0) < 1 or
                    len(attempts) < 3 or [a.get("phase") for a in attempts[-2:]] != ["candidate_patch", "fresh_verification"] or
                    any(a.get("status") != "passed" for a in attempts[-2:])):
                raise ValueError("A model fix highlight needs candidate and independent fresh verification")
            model_fixes.append(record)
        if record_path.parent.name == "model-trial-md2html-20261020-003-once":
            latest = record
    if latest is None:
        raise ValueError("The previously published two-round model trial is missing")
    progress_path = project / "evidence/real-progress-20261024.json"
    if progress_path.is_file():
        proof = json.loads(source("evidence/real-progress-20261024.json"))
        def report(rel, expected):
            raw = source(rel)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("Verified development report changed: " + rel)
            return json.loads(raw)
        def recorded(ev, scope):
            startup = scope == "startup"
            repo = ev["repo"]
            return {"schema_version": 1, "data_origin": "execution", "record_source": "owner_local_engine",
                "api_persisted": False, "evaluation_eligible": False, "recording_scope": scope,
                "counted_in_fix_rate": False, "run_id": ev["run_id"], "case_id": ev.get("case_id"),
                "case_available": scope == "development", "repo_url": repo.get("url", repo.get("repo_url")),
                "commit_sha": repo.get("commit", repo.get("commit_sha")), "run_state": "completed",
                "result_status": ev["status"], "phase": "finished", "reason": ev.get("reason"),
                "repair_mode": "manual_candidate" if ev.get("repair_author") == "manual" else "none",
                "repair_author": ev.get("repair_author"), "patch_available": bool(ev.get("patch")),
                "verification_level": "startup" if startup else "functional", "dependency_mode": "pypi_live",
                "acceptance_sha256": ev.get("acceptance_sha256"), "review": ev.get("review", {"status": "draft"}),
                "created_at": ev["started_at"], "started_at": ev["started_at"], "finished_at": ev["finished_at"],
                "evidence": ev, "costs": {"currency": "USD", "model_calls": [], "model_usd": 0.0,
                    "billed_usd": None, "runner_usd": None, "billing_status": "local_compute_not_metered",
                    "paid_calls_enabled": False}}
        startup_name = "reports/github-rich-cli-startup-20261024-v1/report.json"
        ev = report(startup_name, proof["report_hashes"][startup_name])
        if ev["run_id"] != proof["startup"]["run_id"]:
            raise ValueError("Startup evidence identity changed")
        add_record(recorded(ev, "startup"))
        boundary_name = "reports/boundary-baseline-20261024-v1/boundary-results.json"
        boundary = report(boundary_name, proof["report_hashes"][boundary_name])
        if boundary.get("counted_in_fix_rate") is not False or boundary.get("model_calls") != 0:
            raise ValueError("Boundary baseline cannot enter repair rates or model claims")
        for row in boundary["cases"]:
            ev = report("reports/boundary-baseline-20261024-v1/" + row["case_id"] + "/report.json", row["report_sha256"])
            if (ev["run_id"] != row["run_id"] or ev["case_id"] != row["case_id"] or ev["status"] != row["result_status"] or
                ev.get("model_calls") != 0 or ev.get("repair_author") is not None):
                raise ValueError("Boundary baseline identity changed")
            add_record(recorded(ev, "boundary_baseline"))
        manual_name = "reports/md2html-manual-recheck-20261024-v1/report.json"
        ev = report(manual_name, proof["report_hashes"][manual_name])
        if ev.get("repair_author") != "manual" or ev.get("model_calls") != 0:
            raise ValueError("Manual recheck cannot be relabelled as an AI repair")
        record = recorded(ev, "development")
        add_record(record)
        diff = ev["patch"]["diff"].encode()
        if hashlib.sha256(diff).hexdigest() != ev["patch"]["sha256"]:
            raise ValueError("Manual recheck patch differs from its evidence")
        name = "patches/" + ev["run_id"] + ".diff"
        files[name] = diff
        patches.append({"run_id": ev["run_id"], "file": name})
        fixed = record
    saved_path = project / 'reports/saved-model-candidate-md2html-20261024-v1/run-record.json'
    if saved_path.is_file():
        base = saved_path.parent.relative_to(project).as_posix() + '/'
        raw = source(base + 'run-record.json')
        record = json.loads(raw)
        if record['evidence'] != json.loads(source(base + 'report.json')):
            raise ValueError('Saved candidate verification report changed')
        add_record(record, raw)
        diff = record['evidence']['patch']['diff'].encode()
        name = 'patches/' + record['run_id'] + '.diff'
        files[name] = diff
        patches.append({'run_id':record['run_id'], 'file':name})
        graph = {row['run_id']:json.loads(files[row['file']]) for row in rows}
        validate_saved_candidates(graph)
        if record['result_status'] == 'verified_fix':
            model_fixes.append(record)
    highlights = [
        {"key": "fail", "run_id": rows[0]["run_id"]}, {"key": "fix", "run_id": fixed["run_id"]},
        {"key": "model_rounds", "run_id": latest["run_id"]}, {"key": "healthy", "run_id": rows[2]["run_id"]},
    ]
    # One AI-fix highlight per repository (its newest verified model fix), earliest repository first, so a later
    # repository adds a story instead of replacing the first one.
    highlights = model_fix_highlights(model_fixes) + highlights
    # A refresh replaces this snapshot: carry owner-curated note drafts too.
    for path in sorted((project/'web/demo/recordings/notes').glob('*.json')):
        files['notes/'+path.name]=source('web/demo/recordings/notes/'+path.name)
    manifest = {"format": FORMAT, "captured_at": datetime.now(timezone.utc).isoformat(), "scope": "development_replays",
                "notice": "Curated recorded evidence, not an evaluation or a new execution. Hashes detect byte changes, not reviewer/provider identity.",
                "files": {n: hashlib.sha256(raw).hexdigest() for n, raw in sorted(files.items())},
                "runs": rows, "patches": patches, "highlights": highlights, "sources": sources}
    return files, manifest


def validate_saved_candidates(runs):
    for record in runs.values():
        if record.get('recording_scope') != 'saved_model_candidate_validation':
            continue
        ev = record['evidence']
        proof = ev.get('candidate_provenance') or {}
        original = runs.get(proof.get('source_run_id'), {}).get('evidence') or {}
        calls = (original.get('model_ledger') or {}).get('calls', [])
        diagnosis = {k:v for k,v in (original.get('diagnosis') or {}).items() if k!='ledger'}
        if (ev.get('model_calls') != 0 or proof.get('new_model_calls') != 0 or
            record.get('repair_author') != 'model' or original.get('repair_author') != 'model' or
            len(calls) != 2 or original.get('model_calls') != 2 or
            proof.get('source_call_ids') != [c.get('call_id') for c in calls] or
            any(c.get('data_origin') != 'nebius_api' or not c.get('dispatch_started_at') for c in calls) or
            ev.get('acceptance_sha256') != original.get('acceptance_sha256') or ev.get('repo') != original.get('repo') or
            ev.get('candidate_patch', {}).get('patch') != original.get('candidate_patch', {}).get('patch') or
            digest(ev.get('candidate_patch', {}).get('patch')) != proof.get('candidate_sha256') or
            ev.get('patch', {}).get('sha256') != original.get('patch', {}).get('sha256') or
            ev.get('patch', {}).get('sha256') != proof.get('source_patch_sha256') or
            record.get('source_model_diagnosis') != diagnosis):
            raise ValueError('Saved candidate provenance differs from the real model proposal')
        if record.get('result_status') == 'verified_fix' and (
            [a.get('phase') for a in ev.get('attempts', [])] != ['original_readme','candidate_patch','fresh_verification'] or
            [a.get('status') for a in ev.get('attempts', [])] != ['failed','passed','passed']):
            raise ValueError('Saved model candidate needs both clean passes against the failed original')


def load(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("format") != FORMAT or manifest.get("scope") != "development_replays":
        raise ValueError("Unsupported public recording manifest")
    files = {}
    for name, expected in manifest["files"].items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or str(path) != name:
            raise ValueError("Recording path escapes its directory")
        target = root / name
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsupported recording file link")
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Public recording hash changed: " + name)
        checked_text(raw, name)
        files[name] = raw
    runs, patches = {}, {}
    for row in manifest["runs"]:
        record = json.loads(files[row["file"]])
        validate_record(record)
        if (any(record[k] != row[k] for k in ("run_id", "case_id", "result_status")) or
            record["evidence"]["run_id"] != row["evidence_run_id"] or record["run_id"] in runs):
            raise ValueError("Recording identity differs from manifest")
        runs[record["run_id"]] = record
    for row in manifest["patches"]:
        record = runs[row["run_id"]]
        raw = files[row["file"]]
        expected = record["evidence"].get("patch", {}).get("sha256")
        if not expected or hashlib.sha256(raw).hexdigest() != expected or not record.get("patch_available"):
            raise ValueError("Published patch differs from its recorded hash")
        patches[row["run_id"]] = raw.decode()
    validate_saved_candidates(runs)
    if any(h["run_id"] not in runs for h in manifest["highlights"]):
        raise ValueError("A demo highlight refers to an absent recorded run")
    return runs, patches, manifest
