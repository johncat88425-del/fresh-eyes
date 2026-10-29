"""Build a read-only static demo from versionable recordings, without keys or execution.

Default: python3.12 web/demo/build.py [--replace]
Explicit owner refresh of validated completed development reports: --refresh-recordings.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fresh_eyes.approval_io import contracts  # noqa: E402
from fresh_eyes.contracts import Contract  # noqa: E402
from recordings import FORMAT, checked_text, collect, load  # noqa: E402
import evaluation_recordings  # noqa: E402

MARKER = ".fresh-eyes-demo"
SUMMARY_KEYS = ("run_id", "case_id", "case_available", "repo_url", "commit_sha", "verification_level", "recording_scope", "run_state", "result_status", "dependency_mode", "created_at", "repair_mode", "data_origin", "started_at", "finished_at", "evaluation_group", "result_review")


def write(out, rel, value):
    path = out / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def refresh_recordings(project):
    """Only this explicit owner action reads reports/; normal builds use the snapshot."""
    target = project / "web/demo/recordings"
    if target.is_symlink() or (target.exists() and
            (not (target / "manifest.json").is_file() or json.loads((target / "manifest.json").read_text()).get("format") != FORMAT)):
        raise ValueError("Refusing to replace an unrecognized recordings directory")
    files, manifest = collect(project)
    with tempfile.TemporaryDirectory(prefix=".recordings-", dir=target.parent) as temporary:
        stage = Path(temporary) / "snapshot"
        stage.mkdir()
        for name, raw in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        write(stage, "manifest.json", manifest)
        load(stage)
        if target.exists():
            shutil.rmtree(target)
        stage.rename(target)


def cases_and_history(project):
    proof = project / "evidence/approval-history-verified.json"
    events = {e["case_id"]: e for e in json.loads(proof.read_text())["approval_events"]}
    cases, history = [], {}
    for case_id, contract in contracts(project).items():
        d = contract.data
        event = events.get(case_id)
        review = dict(d["review"])
        if event and event["acceptance_sha256"] == contract.acceptance_sha256:
            review = {"status": "approved", "reviewed_by": event["reviewed_by"], "approved_at": event["approved_at"],
                      "scope": "team_acceptance_review", "acceptance_sha256": event["acceptance_sha256"],
                      "review_revision": event["revision"]}
            history[case_id] = {"case_id": case_id, "acceptance_sha256": contract.acceptance_sha256,
                "items": [{"revision": event["revision"], "event_type": event["event_type"],
                           "recorded_at": event["recorded_at"], "review": review}],
                "notice": "Legacy snapshots preserve the observed current value; earlier overwritten history cannot be reconstructed."}
        elif review.get("status") == "approved":
            review = {"status": "draft", "scope": "team_acceptance_review"}
        slug = d["repo"]["url"].rstrip("/").split("/")[-1]
        cases.append({"case_id": case_id, "data_origin": "registered_contract", "repo_url": d["repo"]["url"],
            "commit_sha": d["repo"]["commit"], "goal": d["goal"], "readme_source": d["readme_source"],
            "environment": d["environment"], "acceptance_sha256": contract.acceptance_sha256,
            "contract_sha256": contract.sha256, "review": review, "upstream_maintainer_approval": False,
            "fixtures": d["fixtures"], "manual_candidate_available": (project / "cases" / f"{slug}-candidate-patch.json").is_file(),
            "execution_enabled": False, "can_run": False})
    return cases, history


BOUNDARY_REVIEWS = "web/demo/boundary-approvals.json"  # snapshot of evaluation/review-intake-20261024-v8/boundary-reviews.json


def boundary_cases(project):
    """Boundary cases (expected to stop) that a person approved, so the beginner pages can offer them next to the
    development cases. Only an approval whose acceptance hash matches the current contract counts."""
    if not (project / BOUNDARY_REVIEWS).is_file() or not (project / "cases/boundary/expected.json").is_file():
        return [], {}  # a partial project copy (some tests) has no boundary set to show
    approvals = {a["case_id"]: a for a in json.loads((project / BOUNDARY_REVIEWS).read_text())["approvals"]
                 if a.get("status") == "approved"}
    expected = {c["case_id"]: c for c in json.loads((project / "cases/boundary/expected.json").read_text())["cases"]}
    cases, history = [], {}
    for path in sorted((project / "cases/boundary").glob("*.json")):
        if path.name == "expected.json":
            continue
        contract = Contract.load(path)
        d, approval = contract.data, approvals.get(contract.data["case_id"])
        if not approval or approval["acceptance_sha256"] != contract.acceptance_sha256:
            continue
        cases.append({"case_id": d["case_id"], "data_origin": "boundary_contract", "group": "boundary",
            "repo_url": d["repo"]["url"], "commit_sha": d["repo"]["commit"], "goal": d["goal"],
            "readme_source": d["readme_source"], "environment": d["environment"],
            "acceptance_sha256": contract.acceptance_sha256, "contract_sha256": contract.sha256,
            "review": {"status": "approved", "reviewed_by": approval["reviewed_by"], "approved_at": approval["approved_at"],
                       "scope": "boundary_acceptance_review", "acceptance_sha256": approval["acceptance_sha256"]},
            "expected_result_status": expected.get(d["case_id"], {}).get("expected_result_status"),
            "upstream_maintainer_approval": False, "fixtures": d["fixtures"], "manual_candidate_available": False,
            "execution_enabled": False, "can_run": False})
        history[d["case_id"]] = {"case_id": d["case_id"], "acceptance_sha256": contract.acceptance_sha256,
            "items": [{"revision": 1, "event_type": "approved", "recorded_at": approval["approved_at"], "review": cases[-1]["review"]}],
            "notice": "From the boundary review intake (web/demo/boundary-approvals.json); one approval, no corrections."}
    return cases, history


def refresh_evaluation(project, roots):
    target = Path(project) / "web/demo/evaluation-recordings"
    if target.is_symlink() or (target.exists() and
            (not (target / "manifest.json").is_file() or
             json.loads((target / "manifest.json").read_text()).get("format") != evaluation_recordings.FORMAT)):
        raise ValueError("Refusing to replace an unrecognized evaluation recordings directory")
    files, manifest = evaluation_recordings.collect(project, roots)
    with tempfile.TemporaryDirectory(prefix=".evaluation-recordings-", dir=target.parent) as temporary:
        stage = Path(temporary) / "snapshot"
        stage.mkdir()
        for name, raw in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        write(stage, "manifest.json", manifest)
        evaluation_recordings.load(stage)
        if target.exists():
            shutil.rmtree(target)
        stage.rename(target)


def attach_notes(folder, runs):
    """Maintainer-note drafts (fresh-eyes maintainer-note, result.json) go on the recorded run whose verified patch
    they describe. Only unsent drafts bound to that exact patch; the ledger summary is not published."""
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        note = json.loads(path.read_text())
        record = next((r for r in runs.values() if note.get("run_id") in {r["run_id"], (r.get("evidence") or {}).get("run_id")}), None)
        patch = ((record or {}).get("evidence") or {}).get("patch") or {}
        if (record is None or note.get("status") != "draft" or note.get("posted") is not False or
                note.get("human_review_required") is not True or record.get("result_status") != "verified_fix" or
                not patch.get("sha256") or note.get("patch_sha256") != patch["sha256"] or note.get("diff") != patch.get("diff")):
            raise ValueError("Maintainer note does not match an unsent draft of a recorded verified fix: " + path.name)
        record["maintainer_note"] = {k: note[k] for k in ("title", "body", "footer", "diff", "patch_sha256", "run_id", "model", "status", "posted", "human_review_required")}


def build(project, out, replace=False, evaluation_records=None):
    project, requested = Path(project).resolve(), Path(out)
    out = requested.resolve()
    if (requested.is_symlink() or out == project or out in project.parents or
            out.is_relative_to(project / "web") or out.is_relative_to(project / "src")):
        raise ValueError("Unsafe demo output directory")
    if out.exists() and (not out.is_dir() or (any(out.iterdir()) and
            not (replace and (out / MARKER).is_file()))):
        raise ValueError("Output is not an empty directory or a recognized --replace demo")
    runs, patches, provenance = load(project / "web/demo/recordings")
    if evaluation_records:
        refresh_evaluation(project, evaluation_records)
    evaluation = None
    evaluation_proof = None
    snapshot = project / "web/demo/evaluation-recordings"
    source_results = project / "evaluation/results.json"
    if source_results.is_file() and json.loads(source_results.read_text()).get("data_origin") != "evaluation":
        raise ValueError("Only formal evaluation results may enter evaluation.json")
    if (snapshot / "manifest.json").is_file():
        held, held_patches, evaluation, evaluation_proof = evaluation_recordings.load(snapshot)
        if source_results.is_file() and source_results.read_bytes() != (snapshot / "results.json").read_bytes():
            raise ValueError("Evaluation results changed; capture their matching evidence again with --evaluation-records")
        if set(held) & set(runs):
            raise ValueError("Evaluation and development recording IDs collide")
        runs.update(held)
        patches.update(held_patches)
    elif source_results.is_file():
        raise ValueError("Evaluation results need openable captured reports; use --evaluation-records")
    attach_notes(project / "web/demo/recordings/notes", runs)
    cases, history = cases_and_history(project)
    boundary, boundary_history = boundary_cases(project)
    cases += boundary
    history.update(boundary_history)
    for record in runs.values():  # their recorded baselines now have a case page to link to
        if record.get("case_id") in {c["case_id"] for c in boundary}:
            record["case_available"] = True
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".demo-build-", dir=out.parent) as temporary:
        stage = Path(temporary) / "site"
        stage.mkdir()
        write(stage, MARKER, "Built by web/demo/build.py\n")
        write(stage, ".nojekyll", "")
        for name in ("index.html", "app.js", "i18n.js", "evaluation.js", "repair_path.js", "readme_plan.js", "github_readme.js", "nightly.html", "nightly_history.js"):
            shutil.copyfile(project / "web" / name, stage / name)
        shutil.copyfile(project / "web/demo/static-client.js", stage / "client.js")
        (stage / "examples").mkdir()
        shutil.copyfile(project / "web/examples/evaluation-example.json", stage / "examples/evaluation-example.json")
        if evaluation is not None:
            write(stage, "evaluation.json", evaluation)
            write(stage, "data/evaluation-provenance.json", evaluation_proof)
        built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write(stage, "data/health.json", {"schema_version": 1, "read_only": True, "demo_built_at": built_at,
            "recordings_captured_at": provenance["captured_at"], "demo_highlights": provenance["highlights"],
            "execution_enabled": False, "paid_calls_enabled": False, "live_pypi_enabled": False,
            "dependency_modes": ["offline_snapshot", "pypi_live"], "nightly_checks": False,
            "approval_history": True, "approval_corrections": False})
        write(stage, "data/cases.json", {"schema_version": 1, "items": cases})
        for case_id, body in history.items():
            write(stage, f"data/history/{case_id}.json", body)
        for record in runs.values():
            record["original_data_origin"] = record["data_origin"]
            record["data_origin"] = "recorded_replay"
            record["evaluation_eligible"] = False
            record.setdefault("created_at", record.get("started_at"))
            costs = record.setdefault("costs", {})
            record["historical_paid_activation"] = costs.get("paid_calls_enabled", False)
            costs["paid_calls_enabled"] = False
        ordered = sorted(runs.values(), key=lambda r: r.get("created_at") or "", reverse=True)
        write(stage, "data/runs.json", {"schema_version": 1, "items": [{k: r.get(k) for k in SUMMARY_KEYS} for r in ordered]})
        for run_id, record in runs.items():
            write(stage, f"data/runs/{run_id}.json", record)
        for run_id, diff in patches.items():
            write(stage, f"data/patches/{run_id}.diff", diff)
        examples = {}
        for path in sorted((project / "api/examples").glob("*.json")):
            record = json.loads(path.read_text())
            if record.get("data_origin") == "example":
                examples[path.stem] = record
                write(stage, f"data/examples/{path.stem}.json", record)
        write(stage, "data/examples.json", {"schema_version": 1, "items": examples})
        write(stage, "data/recording-provenance.json", provenance)
        files = {}
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                raw = path.read_bytes()
                name = path.relative_to(stage).as_posix()
                checked_text(raw, name)
                if name != MARKER:
                    files[name] = hashlib.sha256(raw).hexdigest()
        write(stage, "data/manifest.json", {"built_at": built_at, "files": files})
        if out.exists():
            shutil.rmtree(out)
        stage.rename(out)
    return {"cases": len(cases), "approved": sum(c["review"].get("status") == "approved" for c in cases),
            "recorded_runs": len(runs), "labelled_examples": len(examples), "files": len(files) + 2,
            "new_model_calls": 0, "published": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist/demo")
    parser.add_argument("--replace", action="store_true", help="replace a previously marked demo build")
    parser.add_argument("--refresh-recordings", action="store_true", help="owner: refresh the versionable inputs from the fixed reports whitelist")
    parser.add_argument("--evaluation-records", type=Path, action="append", default=[],
                        help="owner: validate and capture exported evaluation evidence; repeat for every result batch")
    args = parser.parse_args()
    try:
        if args.refresh_recordings:
            refresh_recordings(ROOT)
        result = build(ROOT, args.out, args.replace, args.evaluation_records)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Demo build stopped: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
