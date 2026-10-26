"""Publish only evidence revalidated by the formal exporter, then rebuild offline.

This code reads saved files only. Hashes bind bytes, not provider/reviewer identity.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile

from fresh_eyes.evaluation import export_results
from fresh_eyes.result_reviews import apply_reviews, template
from recordings import checked_text, validate_record

FORMAT = "fresh-eyes-public-evaluation-recordings-v1"


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def collect(project, roots):
    project, roots = Path(project), [Path(root) for root in roots]
    source = project / "evaluation/results.json"
    if not roots or not source.is_file() or source.is_symlink():
        raise ValueError("Evaluation capture needs real evaluation/results.json and record directories")
    raw_results = source.read_bytes()
    results = json.loads(checked_text(raw_results, "evaluation/results.json"))
    if results.get("data_origin") != "evaluation":
        raise ValueError("Only formal evaluation results may enter evaluation.json")
    # Reuse the producer's full freeze/contract/protocol/ledger/origin validation.
    # Never use a run ID plus an arbitrary matching report hash as sufficient proof.
    with tempfile.TemporaryDirectory(prefix="evaluation-publish-") as temporary:
        output = Path(temporary) / "checked-results.json"
        export_results(project, project / "cases/holdout/freeze.json", roots[0] / "freeze.json", roots, output)
        checked = json.loads(output.read_text())
        reviews = template(checked)
        supplied = {(r["case_id"], r["group"], r["run_id"]): r for r in results.get("runs", [])}
        for row in reviews["reviews"]:
            original = supplied.get((row["case_id"], row["group"], row["run_id"]), {})
            review = original.get("human_review")
            if review is not None:
                if set(review) != {"verdict", "reviewer", "reviewed_at", "note", "report_sha256", "scope"} or review.get("scope") != "team_patch_output_review":
                    raise ValueError("Malformed exported human result review")
                row.update({k: review[k] for k in ("verdict", "reviewer", "reviewed_at", "note", "report_sha256")})
        review_path = Path(temporary) / "existing-reviews.json"
        review_path.write_bytes(encoded(reviews))
        apply_reviews(checked, review_path)
        if checked != results:
            raise ValueError("Published results differ from the evidence-bound export")

    cases = {c["case_id"]: c for c in results["cases"]}
    bindings = {}
    for root in roots:
        for path in sorted(root.glob("*/binding.json")):
            body = json.loads(path.read_text())
            key = body["case_id"], body["group"], body["run_id"]
            if key in bindings:
                raise ValueError("Duplicate evaluation record identity")
            bindings[key] = path.parent
    files, rows, patches = {"results.json": raw_results}, [], []
    for row in results["runs"]:
        if row["run_state"] != "completed":
            continue  # Running rows remain in evaluation.json; never invent a final report.
        folder = bindings[(row["case_id"], row["group"], row["run_id"])]
        raw = (folder / "report.json").read_bytes()
        evidence = json.loads(raw)
        case = cases[row["case_id"]]
        if (hashlib.sha256(raw).hexdigest() != row["report_sha256"] or
                evidence["repo"].get("url") != case["repo_url"] or evidence["repo"].get("commit") != case["commit_sha"]):
            raise ValueError("Evaluation report changed or repo differs from its frozen case")
        ledger_path = folder / "model-ledger.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.is_file() else {"calls": [], "model_calls": 0}
        record = {"schema_version": 1, "data_origin": "execution", "record_source": "frozen_evaluation_report",
            "evaluation_eligible": False, "evaluation_group": row["group"], "case_available": False,
            "run_id": row["run_id"], "case_id": row["case_id"], "repo_url": case["repo_url"], "commit_sha": case["commit_sha"],
            "run_state": row["run_state"], "result_status": row["result_status"], "phase": evidence.get("current_phase"),
            "reason": evidence.get("reason"), "verification_level": "functional", "repair_mode": "none" if row["group"] == "A" else "model",
            "repair_author": evidence.get("repair_author"), "patch_available": False,
            "dependency_mode": evidence["dependency_mode"], "acceptance_sha256": row["acceptance_sha256"],
            "review": evidence["review"], "created_at": evidence["started_at"], "started_at": evidence["started_at"],
            "finished_at": evidence["finished_at"], "evidence": evidence, "result_review": row["human_review"],
            "costs": {"currency": "USD", "model_calls": ledger["calls"], "model_usd": row["model_usd"],
                      "runner_usd": None, "billed_usd": ledger.get("billed_usd"), "billing_status": row["model_cost_status"],
                      "paid_calls_enabled": False}}
        patch = folder / "candidate.diff"
        expected = evidence.get("patch", {}).get("sha256")
        if expected:
            if not patch.is_file() or patch.is_symlink() or hashlib.sha256(patch.read_bytes()).hexdigest() != expected:
                raise ValueError("Evaluation candidate patch missing or changed")
            name = "patches/" + record["run_id"] + ".diff"
            files[name] = patch.read_bytes()
            record["patch_available"] = True
            patches.append({"run_id": record["run_id"], "file": name})
        validate_record(record)
        evidence_file = "reports/" + record["run_id"] + ".json"
        files[evidence_file] = raw
        name = "runs/" + record["run_id"] + ".json"
        files[name] = encoded(record)
        rows.append({"run_id": record["run_id"], "case_id": row["case_id"], "group": row["group"],
                     "report_sha256": row["report_sha256"], "evidence_file": evidence_file, "file": name})
    for name, raw in files.items():
        checked_text(raw, name)
    manifest = {"format": FORMAT, "scope": "formal_evaluation_replays", "captured_at": datetime.now(timezone.utc).isoformat(),
        "freeze_sha256": results["freeze_sha256"], "files": {n: hashlib.sha256(v).hexdigest() for n, v in sorted(files.items())},
        "runs": rows, "patches": patches,
        "notice": "Exporter-validated saved evidence, not a new execution. Hashes are unsigned byte bindings, not proof of identities."}
    return files, manifest


def load(root):
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("format") != FORMAT or manifest.get("scope") != "formal_evaluation_replays":
        raise ValueError("Unsupported public evaluation recording manifest")
    files = {}
    for name, sha in manifest["files"].items():
        path = PurePosixPath(name)
        target = root / name
        if (path.is_absolute() or ".." in path.parts or str(path) != name or target.is_symlink() or
                not target.resolve().is_relative_to(root.resolve())):
            raise ValueError("Public evaluation recording path escapes its directory")
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError("Public evaluation recording hash changed: " + name)
        checked_text(raw, name)
        files[name] = raw
    results = json.loads(files["results.json"])
    if results.get("data_origin") != "evaluation" or results.get("freeze_sha256") != manifest["freeze_sha256"]:
        raise ValueError("Public evaluation result identity changed")
    completed = {r["run_id"]: r for r in results["runs"] if r["run_state"] == "completed"}
    runs, patches = {}, {}
    for row in manifest["runs"]:
        record = json.loads(files[row["file"]])
        validate_record(record)
        actual = completed.get(record["run_id"], {})
        evidence_raw = files[row["evidence_file"]]
        if (record["run_id"] in runs or row["run_id"] != record["run_id"] or
                any(row[k] != actual.get(k) for k in ("case_id", "group", "report_sha256")) or
                hashlib.sha256(evidence_raw).hexdigest() != row["report_sha256"] or
                json.loads(evidence_raw) != record["evidence"] or
                record["evaluation_group"] != row["group"] or record["result_status"] != actual.get("result_status") or
                record["acceptance_sha256"] != actual.get("acceptance_sha256") or
                record.get("result_review") != actual.get("human_review")):
            raise ValueError("Public evaluation report and result row disagree")
        runs[record["run_id"]] = record
    if set(runs) != set(completed):
        raise ValueError("A completed evaluation result has no openable report")
    for row in manifest["patches"]:
        record = runs[row["run_id"]]
        raw = files[row["file"]]
        if hashlib.sha256(raw).hexdigest() != record["evidence"].get("patch", {}).get("sha256") or not record["patch_available"]:
            raise ValueError("Published evaluation patch differs from evidence")
        patches[row["run_id"]] = raw.decode()
    return runs, patches, results, manifest
