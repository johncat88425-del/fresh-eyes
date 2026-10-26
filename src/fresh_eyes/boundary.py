"""Explicit boundary baseline entry; separate from repair and holdout scoring."""
import hashlib
import json
from pathlib import Path
import re

from .approval_io import timestamp
from .contracts import Contract, InvalidCase, digest
from .engine import run_case, save_report
from .evaluation import atomic_json, code_files
from .runner import DockerRunner

FORMAT = "fresh-eyes-boundary-baseline-plan-v1"


def plan(project, manifest, runner_id):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", runner_id):
        raise InvalidCase("Boundary plan needs an immutable runner image ID")
    project, manifest = Path(project), Path(manifest)
    inventory = json.loads(manifest.read_text())
    if inventory.get("kind") != "boundary" or inventory.get("schema_version") != 1:
        raise InvalidCase("Expected a boundary inventory")
    rows, ids = [], set()
    for entry in inventory["cases"]:
        name = entry["file"]
        if Path(name).name != name or not name.endswith(".json"):
            raise InvalidCase("Boundary contract must be a JSON basename")
        path = manifest.parent / name
        contract = Contract.load(path)
        if (path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["file_sha256"] or
            contract.sha256 != entry["contract_sha256"] or contract.acceptance_sha256 != entry["acceptance_sha256"] or
            contract.data["case_id"] != entry["case_id"] or entry["case_id"] in ids or entry["counted_in_fix_rate"] is not False or
            not set(entry["expected_result_status"]) <= {"unsupported", "needs_review", "unresolved", "needs_input"}):
            raise InvalidCase("Boundary contract bytes, identity or expected statuses changed")
        ids.add(entry["case_id"])
        rows.append({**entry, "repo": contract.data["repo"], "environment": contract.data["environment"]})
    value = {"format": FORMAT, "scope": "boundary_baseline", "counted_in_fix_rate": False,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(), "runner_id": runner_id,
        "dependency_mode": "pypi_live", "time_limit_s": 300, "code_files": code_files(project), "cases": rows,
        "model_calls": 0, "executed": False,
        "notice": "Original workflows only; no model diagnosis/repair. Human review is required before execution. A failed baseline alone does not prove why it failed."}
    value["payload_sha256"] = digest(value)
    return value


def review_template(value):
    return {"format": "fresh-eyes-boundary-human-reviews-v1", "plan_sha256": value["payload_sha256"],
            "approvals": [{"case_id": c["case_id"], "acceptance_sha256": c["acceptance_sha256"],
                           "status": "draft", "reviewed_by": None, "approved_at": None} for c in value["cases"]]}


def run(project, manifest, plan_path, reviews_path, source_root, output, *, docker="docker", runner_factory=None):
    saved = json.loads(Path(plan_path).read_text())
    if saved != plan(project, manifest, saved["runner_id"]):
        raise InvalidCase("Boundary code, contracts or configuration changed; prepare a new plan")
    reviews = json.loads(Path(reviews_path).read_text())
    if (reviews.get("format") != "fresh-eyes-boundary-human-reviews-v1" or
        reviews.get("plan_sha256") != saved["payload_sha256"]):
        raise InvalidCase("Boundary human reviews do not match the plan")
    approvals = reviews.get("approvals", [])
    if len(approvals) != len(saved["cases"]) or len({r.get("case_id") for r in approvals}) != len(approvals):
        raise InvalidCase("Boundary reviews require one attributed entry per case")
    by_id = {r["case_id"]: r for r in approvals}
    for c in saved["cases"]:
        r = by_id.get(c["case_id"], {})
        if (r.get("acceptance_sha256") != c["acceptance_sha256"] or r.get("status") != "approved" or
            not isinstance(r.get("reviewed_by"), str) or not r["reviewed_by"].strip() or not isinstance(r.get("approved_at"), str)):
            raise InvalidCase("Attributed human approval is required for every boundary case")
        timestamp(r["approved_at"])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / "plan.json", saved)
    results = []
    for c in saved["cases"]:
        data = Contract.load(Path(manifest).parent / c["file"]).data
        r = by_id[c["case_id"]]
        data["review"] = {k: r[k] for k in ("status", "reviewed_by", "approved_at", "acceptance_sha256")}
        contract = Contract.from_dict(data)
        target = output / c["case_id"]
        runner = runner_factory(contract) if runner_factory else DockerRunner(image=saved["runner_id"],
            platform=data["environment"]["platform"], executable=docker, dependency_mode="pypi_live")
        source = Path(source_root) / data["repo"]["url"].rstrip("/").split("/")[-1]
        report = run_case(contract, source, None, target, runner=runner, time_limit_s=saved["time_limit_s"])
        report.update(execution_scope="boundary_baseline", evaluation_eligible=False)
        save_report(report, target)
        results.append({"case_id": c["case_id"], "run_id": report["run_id"], "result_status": report["status"],
            "expected_result_status": c["expected_result_status"], "expected_status_match": report["status"] in c["expected_result_status"],
            "counted_in_fix_rate": False, "model_calls": report["model_calls"],
            "data_origin": "engineering_test" if report.get("environment", {}).get("test_double") else "execution",
            "report_sha256": hashlib.sha256((target / "report.json").read_bytes()).hexdigest()})
    summary = {"format": "fresh-eyes-boundary-baseline-results-v1", "scope": "boundary_baseline", "cases": results,
               "counted_in_fix_rate": False, "model_calls": 0, "limitations": saved["notice"]}
    atomic_json(output / "boundary-results.json", summary)
    return summary
